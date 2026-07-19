"""增加确认挑战与内容寻址制品账本。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0016_confirm_artifacts"
down_revision: str | None = "0015d_rollout_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_confirmations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("invocation_id", sa.String(length=36), nullable=False),
        sa.Column("policy", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("caller_type", sa.String(length=32), nullable=False),
        sa.Column("caller_id", sa.String(length=128), nullable=False),
        sa.Column("required_approvals", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "policy IN ('on_write', 'always', 'two_person')",
            name="ck_tool_confirmation_policy",
        ),
        sa.CheckConstraint(
            "caller_type IN ('command', 'admin_api')",
            name="ck_tool_confirmation_caller",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'consumed', 'rejected', 'expired')",
            name="ck_tool_confirmation_state",
        ),
        sa.CheckConstraint(
            "resource_version >= 1", name="ck_tool_confirmation_resource_version"
        ),
        sa.CheckConstraint(
            "required_approvals >= 1 AND required_approvals <= 2",
            name="ck_tool_confirmation_required_approvals",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_tool_confirmation_expiry"),
        sa.CheckConstraint(
            "((state = 'pending' AND consumed_at IS NULL AND rejected_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(state = 'consumed' AND consumed_at IS NOT NULL AND rejected_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(state = 'rejected' AND consumed_at IS NULL AND rejected_at IS NOT NULL "
            "AND expired_at IS NULL) OR "
            "(state = 'expired' AND consumed_at IS NULL AND rejected_at IS NULL "
            "AND expired_at IS NOT NULL))",
            name="ck_tool_confirmation_terminal_time",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"], ["tool_invocations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invocation_id", name="uq_tool_confirmation_invocation"),
    )
    op.create_index(
        "ix_tool_confirmations_state_expiry",
        "tool_confirmations",
        ["state", "expires_at"],
    )
    op.create_table(
        "tool_confirmation_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("confirmation_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_tool_confirmation_event_sequence"),
        sa.CheckConstraint(
            "event IN ('create', 'approve', 'reject', 'expire')",
            name="ck_tool_confirmation_event_type",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'consumed', 'rejected', 'expired')",
            name="ck_tool_confirmation_event_state",
        ),
        sa.CheckConstraint(
            "actor_type IN ('command', 'admin_api', 'reaper', 'system')",
            name="ck_tool_confirmation_event_actor",
        ),
        sa.CheckConstraint(
            "((event = 'create' AND sequence = 1 AND previous_state IS NULL "
            "AND state = 'pending') OR "
            "(event <> 'create' AND sequence > 1 AND previous_state = 'pending'))",
            name="ck_tool_confirmation_event_initial",
        ),
        sa.ForeignKeyConstraint(
            ["confirmation_id"], ["tool_confirmations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "confirmation_id", "sequence", name="uq_tool_confirmation_event_sequence"
        ),
        sa.UniqueConstraint(
            "confirmation_id",
            "actor_type",
            "actor_id",
            "idempotency_key",
            name="uq_tool_confirmation_event_idempotency",
        ),
    )
    op.create_index(
        "ix_tool_confirmation_events_created",
        "tool_confirmation_events",
        ["confirmation_id", "created_at"],
    )

    op.create_table(
        "tool_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("invocation_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("reservation_request_hash", sa.String(length=64), nullable=False),
        sa.Column("producer_tool_id", sa.String(length=128), nullable=False),
        sa.Column("producer_descriptor_version", sa.String(length=64), nullable=False),
        sa.Column("producer_descriptor_hash", sa.String(length=64), nullable=False),
        sa.Column("data_classification", sa.String(length=32), nullable=False),
        sa.Column("canonical_conversation", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=False),
        sa.Column("policy_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("max_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_width_pixels", sa.Integer(), nullable=False),
        sa.Column("max_height_pixels", sa.Integer(), nullable=False),
        sa.Column("declared_bytes", sa.BigInteger(), nullable=True),
        sa.Column("declared_sha256", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("upload_secret_hash", sa.String(length=64), nullable=False),
        sa.Column("quarantine_key", sa.String(length=512), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("width_pixels", sa.Integer(), nullable=True),
        sa.Column("height_pixels", sa.Integer(), nullable=True),
        sa.Column("storage_key", sa.String(length=512), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("referenced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("fencing_token >= 1", name="ck_tool_artifact_fencing_token"),
        sa.CheckConstraint(
            "data_classification IN ('public', 'conversation', 'sensitive', 'administrative')",
            name="ck_tool_artifact_classification",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'uploading', 'finalized', 'rejected', 'expired')",
            name="ck_tool_artifact_state",
        ),
        sa.CheckConstraint(
            "resource_version >= 1", name="ck_tool_artifact_resource_version"
        ),
        sa.CheckConstraint(
            "max_bytes >= 1 AND max_width_pixels >= 1 AND max_height_pixels >= 1",
            name="ck_tool_artifact_bounds",
        ),
        sa.CheckConstraint(
            "declared_bytes IS NULL OR (declared_bytes >= 1 AND declared_bytes <= max_bytes)",
            name="ck_tool_artifact_declared_bytes",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_tool_artifact_expiry"),
        sa.CheckConstraint(
            "((width_pixels IS NULL AND height_pixels IS NULL) OR "
            "(width_pixels IS NOT NULL AND height_pixels IS NOT NULL "
            "AND width_pixels >= 1 AND width_pixels <= max_width_pixels "
            "AND height_pixels >= 1 AND height_pixels <= max_height_pixels))",
            name="ck_tool_artifact_dimensions",
        ),
        sa.CheckConstraint(
            "((content_sha256 IS NULL AND byte_size IS NULL) OR "
            "(content_sha256 IS NOT NULL AND byte_size IS NOT NULL "
            "AND byte_size >= 1 AND byte_size <= max_bytes))",
            name="ck_tool_artifact_content",
        ),
        sa.CheckConstraint(
            "((state = 'finalized' AND content_sha256 IS NOT NULL "
            "AND byte_size IS NOT NULL AND storage_key IS NOT NULL "
            "AND finalized_at IS NOT NULL AND rejected_at IS NULL AND expired_at IS NULL) OR "
            "(state = 'rejected' AND rejected_at IS NOT NULL AND finalized_at IS NULL "
            "AND expired_at IS NULL) OR "
            "(state = 'expired' AND expired_at IS NOT NULL AND finalized_at IS NULL "
            "AND rejected_at IS NULL) OR "
            "(state IN ('reserved', 'uploading') AND finalized_at IS NULL "
            "AND rejected_at IS NULL AND expired_at IS NULL))",
            name="ck_tool_artifact_terminal_time",
        ),
        sa.ForeignKeyConstraint(
            ["invocation_id"], ["tool_invocations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["attempt_id"], ["tool_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["provider_id"], ["tool_providers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider_id", "idempotency_key", name="uq_tool_artifact_idempotency"
        ),
    )
    op.create_index(
        "ix_tool_artifacts_attempt_state", "tool_artifacts", ["attempt_id", "state"]
    )
    op.create_index(
        "ix_tool_artifacts_state_expiry", "tool_artifacts", ["state", "expires_at"]
    )
    op.create_index(
        "ix_tool_artifacts_content_hash", "tool_artifacts", ["content_sha256"]
    )
    op.create_table(
        "tool_artifact_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("artifact_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("previous_state", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_tool_artifact_event_sequence"),
        sa.CheckConstraint(
            "fencing_token >= 1", name="ck_tool_artifact_event_fencing_token"
        ),
        sa.CheckConstraint(
            "event IN ('reserve', 'upload_start', 'upload_complete', 'finalize', "
            "'reference', 'reject', 'expire', 'cleanup')",
            name="ck_tool_artifact_event_type",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'uploading', 'finalized', 'rejected', 'expired')",
            name="ck_tool_artifact_event_state",
        ),
        sa.CheckConstraint(
            "actor_type IN ('provider', 'reaper', 'system')",
            name="ck_tool_artifact_event_actor",
        ),
        sa.CheckConstraint(
            "((event = 'reserve' AND sequence = 1 AND previous_state IS NULL "
            "AND state = 'reserved') OR "
            "(event <> 'reserve' AND sequence > 1 AND previous_state IS NOT NULL))",
            name="ck_tool_artifact_event_initial",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"], ["tool_artifacts.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "artifact_id", "sequence", name="uq_tool_artifact_event_sequence"
        ),
    )
    op.create_index(
        "ix_tool_artifact_events_created",
        "tool_artifact_events",
        ["artifact_id", "created_at"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        _install_sqlite_triggers()
    elif dialect == "postgresql":
        _install_postgres_triggers()


def _install_sqlite_triggers() -> None:
    statements = [
        """
        CREATE TRIGGER tool_confirmations_authority_no_update
        BEFORE UPDATE OF invocation_id, policy, request_hash, input_hash, principal_hash,
            policy_hash, caller_type, caller_id, required_approvals, expires_at, created_at
        ON tool_confirmations BEGIN
            SELECT RAISE(ABORT, 'tool confirmation authority is immutable');
        END
        """,
        """
        CREATE TRIGGER tool_confirmations_no_delete BEFORE DELETE ON tool_confirmations
        BEGIN SELECT RAISE(ABORT, 'tool confirmation evidence cannot be deleted'); END
        """,
        """
        CREATE TRIGGER tool_confirmations_state_guard
        BEFORE UPDATE OF state, resource_version, consumed_at, rejected_at, expired_at, updated_at
        ON tool_confirmations
        WHEN NEW.state IS NOT OLD.state
          OR NEW.resource_version IS NOT OLD.resource_version
          OR NEW.consumed_at IS NOT OLD.consumed_at
          OR NEW.rejected_at IS NOT OLD.rejected_at
          OR NEW.expired_at IS NOT OLD.expired_at
          OR NEW.updated_at IS NOT OLD.updated_at
        BEGIN
            SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
                THEN RAISE(ABORT, 'tool confirmation resource version must increase by one') END;
            SELECT CASE WHEN NOT (
                (OLD.state = 'pending' AND NEW.state = 'consumed') OR
                (OLD.state = 'pending' AND NEW.state = 'rejected') OR
                (OLD.state = 'pending' AND NEW.state = 'expired')
            ) THEN RAISE(ABORT, 'tool confirmation state transition is not allowed') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM tool_confirmation_events
                WHERE confirmation_id = OLD.id AND sequence = NEW.resource_version
                  AND previous_state = OLD.state AND state = NEW.state
                  AND effective_at IS NEW.updated_at
                  AND ((event = 'approve' AND NEW.state = 'consumed'
                        AND NEW.consumed_at IS effective_at) OR
                       (event = 'reject' AND NEW.state = 'rejected'
                        AND NEW.rejected_at IS effective_at) OR
                       (event = 'expire' AND NEW.state = 'expired'
                        AND NEW.expired_at IS effective_at))
            ) THEN RAISE(ABORT, 'tool confirmation event is required') END;
        END
        """,
        """
        CREATE TRIGGER tool_artifacts_authority_no_update
        BEFORE UPDATE OF invocation_id, attempt_id, provider_id, fencing_token,
            idempotency_key, reservation_request_hash, producer_tool_id,
            producer_descriptor_version, producer_descriptor_hash, data_classification,
            canonical_conversation, mime_type, policy_snapshot_json, policy_hash,
            max_bytes, max_width_pixels, max_height_pixels, declared_bytes, declared_sha256,
            expires_at, upload_secret_hash, quarantine_key, created_at
        ON tool_artifacts BEGIN
            SELECT RAISE(ABORT, 'tool artifact authority is immutable');
        END
        """,
        """
        CREATE TRIGGER tool_artifacts_no_delete BEFORE DELETE ON tool_artifacts
        BEGIN SELECT RAISE(ABORT, 'tool artifact evidence cannot be deleted'); END
        """,
        """
        CREATE TRIGGER tool_artifacts_state_guard
        BEFORE UPDATE OF state, resource_version, content_sha256, byte_size, width_pixels,
            height_pixels, storage_key, finalized_at, referenced_at, rejected_at,
            expired_at, content_deleted_at, updated_at
        ON tool_artifacts
        WHEN NEW.state IS NOT OLD.state
          OR NEW.resource_version IS NOT OLD.resource_version
          OR NEW.content_sha256 IS NOT OLD.content_sha256
          OR NEW.byte_size IS NOT OLD.byte_size
          OR NEW.width_pixels IS NOT OLD.width_pixels
          OR NEW.height_pixels IS NOT OLD.height_pixels
          OR NEW.storage_key IS NOT OLD.storage_key
          OR NEW.finalized_at IS NOT OLD.finalized_at
          OR NEW.referenced_at IS NOT OLD.referenced_at
          OR NEW.rejected_at IS NOT OLD.rejected_at
          OR NEW.expired_at IS NOT OLD.expired_at
          OR NEW.content_deleted_at IS NOT OLD.content_deleted_at
          OR NEW.updated_at IS NOT OLD.updated_at
        BEGIN
            SELECT CASE WHEN NEW.resource_version != OLD.resource_version + 1
                THEN RAISE(ABORT, 'tool artifact resource version must increase by one') END;
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM tool_artifact_events
                WHERE artifact_id = OLD.id AND sequence = NEW.resource_version
                  AND previous_state = OLD.state AND state = NEW.state
                  AND provider_id = OLD.provider_id AND fencing_token = OLD.fencing_token
                  AND effective_at IS NEW.updated_at
                  AND ((event = 'upload_start' AND OLD.state = 'reserved'
                        AND NEW.state = 'uploading'
                        AND NEW.content_sha256 IS OLD.content_sha256
                        AND NEW.byte_size IS OLD.byte_size
                        AND NEW.width_pixels IS OLD.width_pixels
                        AND NEW.height_pixels IS OLD.height_pixels
                        AND NEW.storage_key IS OLD.storage_key
                        AND NEW.finalized_at IS OLD.finalized_at
                        AND NEW.referenced_at IS OLD.referenced_at
                        AND NEW.rejected_at IS OLD.rejected_at
                        AND NEW.expired_at IS OLD.expired_at
                        AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                       (event = 'upload_complete' AND OLD.state = 'uploading'
                        AND NEW.state = 'uploading'
                        AND OLD.content_sha256 IS NULL AND OLD.byte_size IS NULL
                        AND OLD.width_pixels IS NULL AND OLD.height_pixels IS NULL
                        AND NEW.content_sha256 IS NOT NULL AND NEW.byte_size IS NOT NULL
                        AND NEW.width_pixels IS NOT NULL AND NEW.height_pixels IS NOT NULL
                        AND NEW.storage_key IS OLD.storage_key
                        AND NEW.finalized_at IS OLD.finalized_at
                        AND NEW.referenced_at IS OLD.referenced_at
                        AND NEW.rejected_at IS OLD.rejected_at
                        AND NEW.expired_at IS OLD.expired_at
                        AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                       (event = 'finalize' AND OLD.state = 'uploading'
                        AND NEW.state = 'finalized' AND OLD.finalized_at IS NULL
                        AND NEW.content_sha256 IS OLD.content_sha256
                        AND NEW.byte_size IS OLD.byte_size
                        AND NEW.width_pixels IS OLD.width_pixels
                        AND NEW.height_pixels IS OLD.height_pixels
                        AND NEW.storage_key IS NOT NULL
                        AND NEW.finalized_at IS effective_at
                        AND NEW.referenced_at IS OLD.referenced_at
                        AND NEW.rejected_at IS OLD.rejected_at
                        AND NEW.expired_at IS OLD.expired_at
                        AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                       (event = 'reference' AND OLD.state = 'finalized'
                        AND NEW.state = 'finalized' AND OLD.referenced_at IS NULL
                        AND NEW.content_sha256 IS OLD.content_sha256
                        AND NEW.byte_size IS OLD.byte_size
                        AND NEW.width_pixels IS OLD.width_pixels
                        AND NEW.height_pixels IS OLD.height_pixels
                        AND NEW.storage_key IS OLD.storage_key
                        AND NEW.finalized_at IS OLD.finalized_at
                        AND NEW.referenced_at IS effective_at
                        AND NEW.rejected_at IS OLD.rejected_at
                        AND NEW.expired_at IS OLD.expired_at
                        AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                       (event = 'reject' AND OLD.state IN ('reserved', 'uploading')
                        AND NEW.state = 'rejected' AND OLD.rejected_at IS NULL
                        AND NEW.content_sha256 IS OLD.content_sha256
                        AND NEW.byte_size IS OLD.byte_size
                        AND NEW.width_pixels IS OLD.width_pixels
                        AND NEW.height_pixels IS OLD.height_pixels
                        AND NEW.storage_key IS OLD.storage_key
                        AND NEW.finalized_at IS OLD.finalized_at
                        AND NEW.referenced_at IS OLD.referenced_at
                        AND NEW.rejected_at IS effective_at
                        AND NEW.expired_at IS OLD.expired_at
                        AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                       (event = 'expire' AND OLD.state IN ('reserved', 'uploading')
                        AND NEW.state = 'expired' AND OLD.expired_at IS NULL
                        AND NEW.content_sha256 IS OLD.content_sha256
                        AND NEW.byte_size IS OLD.byte_size
                        AND NEW.width_pixels IS OLD.width_pixels
                        AND NEW.height_pixels IS OLD.height_pixels
                        AND NEW.storage_key IS OLD.storage_key
                        AND NEW.finalized_at IS OLD.finalized_at
                        AND NEW.referenced_at IS OLD.referenced_at
                        AND NEW.rejected_at IS OLD.rejected_at
                        AND NEW.expired_at IS effective_at
                        AND NEW.content_deleted_at IS OLD.content_deleted_at) OR
                       (event = 'cleanup' AND NEW.state = OLD.state
                        AND OLD.content_deleted_at IS NULL
                        AND NEW.content_sha256 IS OLD.content_sha256
                        AND NEW.byte_size IS OLD.byte_size
                        AND NEW.width_pixels IS OLD.width_pixels
                        AND NEW.height_pixels IS OLD.height_pixels
                        AND NEW.storage_key IS OLD.storage_key
                        AND NEW.finalized_at IS OLD.finalized_at
                        AND NEW.referenced_at IS OLD.referenced_at
                        AND NEW.rejected_at IS OLD.rejected_at
                        AND NEW.expired_at IS OLD.expired_at
                        AND NEW.content_deleted_at IS effective_at))
            ) THEN RAISE(ABORT, 'tool artifact event is required') END;
        END
        """,
    ]
    for table in ("tool_confirmation_events", "tool_artifact_events"):
        statements.extend(
            [
                f"""
                CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'confirmation and artifact events are append-only'); END
                """,
                f"""
                CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, 'confirmation and artifact events are append-only'); END
                """,
            ]
        )
    for statement in statements:
        op.execute(statement)


def _install_postgres_triggers() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_tool_confirmation_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'tool confirmation evidence cannot be deleted';
            END IF;
            IF NEW.invocation_id IS DISTINCT FROM OLD.invocation_id
               OR NEW.policy IS DISTINCT FROM OLD.policy
               OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
               OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
               OR NEW.principal_hash IS DISTINCT FROM OLD.principal_hash
               OR NEW.policy_hash IS DISTINCT FROM OLD.policy_hash
               OR NEW.caller_type IS DISTINCT FROM OLD.caller_type
               OR NEW.caller_id IS DISTINCT FROM OLD.caller_id
               OR NEW.required_approvals IS DISTINCT FROM OLD.required_approvals
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'tool confirmation authority is immutable';
            END IF;
            IF NEW.state IS DISTINCT FROM OLD.state
               OR NEW.resource_version IS DISTINCT FROM OLD.resource_version
               OR NEW.consumed_at IS DISTINCT FROM OLD.consumed_at
               OR NEW.rejected_at IS DISTINCT FROM OLD.rejected_at
               OR NEW.expired_at IS DISTINCT FROM OLD.expired_at
               OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
                IF NEW.resource_version != OLD.resource_version + 1 THEN
                    RAISE EXCEPTION 'tool confirmation resource version must increase by one';
                END IF;
                IF NOT ((OLD.state = 'pending' AND NEW.state = 'consumed') OR
                        (OLD.state = 'pending' AND NEW.state = 'rejected') OR
                        (OLD.state = 'pending' AND NEW.state = 'expired')) THEN
                    RAISE EXCEPTION 'tool confirmation state transition is not allowed';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM tool_confirmation_events
                    WHERE confirmation_id = OLD.id AND sequence = NEW.resource_version
                      AND previous_state = OLD.state AND state = NEW.state
                      AND effective_at IS NOT DISTINCT FROM NEW.updated_at
                      AND ((event = 'approve' AND NEW.state = 'consumed'
                            AND NEW.consumed_at IS NOT DISTINCT FROM effective_at) OR
                           (event = 'reject' AND NEW.state = 'rejected'
                            AND NEW.rejected_at IS NOT DISTINCT FROM effective_at) OR
                           (event = 'expire' AND NEW.state = 'expired'
                            AND NEW.expired_at IS NOT DISTINCT FROM effective_at))
                ) THEN RAISE EXCEPTION 'tool confirmation event is required';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER tool_confirmations_guard BEFORE UPDATE OR DELETE ON tool_confirmations
        FOR EACH ROW EXECUTE FUNCTION guard_tool_confirmation_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_tool_artifact_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'tool artifact evidence cannot be deleted';
            END IF;
            IF NEW.invocation_id IS DISTINCT FROM OLD.invocation_id
               OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id
               OR NEW.provider_id IS DISTINCT FROM OLD.provider_id
               OR NEW.fencing_token IS DISTINCT FROM OLD.fencing_token
               OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
               OR NEW.reservation_request_hash IS DISTINCT FROM OLD.reservation_request_hash
               OR NEW.producer_tool_id IS DISTINCT FROM OLD.producer_tool_id
               OR NEW.producer_descriptor_version IS DISTINCT FROM OLD.producer_descriptor_version
               OR NEW.producer_descriptor_hash IS DISTINCT FROM OLD.producer_descriptor_hash
               OR NEW.data_classification IS DISTINCT FROM OLD.data_classification
               OR NEW.canonical_conversation IS DISTINCT FROM OLD.canonical_conversation
               OR NEW.mime_type IS DISTINCT FROM OLD.mime_type
               OR NEW.policy_snapshot_json::text IS DISTINCT FROM OLD.policy_snapshot_json::text
               OR NEW.policy_hash IS DISTINCT FROM OLD.policy_hash
               OR NEW.max_bytes IS DISTINCT FROM OLD.max_bytes
               OR NEW.max_width_pixels IS DISTINCT FROM OLD.max_width_pixels
               OR NEW.max_height_pixels IS DISTINCT FROM OLD.max_height_pixels
               OR NEW.declared_bytes IS DISTINCT FROM OLD.declared_bytes
               OR NEW.declared_sha256 IS DISTINCT FROM OLD.declared_sha256
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
               OR NEW.upload_secret_hash IS DISTINCT FROM OLD.upload_secret_hash
               OR NEW.quarantine_key IS DISTINCT FROM OLD.quarantine_key
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'tool artifact authority is immutable';
            END IF;
            IF NEW.state IS DISTINCT FROM OLD.state
               OR NEW.resource_version IS DISTINCT FROM OLD.resource_version
               OR NEW.content_sha256 IS DISTINCT FROM OLD.content_sha256
               OR NEW.byte_size IS DISTINCT FROM OLD.byte_size
               OR NEW.width_pixels IS DISTINCT FROM OLD.width_pixels
               OR NEW.height_pixels IS DISTINCT FROM OLD.height_pixels
               OR NEW.storage_key IS DISTINCT FROM OLD.storage_key
               OR NEW.finalized_at IS DISTINCT FROM OLD.finalized_at
               OR NEW.referenced_at IS DISTINCT FROM OLD.referenced_at
               OR NEW.rejected_at IS DISTINCT FROM OLD.rejected_at
               OR NEW.expired_at IS DISTINCT FROM OLD.expired_at
               OR NEW.content_deleted_at IS DISTINCT FROM OLD.content_deleted_at
               OR NEW.updated_at IS DISTINCT FROM OLD.updated_at THEN
                IF NEW.resource_version != OLD.resource_version + 1 THEN
                    RAISE EXCEPTION 'tool artifact resource version must increase by one';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM tool_artifact_events
                    WHERE artifact_id = OLD.id AND sequence = NEW.resource_version
                      AND previous_state = OLD.state AND state = NEW.state
                      AND provider_id = OLD.provider_id AND fencing_token = OLD.fencing_token
                      AND effective_at IS NOT DISTINCT FROM NEW.updated_at
                      AND ((event = 'upload_start' AND OLD.state = 'reserved'
                            AND NEW.state = 'uploading'
                            AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                            AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                            AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                            AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                            AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                            AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                            AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                            AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                            AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                            AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                           (event = 'upload_complete' AND OLD.state = 'uploading'
                            AND NEW.state = 'uploading'
                            AND OLD.content_sha256 IS NULL AND OLD.byte_size IS NULL
                            AND OLD.width_pixels IS NULL AND OLD.height_pixels IS NULL
                            AND NEW.content_sha256 IS NOT NULL AND NEW.byte_size IS NOT NULL
                            AND NEW.width_pixels IS NOT NULL AND NEW.height_pixels IS NOT NULL
                            AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                            AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                            AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                            AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                            AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                            AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                           (event = 'finalize' AND OLD.state = 'uploading'
                            AND NEW.state = 'finalized' AND OLD.finalized_at IS NULL
                            AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                            AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                            AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                            AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                            AND NEW.storage_key IS NOT NULL
                            AND NEW.finalized_at IS NOT DISTINCT FROM effective_at
                            AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                            AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                            AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                            AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                           (event = 'reference' AND OLD.state = 'finalized'
                            AND NEW.state = 'finalized' AND OLD.referenced_at IS NULL
                            AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                            AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                            AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                            AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                            AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                            AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                            AND NEW.referenced_at IS NOT DISTINCT FROM effective_at
                            AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                            AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                            AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                           (event = 'reject' AND OLD.state IN ('reserved', 'uploading')
                            AND NEW.state = 'rejected' AND OLD.rejected_at IS NULL
                            AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                            AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                            AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                            AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                            AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                            AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                            AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                            AND NEW.rejected_at IS NOT DISTINCT FROM effective_at
                            AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                            AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                           (event = 'expire' AND OLD.state IN ('reserved', 'uploading')
                            AND NEW.state = 'expired' AND OLD.expired_at IS NULL
                            AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                            AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                            AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                            AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                            AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                            AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                            AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                            AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                            AND NEW.expired_at IS NOT DISTINCT FROM effective_at
                            AND NEW.content_deleted_at IS NOT DISTINCT FROM OLD.content_deleted_at) OR
                           (event = 'cleanup' AND NEW.state = OLD.state
                            AND OLD.content_deleted_at IS NULL
                            AND NEW.content_sha256 IS NOT DISTINCT FROM OLD.content_sha256
                            AND NEW.byte_size IS NOT DISTINCT FROM OLD.byte_size
                            AND NEW.width_pixels IS NOT DISTINCT FROM OLD.width_pixels
                            AND NEW.height_pixels IS NOT DISTINCT FROM OLD.height_pixels
                            AND NEW.storage_key IS NOT DISTINCT FROM OLD.storage_key
                            AND NEW.finalized_at IS NOT DISTINCT FROM OLD.finalized_at
                            AND NEW.referenced_at IS NOT DISTINCT FROM OLD.referenced_at
                            AND NEW.rejected_at IS NOT DISTINCT FROM OLD.rejected_at
                            AND NEW.expired_at IS NOT DISTINCT FROM OLD.expired_at
                            AND NEW.content_deleted_at IS NOT DISTINCT FROM effective_at))
                ) THEN RAISE EXCEPTION 'tool artifact event is required';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER tool_artifacts_guard BEFORE UPDATE OR DELETE ON tool_artifacts
        FOR EACH ROW EXECUTE FUNCTION guard_tool_artifact_mutation()
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_confirmation_artifact_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'confirmation and artifact events are append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in ("tool_confirmation_events", "tool_artifact_events"):
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_mutation BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_confirmation_artifact_event_mutation()
            """
        )


def downgrade() -> None:
    op.drop_index("ix_tool_artifact_events_created", table_name="tool_artifact_events")
    op.drop_table("tool_artifact_events")
    op.drop_index("ix_tool_artifacts_content_hash", table_name="tool_artifacts")
    op.drop_index("ix_tool_artifacts_state_expiry", table_name="tool_artifacts")
    op.drop_index("ix_tool_artifacts_attempt_state", table_name="tool_artifacts")
    op.drop_table("tool_artifacts")
    op.drop_index(
        "ix_tool_confirmation_events_created", table_name="tool_confirmation_events"
    )
    op.drop_table("tool_confirmation_events")
    op.drop_index("ix_tool_confirmations_state_expiry", table_name="tool_confirmations")
    op.drop_table("tool_confirmations")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS reject_confirmation_artifact_event_mutation()")
        op.execute("DROP FUNCTION IF EXISTS guard_tool_artifact_mutation()")
        op.execute("DROP FUNCTION IF EXISTS guard_tool_confirmation_mutation()")
