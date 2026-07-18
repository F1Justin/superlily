"""增加 descriptor mutation 的资源版本、持久 preview 与不可变证据。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015b_descriptor_mutations"
down_revision: str | None = "0015a_control_plane_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ROLES = "'auditor', 'operator', 'reviewer', 'security_admin', 'break_glass'"


def upgrade() -> None:
    op.add_column(
        "tool_descriptors",
        sa.Column("resource_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.execute(
        """
        UPDATE tool_descriptors
        SET resource_version = COALESCE(
            (SELECT MAX(sequence) FROM tool_descriptor_lifecycle_events
             WHERE descriptor_id = tool_descriptors.id),
            1
        )
        """
    )

    op.create_table(
        "control_plane_previews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=512), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("preview_json", sa.JSON(), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(f"role IN ({_ROLES})", name="ck_control_preview_role"),
        sa.CheckConstraint("expected_version >= 1", name="ck_control_preview_version"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["control_plane_sessions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_control_previews_target_created",
        "control_plane_previews",
        ["target_type", "target_id", "created_at"],
    )
    op.create_index(
        "ix_control_previews_session_expiry",
        "control_plane_previews",
        ["session_id", "expires_at"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER control_plane_previews_no_update
            BEFORE UPDATE ON control_plane_previews
            BEGIN
                SELECT RAISE(ABORT, 'control plane evidence is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER control_plane_previews_no_delete
            BEFORE DELETE ON control_plane_previews
            BEGIN
                SELECT RAISE(ABORT, 'control plane evidence is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_descriptor_lifecycle_events_no_update
            BEFORE UPDATE ON tool_descriptor_lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'descriptor lifecycle evidence is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_descriptor_lifecycle_events_no_delete
            BEFORE DELETE ON tool_descriptor_lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'descriptor lifecycle evidence is append-only');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_descriptors_authority_no_update
            BEFORE UPDATE OF tool_id, version, descriptor_hash, schema_profile, source_plugin,
                review_status, source_commit, bundle_hash, reviewer, canonical_json,
                descriptor_json, import_outcome, imported_at
            ON tool_descriptors
            BEGIN
                SELECT RAISE(ABORT, 'descriptor authority is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_descriptors_no_delete
            BEFORE DELETE ON tool_descriptors
            BEGIN
                SELECT RAISE(ABORT, 'descriptor authority is immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_descriptors_lifecycle_guard
            BEFORE UPDATE OF lifecycle, resource_version ON tool_descriptors
            WHEN NEW.lifecycle != OLD.lifecycle OR NEW.resource_version != OLD.resource_version
            BEGIN
                SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
                    THEN RAISE(ABORT, 'descriptor resource version must increase by one') END;
                SELECT CASE WHEN NOT (
                    (OLD.lifecycle = 'reviewed' AND NEW.lifecycle = 'active') OR
                    (OLD.lifecycle = 'active' AND NEW.lifecycle = 'suspended') OR
                    (OLD.lifecycle = 'suspended' AND NEW.lifecycle = 'active')
                ) THEN RAISE(ABORT, 'descriptor lifecycle transition is not allowed') END;
                SELECT CASE WHEN NOT EXISTS (
                    SELECT 1 FROM tool_descriptor_lifecycle_events
                    WHERE descriptor_id = OLD.id
                      AND sequence = NEW.resource_version
                      AND previous_lifecycle = OLD.lifecycle
                      AND lifecycle = NEW.lifecycle
                ) THEN RAISE(ABORT, 'descriptor lifecycle event is required') END;
            END
            """
        )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE TRIGGER control_plane_previews_no_mutation
            BEFORE UPDATE OR DELETE ON control_plane_previews
            FOR EACH ROW EXECUTE FUNCTION reject_control_plane_evidence_mutation()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_descriptor_lifecycle_evidence_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'descriptor lifecycle evidence is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_descriptor_lifecycle_events_no_mutation
            BEFORE UPDATE OR DELETE ON tool_descriptor_lifecycle_events
            FOR EACH ROW EXECUTE FUNCTION reject_descriptor_lifecycle_evidence_mutation()
            """
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION guard_tool_descriptor_authority_mutation()
            RETURNS trigger AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'descriptor authority is immutable';
                END IF;
                IF NEW.tool_id IS DISTINCT FROM OLD.tool_id
                   OR NEW.version IS DISTINCT FROM OLD.version
                   OR NEW.descriptor_hash IS DISTINCT FROM OLD.descriptor_hash
                   OR NEW.schema_profile IS DISTINCT FROM OLD.schema_profile
                   OR NEW.source_plugin IS DISTINCT FROM OLD.source_plugin
                   OR NEW.review_status IS DISTINCT FROM OLD.review_status
                   OR NEW.source_commit IS DISTINCT FROM OLD.source_commit
                   OR NEW.bundle_hash IS DISTINCT FROM OLD.bundle_hash
                   OR NEW.reviewer IS DISTINCT FROM OLD.reviewer
                   OR NEW.canonical_json IS DISTINCT FROM OLD.canonical_json
                   OR NEW.descriptor_json::text IS DISTINCT FROM OLD.descriptor_json::text
                   OR NEW.import_outcome IS DISTINCT FROM OLD.import_outcome
                   OR NEW.imported_at IS DISTINCT FROM OLD.imported_at THEN
                    RAISE EXCEPTION 'descriptor authority is immutable';
                END IF;
                IF NEW.lifecycle IS DISTINCT FROM OLD.lifecycle
                   OR NEW.resource_version IS DISTINCT FROM OLD.resource_version THEN
                    IF NEW.resource_version != OLD.resource_version + 1 THEN
                        RAISE EXCEPTION 'descriptor resource version must increase by one';
                    END IF;
                    IF NOT (
                        (OLD.lifecycle = 'reviewed' AND NEW.lifecycle = 'active') OR
                        (OLD.lifecycle = 'active' AND NEW.lifecycle = 'suspended') OR
                        (OLD.lifecycle = 'suspended' AND NEW.lifecycle = 'active')
                    ) THEN
                        RAISE EXCEPTION 'descriptor lifecycle transition is not allowed';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM tool_descriptor_lifecycle_events
                        WHERE descriptor_id = OLD.id
                          AND sequence = NEW.resource_version
                          AND previous_lifecycle = OLD.lifecycle
                          AND lifecycle = NEW.lifecycle
                    ) THEN
                        RAISE EXCEPTION 'descriptor lifecycle event is required';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER tool_descriptors_authority_guard
            BEFORE UPDATE OR DELETE ON tool_descriptors
            FOR EACH ROW EXECUTE FUNCTION guard_tool_descriptor_authority_mutation()
            """
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS tool_descriptors_lifecycle_guard")
        op.execute("DROP TRIGGER IF EXISTS tool_descriptors_no_delete")
        op.execute("DROP TRIGGER IF EXISTS tool_descriptors_authority_no_update")
        op.execute("DROP TRIGGER IF EXISTS tool_descriptor_lifecycle_events_no_delete")
        op.execute("DROP TRIGGER IF EXISTS tool_descriptor_lifecycle_events_no_update")
    elif dialect == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS guard_tool_descriptor_authority_mutation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS reject_descriptor_lifecycle_evidence_mutation() CASCADE")
    op.drop_index("ix_control_previews_session_expiry", table_name="control_plane_previews")
    op.drop_index("ix_control_previews_target_created", table_name="control_plane_previews")
    op.drop_table("control_plane_previews")
    op.drop_column("tool_descriptors", "resource_version")
