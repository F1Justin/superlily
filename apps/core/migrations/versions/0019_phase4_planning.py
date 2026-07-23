"""Persist Phase 4 capability decisions and render artifact lifecycle metadata."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0019_phase4_planning"
down_revision: str | None = "0018_render_attempt_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ZERO_HASH = "0" * 64


def _create_artifact_checks() -> None:
    op.create_check_constraint(
        "ck_render_artifact_retention",
        "render_artifacts",
        "retention_until >= expires_at",
    )
    op.create_check_constraint(
        "ck_render_artifact_classification",
        "render_artifacts",
        "data_classification IN ('public', 'conversation', 'sensitive', 'administrative')",
    )
    op.create_check_constraint(
        "ck_render_artifact_deletion",
        "render_artifacts",
        "(content_deleted_at IS NULL AND deletion_reason IS NULL) OR "
        "(content_deleted_at IS NOT NULL AND deletion_reason IS NOT NULL)",
    )


def upgrade() -> None:
    op.add_column(
        "render_artifacts",
        sa.Column(
            "producer_kind",
            sa.String(32),
            nullable=False,
            server_default="document_renderer",
        ),
    )
    op.add_column(
        "render_artifacts",
        sa.Column(
            "producer_id",
            sa.String(128),
            nullable=False,
            server_default="legacy-xelatex-document-v1",
        ),
    )
    op.add_column(
        "render_artifacts", sa.Column("source_invocation_id", sa.String(36))
    )
    op.add_column(
        "render_artifacts",
        sa.Column(
            "data_classification",
            sa.String(32),
            nullable=False,
            server_default="conversation",
        ),
    )
    op.add_column(
        "render_artifacts",
        sa.Column(
            "canonical_scope",
            sa.String(512),
            nullable=False,
            server_default="legacy:unknown",
        ),
    )
    op.add_column(
        "render_artifacts",
        sa.Column(
            "safe_filename",
            sa.String(255),
            nullable=False,
            server_default="rendered-document.png",
        ),
    )
    op.add_column(
        "render_artifacts",
        sa.Column(
            "accessibility_text",
            sa.Text(),
            nullable=False,
            server_default="rendered document",
        ),
    )
    op.add_column(
        "render_artifacts",
        sa.Column(
            "retention_until",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.add_column(
        "render_artifacts",
        sa.Column("content_deleted_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "render_artifacts", sa.Column("deletion_reason", sa.String(64))
    )
    op.execute("UPDATE render_artifacts SET retention_until = expires_at")

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("render_artifacts", recreate="always") as batch:
            batch.create_check_constraint(
                "ck_render_artifact_retention", "retention_until >= expires_at"
            )
            batch.create_check_constraint(
                "ck_render_artifact_classification",
                "data_classification IN "
                "('public', 'conversation', 'sensitive', 'administrative')",
            )
            batch.create_check_constraint(
                "ck_render_artifact_deletion",
                "(content_deleted_at IS NULL AND deletion_reason IS NULL) OR "
                "(content_deleted_at IS NOT NULL AND deletion_reason IS NOT NULL)",
            )
    else:
        _create_artifact_checks()
    op.create_index(
        "ix_render_artifacts_retention",
        "render_artifacts",
        ["content_deleted_at", "retention_until"],
    )

    for name, column in (
        (
            "decision_hash",
            sa.Column(
                "decision_hash",
                sa.String(64),
                nullable=False,
                server_default=_ZERO_HASH,
            ),
        ),
        (
            "resolved_document_hash",
            sa.Column(
                "resolved_document_hash",
                sa.String(64),
                nullable=False,
                server_default=_ZERO_HASH,
            ),
        ),
        (
            "selected_alternatives_json",
            sa.Column(
                "selected_alternatives_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            ),
        ),
        (
            "rejected_alternatives_json",
            sa.Column(
                "rejected_alternatives_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            ),
        ),
        (
            "ordered_payloads_json",
            sa.Column(
                "ordered_payloads_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            ),
        ),
    ):
        del name
        op.add_column("render_delivery_plans", column)

    plans = sa.table(
        "render_delivery_plans",
        sa.column("id", sa.String),
        sa.column("selected_family", sa.String),
        sa.column("conversation_key", sa.String),
        sa.column("capability_hash", sa.String),
        sa.column("ordered_payloads_json", sa.JSON),
    )
    for row in bind.execute(
        sa.select(plans.c.id, plans.c.selected_family)
    ).mappings():
        source = (
            "render_artifact"
            if row["selected_family"] == "image"
            else "fallback_text"
        )
        bind.execute(
            plans.update()
            .where(plans.c.id == row["id"])
            .values(
                ordered_payloads_json=[
                    {
                        "position": 0,
                        "family": row["selected_family"],
                        "source": source,
                        "content_sha256": None,
                    }
                ]
            )
        )

    op.add_column(
        "render_delivery_intents",
        sa.Column(
            "conversation_key",
            sa.String(320),
            nullable=False,
            server_default="legacy:unknown",
        ),
    )
    op.add_column(
        "render_delivery_intents",
        sa.Column(
            "capability_hash",
            sa.String(64),
            nullable=False,
            server_default=_ZERO_HASH,
        ),
    )
    op.add_column(
        "render_delivery_intents",
        sa.Column(
            "ordered_payloads_json",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "render_delivery_intents",
        sa.Column("reply_to_platform_message_id", sa.String(512)),
    )
    op.add_column(
        "render_delivery_intents",
        sa.Column(
            "mention_ids_json",
            sa.JSON(),
            nullable=False,
            server_default="[]",
        ),
    )
    intents = sa.table(
        "render_delivery_intents",
        sa.column("id", sa.String),
        sa.column("plan_id", sa.String),
        sa.column("conversation_key", sa.String),
        sa.column("capability_hash", sa.String),
        sa.column("ordered_payloads_json", sa.JSON),
    )
    plan_rows = {
        row["id"]: row
        for row in bind.execute(
            sa.select(
                plans.c.id,
                plans.c.conversation_key,
                plans.c.capability_hash,
                plans.c.ordered_payloads_json,
            )
        ).mappings()
    }
    for row in bind.execute(sa.select(intents.c.id, intents.c.plan_id)).mappings():
        plan = plan_rows.get(row["plan_id"])
        if plan is None:
            continue
        bind.execute(
            intents.update()
            .where(intents.c.id == row["id"])
            .values(
                conversation_key=plan["conversation_key"],
                capability_hash=plan["capability_hash"],
                ordered_payloads_json=plan["ordered_payloads_json"],
            )
        )


def downgrade() -> None:
    for column in (
        "mention_ids_json",
        "reply_to_platform_message_id",
        "ordered_payloads_json",
        "capability_hash",
        "conversation_key",
    ):
        op.drop_column("render_delivery_intents", column)
    for column in (
        "ordered_payloads_json",
        "rejected_alternatives_json",
        "selected_alternatives_json",
        "resolved_document_hash",
        "decision_hash",
    ):
        op.drop_column("render_delivery_plans", column)

    op.drop_index("ix_render_artifacts_retention", table_name="render_artifacts")
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("render_artifacts", recreate="always") as batch:
            batch.drop_constraint("ck_render_artifact_deletion", type_="check")
            batch.drop_constraint("ck_render_artifact_classification", type_="check")
            batch.drop_constraint("ck_render_artifact_retention", type_="check")
    else:
        for name in (
            "ck_render_artifact_deletion",
            "ck_render_artifact_classification",
            "ck_render_artifact_retention",
        ):
            op.drop_constraint(name, "render_artifacts", type_="check")
    for column in (
        "deletion_reason",
        "content_deleted_at",
        "retention_until",
        "accessibility_text",
        "safe_filename",
        "canonical_scope",
        "data_classification",
        "source_invocation_id",
        "producer_id",
        "producer_kind",
    ):
        op.drop_column("render_artifacts", column)
