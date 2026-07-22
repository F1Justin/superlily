"""Add retryable render attempts and capability-aware delivery intents."""

from collections.abc import Sequence
from datetime import timedelta

from alembic import op
import sqlalchemy as sa


revision: str = "0018_render_attempt_delivery"
down_revision: str | None = "0017_render_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "render_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("render_id", sa.String(36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("renderer_profile", sa.String(64), nullable=False),
        sa.Column("renderer_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("renderer_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_error_code", sa.String(64)),
        sa.Column("render_duration_ms", sa.Integer()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["render_id"], ["render_documents.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("render_id", "attempt_number", name="uq_render_attempt_number"),
        sa.UniqueConstraint("render_id", "fencing_token", name="uq_render_attempt_fence"),
        sa.CheckConstraint(
            "state IN ('running', 'succeeded', 'failed', 'abandoned')",
            name="ck_render_attempt_state",
        ),
        sa.CheckConstraint(
            "(state = 'running' AND completed_at IS NULL AND safe_error_code IS NULL) OR "
            "(state = 'succeeded' AND completed_at IS NOT NULL AND safe_error_code IS NULL) OR "
            "(state IN ('failed', 'abandoned') AND completed_at IS NOT NULL "
            "AND safe_error_code IS NOT NULL)",
            name="ck_render_attempt_terminal",
        ),
        sa.CheckConstraint(
            "render_duration_ms IS NULL OR render_duration_ms BETWEEN 0 AND 120000",
            name="ck_render_attempt_duration",
        ),
    )
    op.create_index(
        "ix_render_attempts_render_started", "render_attempts", ["render_id", "started_at"]
    )

    bind = op.get_bind()
    documents = sa.table(
        "render_documents",
        sa.column("id", sa.String),
        sa.column("status", sa.String),
        sa.column("safe_error_code", sa.String),
        sa.column("render_duration_ms", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    attempts = sa.table(
        "render_attempts",
        sa.column("id", sa.String),
        sa.column("render_id", sa.String),
        sa.column("attempt_number", sa.Integer),
        sa.column("fencing_token", sa.BigInteger),
        sa.column("state", sa.String),
        sa.column("renderer_profile", sa.String),
        sa.column("renderer_snapshot_json", sa.JSON),
        sa.column("renderer_snapshot_hash", sa.String),
        sa.column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.column("safe_error_code", sa.String),
        sa.column("render_duration_ms", sa.Integer),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("completed_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(sa.select(documents)).mappings().all()
    snapshot_hash = "0" * 64
    if rows:
        op.bulk_insert(
            attempts,
            [
                {
                    "id": row["id"],
                    "render_id": row["id"],
                    "attempt_number": 1,
                    "fencing_token": 1,
                    "state": "abandoned" if row["status"] == "pending" else row["status"],
                    "renderer_profile": "legacy-xelatex-document-v1",
                    "renderer_snapshot_json": {"schema_version": "legacy"},
                    "renderer_snapshot_hash": snapshot_hash,
                    "lease_expires_at": (row["completed_at"] or row["created_at"])
                    + timedelta(seconds=120),
                    "safe_error_code": (
                        "legacy_pending_recovered"
                        if row["status"] == "pending"
                        else row["safe_error_code"]
                    ),
                    "render_duration_ms": row["render_duration_ms"],
                    "started_at": row["created_at"],
                    "completed_at": row["completed_at"] or row["created_at"],
                }
                for row in rows
            ],
        )

    op.add_column("render_artifacts", sa.Column("attempt_id", sa.String(36)))
    op.execute("UPDATE render_artifacts SET attempt_id = render_id")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "render_artifacts",
            recreate="always",
            naming_convention={"uq": "uq_%(table_name)s_%(column_0_name)s"},
        ) as batch:
            batch.drop_constraint("uq_render_artifacts_render_id", type_="unique")
            batch.alter_column("attempt_id", existing_type=sa.String(36), nullable=False)
            batch.create_foreign_key(
                "fk_render_artifacts_attempt_id",
                "render_attempts",
                ["attempt_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.create_unique_constraint("uq_render_artifacts_attempt_id", ["attempt_id"])
    else:
        op.drop_constraint("render_artifacts_render_id_key", "render_artifacts", type_="unique")
        op.alter_column("render_artifacts", "attempt_id", existing_type=sa.String(36), nullable=False)
        op.create_foreign_key(
            "fk_render_artifacts_attempt_id",
            "render_artifacts",
            "render_attempts",
            ["attempt_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_unique_constraint(
            "uq_render_artifacts_attempt_id", "render_artifacts", ["attempt_id"]
        )
    op.create_index(
        "ix_render_artifacts_render_created", "render_artifacts", ["render_id", "created_at"]
    )

    op.create_table(
        "render_delivery_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("artifact_id", sa.String(36), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("conversation_key", sa.String(320), nullable=False),
        sa.Column("capability_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("capability_hash", sa.String(64), nullable=False),
        sa.Column("selected_family", sa.String(32), nullable=False),
        sa.Column("fallback_text", sa.Text()),
        sa.Column("degradation_reasons_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["render_artifacts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instance_id"], ["bot_instances.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "artifact_id", "capability_hash", name="uq_render_delivery_plan_capability"
        ),
        sa.CheckConstraint(
            "selected_family IN ('image', 'text')", name="ck_render_delivery_plan_family"
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_render_delivery_plan_expiry"),
    )
    op.create_index(
        "ix_render_delivery_plan_instance_created",
        "render_delivery_plans",
        ["instance_id", "created_at"],
    )
    op.create_table(
        "render_delivery_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("platform_message_id", sa.String(512)),
        sa.Column("safe_error_code", sa.String(64)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["plan_id"], ["render_delivery_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["instance_id"], ["bot_instances.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("instance_id", "idempotency_key", name="uq_render_delivery_intent_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'succeeded', 'failed', 'ambiguous')",
            name="ck_render_delivery_intent_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND completed_at IS NULL AND platform_message_id IS NULL "
            "AND safe_error_code IS NULL) OR "
            "(status = 'succeeded' AND completed_at IS NOT NULL "
            "AND platform_message_id IS NOT NULL AND safe_error_code IS NULL) OR "
            "(status IN ('failed', 'ambiguous') AND completed_at IS NOT NULL "
            "AND safe_error_code IS NOT NULL)",
            name="ck_render_delivery_intent_terminal",
        ),
    )
    op.create_index(
        "ix_render_delivery_intent_deadline",
        "render_delivery_intents",
        ["status", "deadline_at"],
    )
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS render_delivery_attempts_no_update")
        op.execute("DROP TRIGGER IF EXISTS render_delivery_attempts_no_delete")
        with op.batch_alter_table("render_delivery_attempts", recreate="always") as batch:
            batch.add_column(sa.Column("plan_id", sa.String(36)))
            batch.add_column(sa.Column("intent_id", sa.String(36)))
            batch.create_foreign_key(
                "fk_render_delivery_attempts_plan_id",
                "render_delivery_plans",
                ["plan_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.create_foreign_key(
                "fk_render_delivery_attempts_intent_id",
                "render_delivery_intents",
                ["intent_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch.create_unique_constraint("uq_render_delivery_attempt_intent", ["intent_id"])
        for operation in ("UPDATE", "DELETE"):
            suffix = operation.lower()
            op.execute(
                f"CREATE TRIGGER render_delivery_attempts_no_{suffix} "
                f"BEFORE {operation} ON render_delivery_attempts BEGIN "
                "SELECT RAISE(ABORT, 'render delivery evidence is append-only'); END"
            )
    else:
        op.add_column("render_delivery_attempts", sa.Column("plan_id", sa.String(36)))
        op.add_column("render_delivery_attempts", sa.Column("intent_id", sa.String(36)))
        op.create_foreign_key(
            "fk_render_delivery_attempts_plan_id",
            "render_delivery_attempts",
            "render_delivery_plans",
            ["plan_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_render_delivery_attempts_intent_id",
            "render_delivery_attempts",
            "render_delivery_intents",
            ["intent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_unique_constraint(
            "uq_render_delivery_attempt_intent", "render_delivery_attempts", ["intent_id"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS render_delivery_attempts_no_update")
        op.execute("DROP TRIGGER IF EXISTS render_delivery_attempts_no_delete")
        with op.batch_alter_table("render_delivery_attempts", recreate="always") as batch:
            batch.drop_constraint("uq_render_delivery_attempt_intent", type_="unique")
            batch.drop_constraint("fk_render_delivery_attempts_intent_id", type_="foreignkey")
            batch.drop_constraint("fk_render_delivery_attempts_plan_id", type_="foreignkey")
            batch.drop_column("intent_id")
            batch.drop_column("plan_id")
        for operation in ("UPDATE", "DELETE"):
            suffix = operation.lower()
            op.execute(
                f"CREATE TRIGGER render_delivery_attempts_no_{suffix} "
                f"BEFORE {operation} ON render_delivery_attempts BEGIN "
                "SELECT RAISE(ABORT, 'render delivery evidence is append-only'); END"
            )
    else:
        op.drop_constraint(
            "uq_render_delivery_attempt_intent", "render_delivery_attempts", type_="unique"
        )
        op.drop_constraint(
            "fk_render_delivery_attempts_intent_id",
            "render_delivery_attempts",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_render_delivery_attempts_plan_id",
            "render_delivery_attempts",
            type_="foreignkey",
        )
        op.drop_column("render_delivery_attempts", "intent_id")
        op.drop_column("render_delivery_attempts", "plan_id")
    op.drop_index("ix_render_delivery_intent_deadline", table_name="render_delivery_intents")
    op.drop_table("render_delivery_intents")
    op.drop_index(
        "ix_render_delivery_plan_instance_created", table_name="render_delivery_plans"
    )
    op.drop_table("render_delivery_plans")
    op.drop_index("ix_render_artifacts_render_created", table_name="render_artifacts")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("render_artifacts", recreate="always") as batch:
            batch.create_unique_constraint("uq_render_artifacts_render_id", ["render_id"])
            batch.drop_column("attempt_id")
    else:
        op.drop_constraint("uq_render_artifacts_attempt_id", "render_artifacts", type_="unique")
        op.drop_constraint("fk_render_artifacts_attempt_id", "render_artifacts", type_="foreignkey")
        op.drop_column("render_artifacts", "attempt_id")
        op.create_unique_constraint(
            "render_artifacts_render_id_key", "render_artifacts", ["render_id"]
        )
    op.drop_index("ix_render_attempts_render_started", table_name="render_attempts")
    op.drop_table("render_attempts")
