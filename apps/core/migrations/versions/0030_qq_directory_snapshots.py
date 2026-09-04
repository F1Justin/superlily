"""Store bounded QQ friend and group-member directory snapshots."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0030_qq_directory_snapshots"
down_revision: str | None = "0029_qq_platform_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qq_directory_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "instance_id",
            sa.String(128),
            sa.ForeignKey("bot_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_id", sa.String(128), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_kind", sa.String(16), nullable=False),
        sa.Column("group_id", sa.String(256)),
        sa.Column("group_name", sa.String(512)),
        sa.Column("group_remark", sa.String(512)),
        sa.Column("member_count", sa.Integer()),
        sa.Column("max_member_count", sa.Integer()),
        sa.Column("whole_group_ban", sa.Boolean()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_apis_json", sa.JSON(), nullable=False),
        sa.Column("entry_count", sa.Integer(), nullable=False),
        sa.Column("capture_status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("instance_id", "snapshot_id", name="uq_qq_directory_snapshot_identity"),
        sa.CheckConstraint("snapshot_kind IN ('group', 'friends')", name="ck_qq_directory_snapshot_kind"),
        sa.CheckConstraint("capture_status IN ('complete', 'partial')", name="ck_qq_directory_capture_status"),
        sa.CheckConstraint("entry_count >= 0", name="ck_qq_directory_entry_count"),
        sa.CheckConstraint(
            "(snapshot_kind = 'group' AND group_id IS NOT NULL) OR "
            "(snapshot_kind = 'friends' AND group_id IS NULL)",
            name="ck_qq_directory_group_scope",
        ),
    )
    op.create_index(
        "ix_qq_directory_group_time",
        "qq_directory_snapshots",
        ["group_id", "observed_at", "id"],
    )
    op.create_index(
        "ix_qq_directory_instance_time",
        "qq_directory_snapshots",
        ["instance_id", "observed_at", "id"],
    )
    op.create_table(
        "qq_group_member_snapshots",
        sa.Column(
            "snapshot_record_id",
            sa.String(36),
            sa.ForeignKey("qq_directory_snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.String(256), primary_key=True),
        sa.Column("nickname", sa.String(512)),
        sa.Column("card", sa.String(512)),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("title", sa.String(512)),
        sa.Column("member_level", sa.String(512)),
        sa.Column("qq_level", sa.Integer()),
        sa.Column("joined_at", sa.DateTime(timezone=True)),
        sa.Column("last_sent_at", sa.DateTime(timezone=True)),
        sa.Column("muted_until", sa.DateTime(timezone=True)),
        sa.Column("is_robot", sa.Boolean()),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member', 'unknown')", name="ck_qq_group_member_role"),
        sa.CheckConstraint("qq_level IS NULL OR qq_level >= 0", name="ck_qq_group_member_qq_level"),
    )
    op.create_index(
        "ix_qq_group_member_user_snapshot",
        "qq_group_member_snapshots",
        ["user_id", "snapshot_record_id"],
    )
    op.create_table(
        "qq_friend_snapshots",
        sa.Column(
            "snapshot_record_id",
            sa.String(36),
            sa.ForeignKey("qq_directory_snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("user_id", sa.String(256), primary_key=True),
        sa.Column("nickname", sa.String(512)),
        sa.Column("remark", sa.String(512)),
        sa.Column("category_id", sa.String(256)),
        sa.Column("category_name", sa.String(512)),
    )
    op.create_index(
        "ix_qq_friend_user_snapshot",
        "qq_friend_snapshots",
        ["user_id", "snapshot_record_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_qq_friend_user_snapshot", table_name="qq_friend_snapshots")
    op.drop_table("qq_friend_snapshots")
    op.drop_index("ix_qq_group_member_user_snapshot", table_name="qq_group_member_snapshots")
    op.drop_table("qq_group_member_snapshots")
    op.drop_index("ix_qq_directory_instance_time", table_name="qq_directory_snapshots")
    op.drop_index("ix_qq_directory_group_time", table_name="qq_directory_snapshots")
    op.drop_table("qq_directory_snapshots")
