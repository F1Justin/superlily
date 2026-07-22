"""Add deterministic document render artifacts and delivery evidence."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0017_render_delivery"
down_revision: str | None = "0016_confirm_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "render_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("conversation_key", sa.String(320), nullable=False),
        sa.Column("source_event_id", sa.String(512)),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("document_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("safe_error_code", sa.String(64)),
        sa.Column("render_duration_ms", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["instance_id"], ["bot_instances.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("instance_id", "idempotency_key", name="uq_render_document_idempotency"),
        sa.CheckConstraint("status IN ('pending', 'succeeded', 'failed')", name="ck_render_document_status"),
        sa.CheckConstraint("render_duration_ms IS NULL OR render_duration_ms BETWEEN 0 AND 120000", name="ck_render_document_duration"),
        sa.CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND safe_error_code IS NULL) OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL AND safe_error_code IS NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND safe_error_code IS NOT NULL)",
            name="ck_render_document_terminal",
        ),
    )
    op.create_index("ix_render_documents_conversation_created", "render_documents", ["conversation_key", "created_at"])
    op.create_table(
        "render_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("render_id", sa.String(36), nullable=False, unique=True),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(256), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("width_pixels", sa.Integer(), nullable=False),
        sa.Column("height_pixels", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["render_id"], ["render_documents.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("mime_type = 'image/png'", name="ck_render_artifact_mime"),
        sa.CheckConstraint("byte_size BETWEEN 1 AND 8388608", name="ck_render_artifact_bytes"),
        sa.CheckConstraint("width_pixels BETWEEN 1 AND 4096 AND height_pixels BETWEEN 1 AND 4096", name="ck_render_artifact_dimensions"),
        sa.CheckConstraint("expires_at > created_at", name="ck_render_artifact_expiry"),
    )
    op.create_index("ix_render_artifacts_hash", "render_artifacts", ["content_sha256"])
    op.create_table(
        "render_delivery_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("platform_message_id", sa.String(512)),
        sa.Column("safe_error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["render_artifacts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instance_id"], ["bot_instances.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("outcome IN ('succeeded', 'failed', 'ambiguous')", name="ck_render_delivery_outcome"),
        sa.CheckConstraint(
            "(outcome = 'succeeded' AND platform_message_id IS NOT NULL AND safe_error_code IS NULL) OR "
            "(outcome <> 'succeeded' AND safe_error_code IS NOT NULL)",
            name="ck_render_delivery_evidence",
        ),
    )
    op.create_index("ix_render_delivery_artifact_created", "render_delivery_attempts", ["artifact_id", "created_at"])

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for operation in ("UPDATE", "DELETE"):
            suffix = operation.lower()
            op.execute(
                f"CREATE TRIGGER render_delivery_attempts_no_{suffix} "
                f"BEFORE {operation} ON render_delivery_attempts BEGIN "
                "SELECT RAISE(ABORT, 'render delivery evidence is append-only'); END"
            )
    elif bind.dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION reject_render_delivery_attempt_mutation() RETURNS trigger "
            "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'render delivery evidence is append-only'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER render_delivery_attempts_no_mutation BEFORE UPDATE OR DELETE "
            "ON render_delivery_attempts FOR EACH ROW EXECUTE FUNCTION reject_render_delivery_attempt_mutation()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS reject_render_delivery_attempt_mutation() CASCADE")
    op.drop_index("ix_render_delivery_artifact_created", table_name="render_delivery_attempts")
    op.drop_table("render_delivery_attempts")
    op.drop_index("ix_render_artifacts_hash", table_name="render_artifacts")
    op.drop_table("render_artifacts")
    op.drop_index("ix_render_documents_conversation_created", table_name="render_documents")
    op.drop_table("render_documents")
