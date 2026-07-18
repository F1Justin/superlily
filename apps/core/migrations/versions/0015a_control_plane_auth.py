"""增加最小控制面会话、幂等 mutation 与只追加审计。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0015a_control_plane_auth"
down_revision: str | None = "0015_tool_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ROLES = "'auditor', 'operator', 'reviewer', 'security_admin', 'break_glass'"
_APPEND_ONLY_TABLES = (
    "control_plane_login_attempts",
    "control_plane_mutations",
    "control_plane_audit_events",
)


def upgrade() -> None:
    op.create_table(
        "control_plane_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("resource_version", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reauthenticated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(f"role IN ({_ROLES})", name="ck_control_session_role"),
        sa.CheckConstraint("resource_version >= 1", name="ck_control_session_version"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_control_session_expiry"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_control_session_token_hash"),
    )
    op.create_index(
        "ix_control_sessions_operator_expiry",
        "control_plane_sessions",
        ["operator_id", "expires_at"],
    )

    op.create_table(
        "control_plane_login_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("operator_lookup_hash", sa.String(length=64), nullable=False),
        sa.Column("client_fingerprint_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'rejected')",
            name="ck_control_login_outcome",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_control_login_operator_time",
        "control_plane_login_attempts",
        ["operator_lookup_hash", "created_at"],
    )
    op.create_index(
        "ix_control_login_client_time",
        "control_plane_login_attempts",
        ["client_fingerprint_hash", "created_at"],
    )

    op.create_table(
        "control_plane_mutations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=512), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("preview_hash", sa.String(length=64), nullable=False),
        sa.Column("before_hash", sa.String(length=64), nullable=True),
        sa.Column("after_hash", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("result_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(f"role IN ({_ROLES})", name="ck_control_mutation_role"),
        sa.CheckConstraint("expected_version >= 1", name="ck_control_mutation_version"),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'rejected')",
            name="ck_control_mutation_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["control_plane_sessions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "operator_id",
            "operation",
            "idempotency_key",
            name="uq_control_mutation_idempotency",
        ),
    )
    op.create_index(
        "ix_control_mutations_target_time",
        "control_plane_mutations",
        ["target_type", "target_id", "created_at"],
    )

    op.create_table(
        "control_plane_audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("operator_id", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=True),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"role IS NULL OR role IN ({_ROLES})",
            name="ck_control_audit_role",
        ),
        sa.CheckConstraint(
            "outcome IN ('accepted', 'rejected')",
            name="ck_control_audit_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["control_plane_sessions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_control_audit_created",
        "control_plane_audit_events",
        ["created_at"],
    )
    op.create_index(
        "ix_control_audit_operator_created",
        "control_plane_audit_events",
        ["operator_id", "created_at"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'control plane evidence is append-only');
                END
                """
            )
            op.execute(
                f"""
                CREATE TRIGGER {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'control plane evidence is append-only');
                END
                """
            )
    elif dialect == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION reject_control_plane_evidence_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'control plane evidence is append-only';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        for table in _APPEND_ONLY_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER {table}_no_mutation
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION reject_control_plane_evidence_mutation()
                """
            )


def downgrade() -> None:
    op.drop_index("ix_control_audit_operator_created", table_name="control_plane_audit_events")
    op.drop_index("ix_control_audit_created", table_name="control_plane_audit_events")
    op.drop_table("control_plane_audit_events")
    op.drop_index("ix_control_mutations_target_time", table_name="control_plane_mutations")
    op.drop_table("control_plane_mutations")
    op.drop_index("ix_control_login_client_time", table_name="control_plane_login_attempts")
    op.drop_index("ix_control_login_operator_time", table_name="control_plane_login_attempts")
    op.drop_table("control_plane_login_attempts")
    op.drop_index("ix_control_sessions_operator_expiry", table_name="control_plane_sessions")
    op.drop_table("control_plane_sessions")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS reject_control_plane_evidence_mutation()")
