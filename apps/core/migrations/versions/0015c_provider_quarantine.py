"""增加 Provider quarantine 的资源版本与不可变证据。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015c_provider_quarantine"
down_revision: str | None = "0015b_descriptor_mutations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tool_providers",
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.execute(
        """
        UPDATE tool_providers
        SET resource_version = COALESCE(
            (SELECT MAX(sequence) FROM tool_provider_lifecycle_events
             WHERE provider_id = tool_providers.id),
            1
        )
        """
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER tool_provider_lifecycle_events_no_update
            BEFORE UPDATE ON tool_provider_lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'provider lifecycle evidence is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_provider_lifecycle_events_no_delete
            BEFORE DELETE ON tool_provider_lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'provider lifecycle evidence is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_providers_authority_no_update
            BEFORE UPDATE OF id, owner, allowed_protocols_json, tool_selectors_json, created_at
            ON tool_providers
            BEGIN
                SELECT RAISE(ABORT, 'provider authority is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_providers_no_delete
            BEFORE DELETE ON tool_providers
            BEGIN
                SELECT RAISE(ABORT, 'provider authority is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_providers_lifecycle_guard
            BEFORE UPDATE OF lifecycle, resource_version ON tool_providers
            WHEN NEW.lifecycle != OLD.lifecycle OR NEW.resource_version != OLD.resource_version
            BEGIN
                SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
                    THEN RAISE(ABORT, 'provider resource version must increase by one') END;
                SELECT CASE WHEN NOT (
                    (OLD.lifecycle = 'active' AND NEW.lifecycle = 'quarantined') OR
                    (OLD.lifecycle = 'quarantined' AND NEW.lifecycle = 'active')
                ) THEN RAISE(ABORT, 'provider lifecycle transition is not allowed') END;
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM tool_provider_lifecycle_events
                    WHERE provider_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_lifecycle = OLD.lifecycle
                      AND lifecycle = NEW.lifecycle
                ) THEN RAISE(ABORT, 'provider lifecycle event is required') END;
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_provider_lifecycle_evidence_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'provider lifecycle evidence is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_provider_lifecycle_events_no_mutation
            BEFORE UPDATE OR DELETE ON tool_provider_lifecycle_events
            FOR EACH ROW EXECUTE FUNCTION reject_provider_lifecycle_evidence_mutation()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_tool_provider_authority_mutation()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'provider authority is immutable';
                END IF;
                IF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.owner IS DISTINCT FROM OLD.owner
                   OR NEW.allowed_protocols_json::text IS DISTINCT FROM OLD.allowed_protocols_json::text
                   OR NEW.tool_selectors_json::text IS DISTINCT FROM OLD.tool_selectors_json::text
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'provider authority is immutable';
                END IF;
                IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
                   OR NEW.resource_version IS DISTINCT FROM OLD.resource_version THEN
                    IF NEW.resource_version != OLD.resource_version + 1 THEN
                        RAISE EXCEPTION 'provider resource version must increase by one';
                    END IF;
                    IF NOT (
                        (OLD.lifecycle = 'active' AND NEW.lifecycle = 'quarantined') OR
                        (OLD.lifecycle = 'quarantined' AND NEW.lifecycle = 'active')
                    ) THEN
                        RAISE EXCEPTION 'provider lifecycle transition is not allowed';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM tool_provider_lifecycle_events
                        WHERE provider_id = OLD.id
                          AND sequence = NEW.resource_version
                          AND previous_lifecycle = OLD.lifecycle
                          AND lifecycle = NEW.lifecycle
                    ) THEN
                        RAISE EXCEPTION 'provider lifecycle event is required';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_providers_authority_guard
            BEFORE UPDATE OR DELETE ON tool_providers
            FOR EACH ROW EXECUTE FUNCTION guard_tool_provider_authority_mutation()
            """
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS tool_providers_lifecycle_guard")
        op.execute("DROP TRIGGER IF EXISTS tool_providers_no_delete")
        op.execute("DROP TRIGGER IF EXISTS tool_providers_authority_no_update")
        op.execute("DROP TRIGGER IF EXISTS tool_provider_lifecycle_events_no_delete")
        op.execute("DROP TRIGGER IF EXISTS tool_provider_lifecycle_events_no_update")
    elif dialect == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS guard_tool_provider_authority_mutation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS reject_provider_lifecycle_evidence_mutation() CASCADE")
    op.drop_column("tool_providers", "resource_version")
