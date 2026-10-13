"""P06 持久化扫描和分页契约（无需数据库或模型）。"""

from app.services.candidate_repository import validate_scan_batch
from app.services.candidate_retrieval import FieldSpec, retrieve, stable_candidate_key
from app.services.retrieval_plan import (
    CandidateBatchTree,
    ScanCursor,
    build_range_shards,
    candidate_set_digest,
    decode_cursor,
    encode_cursor,
    scan_candidates,
)


def spec():
    return FieldSpec("f", "目标含量", ["目标含量"], "m³/t", True, True)


def page(number, *lines, table=None):
    blocks = [{"text": line, "bbox": [0, i * 20, 100, i * 20 + 10], "source": "native"}
              for i, line in enumerate(lines)]
    return {"page_number": number, "text": "\n".join(lines), "text_blocks": blocks,
            "image_count": 0, "ocr_status": "not_needed", "tables": [table] if table else []}


def test_scan_cursor_is_opaque_and_round_trips():
    cursor = ScanCursor("rev-1", 7, 2, "p1-p2", False)
    assert decode_cursor(encode_cursor(cursor)).as_dict() == cursor.as_dict()


def test_scan_pages_are_paginated_without_max_candidate_loss():
    pages = [page(1, "目标含量 1 m³/t", "目标含量 2 m³/t"),
             page(2, "目标含量 3 m³/t"), page(3, "目标含量 4 m³/t")]
    shard = build_range_shards(pages, ["s"], pages_per_range=3)[0]
    first = scan_candidates(spec(), pages, shard, page_limit=1, content_revision="rev", rule_semantic_hash="r")
    second = scan_candidates(spec(), pages, shard, cursor=first.cursor, page_limit=2,
                             content_revision="rev", rule_semantic_hash="r")
    assert [c.numbers[0] for c in first.candidates] == ["1", "2"]
    assert [c.numbers[0] for c in second.candidates] == ["3", "4"]
    assert second.done


def test_stable_key_ignores_candidate_rank_and_changes_on_revision():
    result = retrieve(spec(), [page(2, "目标含量 2.2 m³/t")], max_candidates=None,
                      content_revision="rev-a", rule_semantic_hash="rule")
    candidate = result.candidates[0]
    assert candidate.stable_key == stable_candidate_key("rev-a", "rule", candidate)
    assert candidate.stable_key != stable_candidate_key("rev-b", "rule", candidate)
    assert candidate_set_digest([candidate])


def test_cross_page_table_header_is_kept_as_candidate_context():
    table = {"table_id": "t-1", "header": "评价单元 | 目标含量(m³/t)"}
    first = page(1, "评价单元 | 目标含量(m³/t)", table=table)
    second = page(2, "目标含量 2.20 m³/t", table=table)
    # 即使在续页上，表格块元数据也要传递给候选项。
    candidate = retrieve(spec(), [first, second], max_candidates=None).candidates[0]
    assert "评价单元" in candidate.context or candidate.table


def test_batch_tree_keeps_missing_ids_pending_and_closes_after_children():
    tree = CandidateBatchTree()
    tree.add("root", ["a", "b", "c"])
    outcome = type("Outcome", (), {"decisions": [], "split_batches": [
        {"candidate_ids": ["a"], "split_path": ["0"], "candidates": [{"id": "a", "image_refs": ["img"]}]},
        {"candidate_ids": ["b", "c"], "split_path": ["1"], "candidates": [{"id": "b"}, {"id": "c"}]},
    ]})()
    assert tree.apply_outcome("root", outcome) == []
    child = type("Outcome", (), {"decisions": [{"candidate_id": "a", "judgement": "accepted"}], "split_batches": []})()
    assert tree.apply_outcome("root/0", child) == []
    child2 = type("Outcome", (), {"decisions": [{"candidate_id": "b", "judgement": "rejected"}], "split_batches": []})()
    assert tree.apply_outcome("root/1", child2) == ["c"]
    child3 = type("Outcome", (), {"decisions": [{"candidate_id": "c", "judgement": "duplicate"}], "split_batches": []})()
    tree.apply_outcome("root/1", child3)
    assert tree.batches["root"]["batch_state"] == "done"


def test_batch_tree_rejects_late_parent_and_overlapping_or_incomplete_split():
    tree = CandidateBatchTree()
    tree.add("root", ["a", "b"])
    split = type("Outcome", (), {"decisions": [], "split_batches": [
        {"candidate_ids": ["a"], "split_path": ["0"]},
        {"candidate_ids": ["b"], "split_path": ["1"]},
    ]})()
    tree.apply_outcome("root", split)
    try:
        tree.apply_outcome("root", split)
        raise AssertionError("late parent outcome accepted")
    except ValueError:
        pass
    overlap = CandidateBatchTree()
    overlap.add("root", ["a", "b"])
    bad = type("Outcome", (), {"decisions": [], "split_batches": [
        {"candidate_ids": ["a", "b"], "split_path": ["0"]},
        {"candidate_ids": ["b"], "split_path": ["1"]},
    ]})()
    try:
        overlap.apply_outcome("root", bad)
        raise AssertionError("overlapping split accepted")
    except ValueError:
        pass


def test_scan_batch_validation_rejects_wrong_revision_range_or_page():
    work = type("Work", (), {"range_key": "p2-p3"})()
    batch = type("Batch", (), {"cursor": ScanCursor("rev-1", 1, 0, "p2-p3", False),
                                "scanned_pages": [2], "candidates": []})()
    validate_scan_batch(work, batch, "rev-1")
    for cursor, pages in ((ScanCursor("rev-2", 1, 0, "p2-p3", False), [2]),
                          (ScanCursor("rev-1", 1, 0, "p4-p4", False), [2]),
                          (ScanCursor("rev-1", 1, 0, "p2-p3", False), [4])):
        bad = type("Batch", (), {"cursor": cursor, "scanned_pages": pages, "candidates": []})()
        try:
            validate_scan_batch(work, bad, "rev-1")
            raise AssertionError("invalid scan batch accepted")
        except ValueError:
            pass


def test_batch_tree_allows_same_candidate_only_for_disjoint_unit_work_scopes():
    tree = CandidateBatchTree()
    tree.add("root", ["a"])
    outcome = type("Outcome", (), {"decisions": [], "split_batches": [
        {"candidate_ids": ["a"], "parent_unit_work_ids": ["u1", "u2"],
         "unit_work_ids": ["u1"], "candidate_unit_work_ids": {"a": ["u1"]},
         "split_path": ["0"], "candidates": [{"candidate_id": "a", "payload": {"unit_work_ids": ["u1"], "units": ["A"]}}]},
        {"candidate_ids": ["a"], "parent_unit_work_ids": ["u1", "u2"],
         "unit_work_ids": ["u2"], "candidate_unit_work_ids": {"a": ["u2"]},
         "split_path": ["1"], "candidates": [{"candidate_id": "a", "payload": {"unit_work_ids": ["u2"], "units": ["B"]}}]},
    ]})()
    tree.apply_outcome("root", outcome)
    accepted = type("Outcome", (), {"decisions": [{"candidate_id": "a", "judgement": "accepted"}], "split_batches": []})()
    tree.apply_outcome("root/0", accepted)
    tree.apply_outcome("root/1", accepted)
    assert tree.batches["root"]["batch_state"] == "done"
