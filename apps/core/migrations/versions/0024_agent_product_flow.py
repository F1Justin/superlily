"""Add the exact-conversation Agent product and native-text delivery ledgers."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0024_agent_product_flow"
down_revision: str | None = "0023_agent_model_routes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_interactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instance_id", sa.String(128), sa.ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_event_id", sa.String(512), sa.ForeignKey("source_events.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id", ondelete="RESTRICT")),
        sa.Column("loop_id", sa.String(36), sa.ForeignKey("agent_tool_loops.id", ondelete="RESTRICT")),
        sa.Column("conversation_key", sa.String(512), nullable=False),
        sa.Column("conversation_type", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(256), nullable=False),
        sa.Column("reply_to_platform_message_id", sa.String(512)),
        sa.Column("trigger_kind", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("instance_id", "source_event_id", name="uq_agent_interaction_source"),
        sa.UniqueConstraint("run_id", name="uq_agent_interaction_run"),
        sa.UniqueConstraint("loop_id", name="uq_agent_interaction_loop"),
        sa.CheckConstraint("conversation_type IN ('group', 'private')", name="ck_agent_interaction_conversation_type"),
        sa.CheckConstraint("trigger_kind IN ('mention', 'reply', 'explicit')", name="ck_agent_interaction_trigger_kind"),
        sa.CheckConstraint(
            "state IN ('accepted', 'planning', 'tool_pending', 'continuing', "
            "'delivery_pending', 'succeeded', 'failed', 'ambiguous', 'expired')",
            name="ck_agent_interaction_state",
        ),
        sa.CheckConstraint("resource_version >= 1", name="ck_agent_interaction_version"),
        sa.CheckConstraint(
            "((state IN ('succeeded', 'failed', 'ambiguous', 'expired') AND terminal_at IS NOT NULL) OR "
            "(state NOT IN ('succeeded', 'failed', 'ambiguous', 'expired') AND terminal_at IS NULL))",
            name="ck_agent_interaction_terminal",
        ),
    )
    op.create_index("ix_agent_interactions_state_updated", "agent_interactions", ["state", "updated_at"])
    op.create_index("ix_agent_interactions_conversation_created", "agent_interactions", ["conversation_key", "created_at"])
    op.create_table(
        "agent_interaction_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("interaction_id", sa.String(36), sa.ForeignKey("agent_interactions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.String(32)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("interaction_id", "sequence", name="uq_agent_interaction_event_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_interaction_event_sequence"),
    )
    op.create_index("ix_agent_interaction_events_created", "agent_interaction_events", ["interaction_id", "created_at"])
    op.create_table(
        "agent_text_delivery_intents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("interaction_id", sa.String(36), sa.ForeignKey("agent_interactions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("instance_id", sa.String(128), sa.ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("conversation_key", sa.String(512), nullable=False),
        sa.Column("conversation_type", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(256), nullable=False),
        sa.Column("reply_to_platform_message_id", sa.String(512)),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("lease_token_hash", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("platform_message_id", sa.String(512)),
        sa.Column("safe_error_code", sa.String(128)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("interaction_id", name="uq_agent_text_delivery_interaction"),
        sa.CheckConstraint("conversation_type IN ('group', 'private')", name="ck_agent_text_delivery_conversation_type"),
        sa.CheckConstraint("state IN ('pending', 'leased', 'succeeded', 'failed', 'ambiguous', 'expired')", name="ck_agent_text_delivery_state"),
        sa.CheckConstraint("fence >= 0", name="ck_agent_text_delivery_fence"),
        sa.CheckConstraint(
            "((state = 'pending' AND fence = 0 AND lease_token_hash IS NULL AND lease_expires_at IS NULL AND terminal_at IS NULL) OR "
            "(state = 'leased' AND fence >= 1 AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL AND terminal_at IS NULL) OR "
            "(state IN ('succeeded', 'failed', 'ambiguous', 'expired') AND terminal_at IS NOT NULL))",
            name="ck_agent_text_delivery_lifecycle",
        ),
    )
    op.create_index("ix_agent_text_deliveries_lease", "agent_text_delivery_intents", ["instance_id", "state", "created_at"])
    op.create_table(
        "agent_text_delivery_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("intent_id", sa.String(36), sa.ForeignKey("agent_text_delivery_intents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("previous_state", sa.String(32)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(128), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("intent_id", "sequence", name="uq_agent_text_delivery_event_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_agent_text_delivery_event_sequence"),
    )
    op.create_index("ix_agent_text_delivery_events_created", "agent_text_delivery_events", ["intent_id", "created_at"])

    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        for table in ("agent_interaction_events", "agent_text_delivery_events"):
            op.execute(
                f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'agent product evidence is append-only'); END"
            )
            op.execute(
                f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'agent product evidence is append-only'); END"
            )
        op.execute(
            """
            CREATE TRIGGER agent_interactions_authority_no_update
            BEFORE UPDATE OF instance_id, source_event_id, conversation_key,
                conversation_type, conversation_id, reply_to_platform_message_id,
                trigger_kind, deadline_at, created_at
            ON agent_interactions
            BEGIN SELECT RAISE(ABORT, 'agent interaction authority is immutable'); END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_interactions_no_delete
            BEFORE DELETE ON agent_interactions
            BEGIN SELECT RAISE(ABORT, 'agent interaction evidence cannot be deleted'); END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_interactions_state_guard
            BEFORE UPDATE OF run_id, loop_id, state, resource_version, reason_code,
                terminal_at, updated_at
            ON agent_interactions
            BEGIN
                SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
                    THEN RAISE(ABORT, 'agent interaction version must increase by one') END;
                SELECT CASE WHEN NOT (
                    (OLD.state = 'accepted' AND NEW.state IN ('planning', 'failed', 'expired')) OR
                    (OLD.state = 'planning' AND NEW.state IN (
                        'tool_pending', 'delivery_pending', 'failed', 'expired'
                    )) OR
                    (OLD.state = 'tool_pending' AND NEW.state IN (
                        'continuing', 'delivery_pending', 'failed', 'expired'
                    )) OR
                    (OLD.state = 'continuing' AND NEW.state IN (
                        'delivery_pending', 'failed', 'expired'
                    )) OR
                    (OLD.state = 'delivery_pending' AND NEW.state IN (
                        'succeeded', 'failed', 'ambiguous', 'expired'
                    ))
                ) THEN RAISE(ABORT, 'agent interaction transition is not allowed') END;
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM agent_interaction_events
                    WHERE interaction_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_state = OLD.state
                      AND state = NEW.state
                ) THEN RAISE(ABORT, 'agent interaction event is required') END;
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_text_delivery_intents_authority_no_update
            BEFORE UPDATE OF interaction_id, instance_id, conversation_key,
                conversation_type, conversation_id, reply_to_platform_message_id,
                content_text, content_sha256, deadline_at, created_at
            ON agent_text_delivery_intents
            BEGIN SELECT RAISE(ABORT, 'agent text delivery authority is immutable'); END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_text_delivery_intents_no_delete
            BEFORE DELETE ON agent_text_delivery_intents
            BEGIN SELECT RAISE(ABORT, 'agent text delivery evidence cannot be deleted'); END
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_text_delivery_intents_state_guard
            BEFORE UPDATE OF state, fence, lease_token_hash, lease_expires_at,
                platform_message_id, safe_error_code, terminal_at, updated_at
            ON agent_text_delivery_intents
            BEGIN
                SELECT CASE WHEN NOT (
                    (OLD.state = 'pending' AND NEW.state = 'leased'
                     AND OLD.fence = 0 AND NEW.fence = 1) OR
                    (OLD.state = 'pending' AND NEW.state = 'expired'
                     AND OLD.fence = 0 AND NEW.fence = 0) OR
                    (OLD.state = 'leased' AND NEW.state IN (
                        'succeeded', 'failed', 'ambiguous'
                     ) AND NEW.fence = OLD.fence)
                ) THEN RAISE(ABORT, 'agent text delivery transition is not allowed') END;
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM agent_text_delivery_events
                    WHERE intent_id = OLD.id
                      AND sequence = CASE WHEN OLD.state = 'pending' THEN 2 ELSE 3 END
                      AND previous_state = OLD.state
                      AND state = NEW.state
                ) THEN RAISE(ABORT, 'agent text delivery event is required') END;
            END
            """
        )
    else:
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_agent_product_evidence_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'agent product evidence is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in ("agent_interaction_events", "agent_text_delivery_events"):
            op.execute(
                f"CREATE TRIGGER {table}_no_mutation BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_agent_product_evidence_mutation()"
            )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_agent_interaction_mutation()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'agent interaction evidence cannot be deleted';
                END IF;
                IF NEW.instance_id IS DISTINCT FROM OLD.instance_id
                   OR NEW.source_event_id IS DISTINCT FROM OLD.source_event_id
                   OR NEW.conversation_key IS DISTINCT FROM OLD.conversation_key
                   OR NEW.conversation_type IS DISTINCT FROM OLD.conversation_type
                   OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
                   OR NEW.reply_to_platform_message_id IS DISTINCT FROM OLD.reply_to_platform_message_id
                   OR NEW.trigger_kind IS DISTINCT FROM OLD.trigger_kind
                   OR NEW.deadline_at IS DISTINCT FROM OLD.deadline_at
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'agent interaction authority is immutable';
                END IF;
                IF NEW.resource_version != OLD.resource_version + 1 THEN
                    RAISE EXCEPTION 'agent interaction version must increase by one';
                END IF;
                IF NOT (
                    (OLD.state = 'accepted' AND NEW.state IN ('planning', 'failed', 'expired')) OR
                    (OLD.state = 'planning' AND NEW.state IN (
                        'tool_pending', 'delivery_pending', 'failed', 'expired'
                    )) OR
                    (OLD.state = 'tool_pending' AND NEW.state IN (
                        'continuing', 'delivery_pending', 'failed', 'expired'
                    )) OR
                    (OLD.state = 'continuing' AND NEW.state IN (
                        'delivery_pending', 'failed', 'expired'
                    )) OR
                    (OLD.state = 'delivery_pending' AND NEW.state IN (
                        'succeeded', 'failed', 'ambiguous', 'expired'
                    ))
                ) THEN RAISE EXCEPTION 'agent interaction transition is not allowed';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM agent_interaction_events
                    WHERE interaction_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_state = OLD.state
                      AND state = NEW.state
                ) THEN RAISE EXCEPTION 'agent interaction event is required';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_interactions_guard
            BEFORE UPDATE OR DELETE ON agent_interactions
            FOR EACH ROW EXECUTE FUNCTION guard_agent_interaction_mutation()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_agent_text_delivery_mutation()
            RETURNS trigger AS $$
            DECLARE expected_sequence integer;
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'agent text delivery evidence cannot be deleted';
                END IF;
                IF NEW.interaction_id IS DISTINCT FROM OLD.interaction_id
                   OR NEW.instance_id IS DISTINCT FROM OLD.instance_id
                   OR NEW.conversation_key IS DISTINCT FROM OLD.conversation_key
                   OR NEW.conversation_type IS DISTINCT FROM OLD.conversation_type
                   OR NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
                   OR NEW.reply_to_platform_message_id IS DISTINCT FROM OLD.reply_to_platform_message_id
                   OR NEW.content_text IS DISTINCT FROM OLD.content_text
                   OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
                   OR NEW.deadline_at IS DISTINCT FROM OLD.deadline_at
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'agent text delivery authority is immutable';
                END IF;
                IF NOT (
                    (OLD.state = 'pending' AND NEW.state = 'leased'
                     AND OLD.fence = 0 AND NEW.fence = 1) OR
                    (OLD.state = 'pending' AND NEW.state = 'expired'
                     AND OLD.fence = 0 AND NEW.fence = 0) OR
                    (OLD.state = 'leased' AND NEW.state IN (
                        'succeeded', 'failed', 'ambiguous'
                     ) AND NEW.fence = OLD.fence)
                ) THEN RAISE EXCEPTION 'agent text delivery transition is not allowed';
                END IF;
                expected_sequence := CASE WHEN OLD.state = 'pending' THEN 2 ELSE 3 END;
                IF NOT EXISTS (
                    SELECT 1 FROM agent_text_delivery_events
                    WHERE intent_id = OLD.id
                      AND sequence = expected_sequence
                      AND previous_state = OLD.state
                      AND state = NEW.state
                ) THEN RAISE EXCEPTION 'agent text delivery event is required';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER agent_text_delivery_intents_guard
            BEFORE UPDATE OR DELETE ON agent_text_delivery_intents
            FOR EACH ROW EXECUTE FUNCTION guard_agent_text_delivery_mutation()
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS guard_agent_text_delivery_mutation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS guard_agent_interaction_mutation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS reject_agent_product_evidence_mutation() CASCADE")
    op.drop_index("ix_agent_text_delivery_events_created", table_name="agent_text_delivery_events")
    op.drop_table("agent_text_delivery_events")
    op.drop_index("ix_agent_text_deliveries_lease", table_name="agent_text_delivery_intents")
    op.drop_table("agent_text_delivery_intents")
    op.drop_index("ix_agent_interaction_events_created", table_name="agent_interaction_events")
    op.drop_table("agent_interaction_events")
    op.drop_index("ix_agent_interactions_conversation_created", table_name="agent_interactions")
    op.drop_index("ix_agent_interactions_state_updated", table_name="agent_interactions")
    op.drop_table("agent_interactions")
