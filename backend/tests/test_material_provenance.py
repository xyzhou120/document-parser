from app.services.material_provenance import build_material_provenance, provenance_for_page


def page(number, blocks):
    return {"page_number": number, "text_blocks": blocks}


def test_report_title_proves_report_body_for_later_pages_with_revision_location():
    pages = [page(1, [{"block_id": "title", "label": "paragraph_title",
                      "text": "4034±13综采工作面瓦斯抽采达标评判报告"}]),
             page(6, [{"block_id": "body", "label": "text", "text": "工作面日产量约8828t，正文提到DGC检测仪"}])]
    result = build_material_provenance(pages, "rev-1")
    assert result[6]["role"] == "report_body"
    assert result[6]["content_revision"] == "rev-1"
    assert result[6]["basis"].startswith("source.material_title:")
    assert result[6]["page_number"] == 1 and result[6]["scope_page_number"] == 6
    assert result[6]["block_id"] == "title"


def test_dgc_body_mention_does_not_promote_report_sheet():
    pages = [page(1, [{"block_id": "title", "label": "doc_title", "text": "评价报告"}]),
             page(2, [{"block_id": "body", "label": "text", "text": "DGC检测单数据见下表"}])]
    result = build_material_provenance(pages, "rev-1")
    assert result[2]["role"] == "report_body"


def test_explicit_attachment_heading_overrides_following_material_range():
    pages = [page(1, [{"block_id": "title", "label": "doc_title", "text": "主报告评价报告"}]),
             page(3, [{"block_id": "dgc", "label": "doc_title", "text": "DGC检测单"}]),
             page(4, [{"block_id": "body", "label": "text", "text": "原始记录 0.18"}])]
    result = build_material_provenance(pages, "rev-1")
    assert result[3]["role"] == "report_sheet"
    assert result[4]["role"] == "report_sheet"
    assert provenance_for_page(pages, 3, "rev-1")["block_id"] == "dgc"


def test_long_mislabelled_body_does_not_become_a_material_title():
    pages = [page(3, [{"label": "paragraph_title", "text": "设备说明" * 50 + "DGC检测单"}])]
    assert build_material_provenance(pages, "rev-1") == {}
