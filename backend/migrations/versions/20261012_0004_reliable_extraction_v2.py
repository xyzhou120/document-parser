"""Add the reliable extraction v2 ledger, content revisions and assistant tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.sqltypes import Text

revision = "20261012_0004"
down_revision = "20261009_0003"
branch_labels = None
depends_on = None


JSON = postgresql.JSON(astext_type=Text())
UUID = sa.UUID()


def _table(name, *columns, constraints=()):
    op.create_table(name, *columns, *constraints)


def _idx(name, table, columns, unique=False):
    op.create_index(name, table, columns, unique=unique)


def upgrade():
    op.add_column("parse_runs", sa.Column("content_revision_id", UUID, nullable=True))
    op.add_column("parse_runs", sa.Column("contract_version", sa.String(length=80), nullable=True))
    op.add_column("parse_runs", sa.Column("execution_generation", sa.Integer(), nullable=False, server_default="0"))
    _idx("ix_parse_runs_content_revision_id", "parse_runs", ["content_revision_id"])
    _idx("ix_parse_runs_contract_version", "parse_runs", ["contract_version"])

    _table("parse_field_jobs",
           sa.Column("job_id", UUID, nullable=False), sa.Column("run_id", UUID, nullable=False),
           sa.Column("field_key", sa.String(200), nullable=False), sa.Column("rule_hash", sa.String(64), nullable=False),
           sa.Column("revision_id", UUID, nullable=False), sa.Column("execution_state", sa.String(32), nullable=False),
           sa.Column("semantic_state", sa.String(32), nullable=False), sa.Column("phase", sa.String(50), nullable=False),
           sa.Column("checkpoint", JSON, nullable=False), sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
           sa.Column("lease_owner", sa.String(200), nullable=True), sa.Column("lease_until", sa.DateTime(), nullable=True),
           sa.Column("fence_token", sa.Integer(), nullable=False), sa.Column("progress_seq", sa.Integer(), nullable=False),
           sa.Column("last_error", sa.Text(), nullable=True), sa.Column("attempt", sa.Integer(), nullable=False),
           sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
           constraints=[sa.PrimaryKeyConstraint("job_id"), sa.UniqueConstraint("run_id", "field_key", name="uq_parse_field_job_run_field")])
    for n, c in [("run_id", "run_id"), ("field_key", "field_key"), ("rule_hash", "rule_hash"), ("revision_id", "revision_id"), ("execution_state", "execution_state"), ("semantic_state", "semantic_state"), ("next_attempt_at", "next_attempt_at"), ("created_at", "created_at"), ("updated_at", "updated_at")]: _idx(f"ix_parse_field_jobs_{n}", "parse_field_jobs", [c])

    _table("parse_range_work_items",
           sa.Column("work_id", UUID, nullable=False), sa.Column("job_id", UUID, nullable=False),
           sa.Column("strategy_version", sa.String(80), nullable=False), sa.Column("range_key", sa.String(300), nullable=False),
           sa.Column("cursor", JSON, nullable=True), sa.Column("scan_done", sa.Boolean(), nullable=False),
           sa.Column("state", sa.String(32), nullable=False), sa.Column("quality_blockers", JSON, nullable=False),
           sa.Column("last_error", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False),
           sa.Column("updated_at", sa.DateTime(), nullable=False),
           constraints=[sa.PrimaryKeyConstraint("work_id"), sa.UniqueConstraint("job_id", "strategy_version", "range_key", name="uq_parse_range_strategy")])
    for n in ("job_id", "strategy_version", "range_key", "scan_done", "state"): _idx(f"ix_parse_range_work_items_{n}", "parse_range_work_items", [n])

    _table("parse_candidates",
           sa.Column("candidate_id", UUID, nullable=False), sa.Column("job_id", UUID, nullable=False),
           sa.Column("stable_key", sa.String(500), nullable=False), sa.Column("block_ref", JSON, nullable=True),
           sa.Column("region_ref", JSON, nullable=True), sa.Column("span_ref", JSON, nullable=True),
           sa.Column("window", sa.Text(), nullable=True), sa.Column("context", sa.Text(), nullable=True),
           sa.Column("strength", sa.String(32), nullable=False), sa.Column("source", sa.String(32), nullable=False),
           sa.Column("state", sa.String(32), nullable=False), sa.Column("judgement", sa.String(32), nullable=True),
           sa.Column("evidence", JSON, nullable=True), sa.Column("split_path", JSON, nullable=True),
           sa.Column("range_work_id", UUID, nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False),
           sa.Column("updated_at", sa.DateTime(), nullable=False),
           constraints=[sa.PrimaryKeyConstraint("candidate_id"), sa.UniqueConstraint("job_id", "stable_key", name="uq_parse_candidate_stable_key")])
    for n in ("job_id", "range_work_id", "strength", "source", "state", "created_at"): _idx(f"ix_parse_candidates_{n}", "parse_candidates", [n])

    _table("parse_model_calls",
           sa.Column("call_id", UUID, nullable=False), sa.Column("job_id", UUID, nullable=False), sa.Column("fence", sa.Integer(), nullable=False),
           sa.Column("provider_key", sa.String(200), nullable=False), sa.Column("dispatch_key", sa.String(500), nullable=False),
           sa.Column("checkpoint_key", sa.String(300), nullable=False), sa.Column("retry_seq", sa.Integer(), nullable=False),
           sa.Column("retry_of_call_id", UUID, nullable=True), sa.Column("candidate_set_hash", sa.String(64), nullable=True),
           sa.Column("split_path", JSON, nullable=True), sa.Column("state", sa.String(32), nullable=False),
           sa.Column("charge_state", sa.String(32), nullable=False), sa.Column("reservation", JSON, nullable=True),
           sa.Column("usage", JSON, nullable=True), sa.Column("usage_revision", sa.Integer(), nullable=False),
           sa.Column("unknown_deadline", sa.DateTime(), nullable=True), sa.Column("usage_quality", sa.String(32), nullable=True),
           sa.Column("finish_reason", sa.String(64), nullable=True), sa.Column("provider_request_id", sa.String(300), nullable=True),
           sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("sent_at", sa.DateTime(), nullable=True), sa.Column("completed_at", sa.DateTime(), nullable=True),
           constraints=[sa.PrimaryKeyConstraint("call_id"), sa.UniqueConstraint("dispatch_key", name="uq_parse_model_calls_dispatch_key")])
    for n in ("job_id", "provider_key", "dispatch_key", "checkpoint_key", "retry_of_call_id", "state", "charge_state", "provider_request_id", "created_at"): _idx(f"ix_parse_model_calls_{n}", "parse_model_calls", [n])

    _table("execution_outbox", sa.Column("event_id", UUID, nullable=False), sa.Column("kind", sa.String(100), nullable=False), sa.Column("aggregate_id", UUID, nullable=False), sa.Column("dedupe_key", sa.String(500), nullable=False), sa.Column("payload", JSON, nullable=False), sa.Column("dispatch_state", sa.String(32), nullable=False), sa.Column("next_attempt_at", sa.DateTime(), nullable=True), sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("last_error", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("event_id"), sa.UniqueConstraint("kind", "dedupe_key", name="uq_execution_outbox_kind_dedupe")])
    for n in ("kind", "aggregate_id", "dispatch_state", "next_attempt_at", "created_at"): _idx(f"ix_execution_outbox_{n}", "execution_outbox", [n])

    _table("content_build_jobs", sa.Column("build_id", UUID, nullable=False), sa.Column("document_id", UUID, nullable=False), sa.Column("file_hash", sa.String(64), nullable=False), sa.Column("profile", JSON, nullable=False), sa.Column("parent_revision_id", UUID, nullable=True), sa.Column("phase", sa.String(50), nullable=False), sa.Column("checkpoint", JSON, nullable=False), sa.Column("dispatch_state", sa.String(32), nullable=False), sa.Column("attempt", sa.Integer(), nullable=False), sa.Column("lease_owner", sa.String(200), nullable=True), sa.Column("lease_until", sa.DateTime(), nullable=True), sa.Column("fence_token", sa.Integer(), nullable=False), sa.Column("last_error", sa.Text(), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("build_id")])
    for n in ("document_id", "file_hash", "parent_revision_id", "phase", "dispatch_state", "created_at"): _idx(f"ix_content_build_jobs_{n}", "content_build_jobs", [n])
    _table("content_revisions", sa.Column("revision_id", UUID, nullable=False), sa.Column("document_id", UUID, nullable=False), sa.Column("artifact_id", UUID, nullable=False), sa.Column("parent_revision_id", UUID, nullable=True), sa.Column("origin_source_revision_id", UUID, nullable=True), sa.Column("origin_event_id", sa.String(200), nullable=True), sa.Column("file_hash", sa.String(64), nullable=False), sa.Column("content_digest", sa.String(64), nullable=False), sa.Column("preprocess_manifest", JSON, nullable=False), sa.Column("adapter_version", sa.String(80), nullable=False), sa.Column("source_kind", sa.String(50), nullable=False), sa.Column("change_kind", sa.String(50), nullable=False), sa.Column("coverage_manifest", JSON, nullable=False), sa.Column("coverage_digest", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("revision_id"), sa.UniqueConstraint("origin_source_revision_id", name="uq_content_revision_source_origin")])
    for n in ("document_id", "artifact_id", "parent_revision_id", "origin_source_revision_id", "origin_event_id", "file_hash", "content_digest", "created_at"): _idx(f"ix_content_revisions_{n}", "content_revisions", [n])
    _table("content_impacts", sa.Column("impact_id", UUID, nullable=False), sa.Column("from_revision_id", UUID, nullable=False), sa.Column("to_revision_id", UUID, nullable=False), sa.Column("changed_ranges", JSON, nullable=False), sa.Column("dependency_precision", sa.String(32), nullable=False), sa.Column("recompute_policy", sa.String(32), nullable=False), sa.Column("invalidation_scope", JSON, nullable=False), sa.Column("request_id", sa.String(200), nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("impact_id")])
    for n in ("from_revision_id", "to_revision_id", "request_id", "created_at"): _idx(f"ix_content_impacts_{n}", "content_impacts", [n])
    _table("content_invalidations", sa.Column("invalidation_id", UUID, nullable=False), sa.Column("document_id", UUID, nullable=False), sa.Column("invalidation_kind", sa.String(40), nullable=False), sa.Column("scope_selector", JSON, nullable=False), sa.Column("effective_at", sa.DateTime(), nullable=False), sa.Column("reason", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("invalidation_id")])
    for n in ("document_id", "invalidation_kind", "effective_at", "created_at"): _idx(f"ix_content_invalidations_{n}", "content_invalidations", [n])
    _table("document_processing_heads", sa.Column("document_id", UUID, nullable=False), sa.Column("active_content_revision_id", UUID, nullable=True), sa.Column("current_parse_run_id", UUID, nullable=True), sa.Column("active_index_generation_id", UUID, nullable=True), sa.Column("cas_version", sa.Integer(), nullable=False), sa.Column("invalidation_watermark", sa.BigInteger(), nullable=False, server_default="0"), sa.Column("updated_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("document_id")])
    for n in ("active_content_revision_id", "current_parse_run_id", "active_index_generation_id"): _idx(f"ix_document_processing_heads_{n}", "document_processing_heads", [n])
    _table("index_generations", sa.Column("generation_id", UUID, nullable=False), sa.Column("revision_id", UUID, nullable=False), sa.Column("config_hash", sa.String(64), nullable=False), sa.Column("snapshot_hash", sa.String(64), nullable=True), sa.Column("build_status", sa.String(32), nullable=False), sa.Column("coverage_status", sa.String(32), nullable=False), sa.Column("serve_status", sa.String(32), nullable=False), sa.Column("serve_version", sa.Integer(), nullable=False), sa.Column("indexed_block_manifest", JSON, nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("generation_id"), sa.UniqueConstraint("revision_id", "config_hash", "snapshot_hash", name="uq_index_generation_revision_config_snapshot")])
    for n in ("revision_id", "build_status", "coverage_status", "serve_status", "created_at"): _idx(f"ix_index_generations_{n}", "index_generations", [n])
    _table("index_chunks", sa.Column("chunk_row_id", UUID, nullable=False), sa.Column("generation_id", UUID, nullable=False), sa.Column("chunk_id", sa.String(300), nullable=False), sa.Column("block_ref", JSON, nullable=True), sa.Column("table_ref", JSON, nullable=True), sa.Column("text", sa.Text(), nullable=False), sa.Column("embedding", JSON, nullable=True), sa.Column("embedding_config", JSON, nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("chunk_row_id"), sa.UniqueConstraint("generation_id", "chunk_id", name="uq_index_chunk_generation_key")])
    for n in ("generation_id", "created_at"): _idx(f"ix_index_chunks_{n}", "index_chunks", [n])

    _table("qa_sessions", sa.Column("session_id", UUID, nullable=False), sa.Column("document_id", UUID, nullable=False), sa.Column("title", sa.String(300), nullable=True), sa.Column("active_revision_id", UUID, nullable=False), sa.Column("active_generation_id", UUID, nullable=True), sa.Column("status", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("session_id")])
    for n in ("document_id", "active_revision_id", "active_generation_id", "status", "created_at"): _idx(f"ix_qa_sessions_{n}", "qa_sessions", [n])
    _table("qa_messages", sa.Column("message_id", UUID, nullable=False), sa.Column("session_id", UUID, nullable=False), sa.Column("parent_message_id", UUID, nullable=True), sa.Column("client_message_id", sa.String(200), nullable=False), sa.Column("question", sa.Text(), nullable=False), sa.Column("mode", sa.String(50), nullable=False, server_default="answer"), sa.Column("revision_id", UUID, nullable=False), sa.Column("generation_id", UUID, nullable=True), sa.Column("scope_snapshot", JSON, nullable=False), sa.Column("execution_state", sa.String(32), nullable=False), sa.Column("answer", sa.Text(), nullable=True), sa.Column("claims", JSON, nullable=True), sa.Column("citations", JSON, nullable=True), sa.Column("error", JSON, nullable=True), sa.Column("checkpoint", JSON, nullable=False), sa.Column("generation_fence", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("message_id"), sa.UniqueConstraint("session_id", "client_message_id", name="uq_qa_message_client_id")])
    for n in ("session_id", "parent_message_id", "revision_id", "generation_id", "execution_state", "created_at"): _idx(f"ix_qa_messages_{n}", "qa_messages", [n])
    _table("document_cards", sa.Column("card_id", UUID, nullable=False), sa.Column("document_id", UUID, nullable=False), sa.Column("revision_id", UUID, nullable=False), sa.Column("generation_id", UUID, nullable=True), sa.Column("status", sa.String(32), nullable=False), sa.Column("summary", sa.Text(), nullable=True), sa.Column("outline", JSON, nullable=True), sa.Column("risks", JSON, nullable=True), sa.Column("citations", JSON, nullable=True), sa.Column("error", JSON, nullable=True), sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False), constraints=[sa.PrimaryKeyConstraint("card_id"), sa.UniqueConstraint("revision_id", "generation_id", name="uq_document_card_revision_generation")])
    for n in ("document_id", "revision_id", "generation_id", "status", "created_at"): _idx(f"ix_document_cards_{n}", "document_cards", [n])
    # Explicit relationships are added after all referenced tables exist.
    for name, table, referent, cols, refcols in (
        ("fk_parse_runs_content_revision", "parse_runs", "content_revisions", ["content_revision_id"], ["revision_id"]),
        ("fk_parse_field_jobs_revision_id", "parse_field_jobs", "content_revisions", ["revision_id"], ["revision_id"]),
        ("fk_parse_field_jobs_run_id", "parse_field_jobs", "parse_runs", ["run_id"], ["parse_run_id"]),
        ("fk_parse_range_work_items_job_id", "parse_range_work_items", "parse_field_jobs", ["job_id"], ["job_id"]),
        ("fk_parse_candidates_range_work_id", "parse_candidates", "parse_range_work_items", ["range_work_id"], ["work_id"]),
        ("fk_parse_candidates_job_id", "parse_candidates", "parse_field_jobs", ["job_id"], ["job_id"]),
        ("fk_parse_model_calls_job_id", "parse_model_calls", "parse_field_jobs", ["job_id"], ["job_id"]),
        ("fk_parse_model_calls_retry_of_call_id", "parse_model_calls", "parse_model_calls", ["retry_of_call_id"], ["call_id"]),
        ("fk_content_build_jobs_document_id", "content_build_jobs", "documents", ["document_id"], ["document_id"]),
        ("fk_content_build_jobs_parent_revision_id", "content_build_jobs", "content_revisions", ["parent_revision_id"], ["revision_id"]),
        ("fk_content_revisions_parent_revision_id", "content_revisions", "content_revisions", ["parent_revision_id"], ["revision_id"]),
        ("fk_content_revisions_origin_source_revision_id", "content_revisions", "source_revisions", ["origin_source_revision_id"], ["revision_id"]),
        ("fk_content_revisions_document_id", "content_revisions", "documents", ["document_id"], ["document_id"]),
        ("fk_content_revisions_artifact_id", "content_revisions", "document_artifacts", ["artifact_id"], ["artifact_id"]),
        ("fk_content_impacts_to_revision_id", "content_impacts", "content_revisions", ["to_revision_id"], ["revision_id"]),
        ("fk_content_impacts_from_revision_id", "content_impacts", "content_revisions", ["from_revision_id"], ["revision_id"]),
        ("fk_content_invalidations_document_id", "content_invalidations", "documents", ["document_id"], ["document_id"]),
        ("fk_document_processing_heads_current_parse_run_id", "document_processing_heads", "parse_runs", ["current_parse_run_id"], ["parse_run_id"]),
        ("fk_document_processing_heads_active_index_generation_id", "document_processing_heads", "index_generations", ["active_index_generation_id"], ["generation_id"]),
        ("fk_document_processing_heads_document_id", "document_processing_heads", "documents", ["document_id"], ["document_id"]),
        ("fk_document_processing_heads_active_content_revision_id", "document_processing_heads", "content_revisions", ["active_content_revision_id"], ["revision_id"]),
        ("fk_index_generations_revision_id", "index_generations", "content_revisions", ["revision_id"], ["revision_id"]),
        ("fk_index_chunks_generation_id", "index_chunks", "index_generations", ["generation_id"], ["generation_id"]),
        ("fk_qa_sessions_document_id", "qa_sessions", "documents", ["document_id"], ["document_id"]),
        ("fk_qa_sessions_active_revision_id", "qa_sessions", "content_revisions", ["active_revision_id"], ["revision_id"]),
        ("fk_qa_sessions_active_generation_id", "qa_sessions", "index_generations", ["active_generation_id"], ["generation_id"]),
        ("fk_qa_messages_parent_message_id", "qa_messages", "qa_messages", ["parent_message_id"], ["message_id"]),
        ("fk_qa_messages_revision_id", "qa_messages", "content_revisions", ["revision_id"], ["revision_id"]),
        ("fk_qa_messages_generation_id", "qa_messages", "index_generations", ["generation_id"], ["generation_id"]),
        ("fk_qa_messages_session_id", "qa_messages", "qa_sessions", ["session_id"], ["session_id"]),
        ("fk_document_cards_document_id", "document_cards", "documents", ["document_id"], ["document_id"]),
        ("fk_document_cards_generation_id", "document_cards", "index_generations", ["generation_id"], ["generation_id"]),
        ("fk_document_cards_revision_id", "document_cards", "content_revisions", ["revision_id"], ["revision_id"]),
    ):
        op.create_foreign_key(name, table, referent, cols, refcols)
    for name, table, expression in (
        ("ck_field_job_execution_state", "parse_field_jobs", "execution_state IN ('queued','running','yielded','waiting_service','waiting_external_quota','paused','cancelled','done','failed_terminal','superseded')"),
        ("ck_field_job_semantic_state", "parse_field_jobs", "semantic_state IN ('unknown','completed','review_required','partial_candidates','not_found','incomplete')"),
        ("ck_field_job_counters", "parse_field_jobs", "fence_token >= 0 AND progress_seq >= 0 AND attempt >= 0"),
        ("ck_model_call_state", "parse_model_calls", "state IN ('queued','sending','started','succeeded','length','invalid','timeout','uncertain')"),
        ("ck_model_call_charge_state", "parse_model_calls", "charge_state IN ('reserved','actual','uncertain','reconciled')"),
        ("ck_model_call_counters", "parse_model_calls", "fence >= 0 AND retry_seq >= 0 AND usage_revision >= 0"),
        ("ck_content_build_counters", "content_build_jobs", "attempt >= 0 AND fence_token >= 0"),
        ("ck_processing_head_counters", "document_processing_heads", "cas_version >= 0 AND invalidation_watermark >= 0"),
        ("ck_index_generation_coverage_status", "index_generations", "coverage_status IN ('unknown','partial','full')"),
        ("ck_index_generation_serve_status", "index_generations", "serve_status IN ('unavailable','eligible','blocked')"),
        ("ck_index_generation_build_status", "index_generations", "build_status IN ('queued','building','succeeded','failed')"),
        ("ck_index_generation_serve_version", "index_generations", "serve_version >= 0"),
        ("ck_qa_message_generation_fence", "qa_messages", "generation_fence >= 0"),
    ):
        op.create_check_constraint(name, table, expression)

    # Content versions and impact records are the immutable source-of-truth
    # for current answers.  The same append-only trigger is emitted by the
    # empty-schema bootstrap generator.
    op.execute("""CREATE OR REPLACE FUNCTION reject_platform_audit_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$;""")
    for table in ("content_revisions", "content_impacts", "content_invalidations"):
        op.execute(f"""CREATE TRIGGER {table}_immutable_audit BEFORE UPDATE OR DELETE ON {table}
FOR EACH ROW EXECUTE FUNCTION reject_platform_audit_mutation()""")


def downgrade():
    raise RuntimeError("Reliable extraction v2 downgrade is intentionally disabled; restore an isolated backup")
