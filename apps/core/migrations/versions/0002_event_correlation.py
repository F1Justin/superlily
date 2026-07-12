"""Add cross-account event correlation fields."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_event_correlation"
down_revision: str | None = "0001_observability_spine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_events", sa.Column("correlation_fingerprint", sa.String(length=64)))
    op.add_column("source_events", sa.Column("correlation_version", sa.String(length=32)))
    op.create_index(
        "ix_source_events_correlation_time",
        "source_events",
        ["correlation_fingerprint", "occurred_at"],
    )

    op.add_column("event_observations", sa.Column("reported_source_event_id", sa.String(length=512)))
    op.add_column("event_observations", sa.Column("platform_message_id", sa.String(length=512)))
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            """
            UPDATE event_observations
            SET reported_source_event_id = source_event_id,
                platform_message_id = (
                    SELECT source.message_id
                    FROM source_events AS source
                    WHERE source.id = event_observations.source_event_id
                )
            """
        )
        with op.batch_alter_table("event_observations") as batch_op:
            batch_op.alter_column(
                "reported_source_event_id",
                existing_type=sa.String(length=512),
                nullable=False,
            )
            batch_op.create_unique_constraint(
                "uq_event_observation_reported_source",
                ["instance_id", "reported_source_event_id"],
            )
    else:
        op.execute(
            """
            UPDATE event_observations AS observation
            SET reported_source_event_id = observation.source_event_id,
                platform_message_id = source.message_id
            FROM source_events AS source
            WHERE source.id = observation.source_event_id
            """
        )
        op.alter_column("event_observations", "reported_source_event_id", nullable=False)
        op.create_unique_constraint(
            "uq_event_observation_reported_source",
            "event_observations",
            ["instance_id", "reported_source_event_id"],
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("event_observations") as batch_op:
            batch_op.drop_constraint(
                "uq_event_observation_reported_source",
                type_="unique",
            )
            batch_op.drop_column("platform_message_id")
            batch_op.drop_column("reported_source_event_id")
    else:
        op.drop_constraint(
            "uq_event_observation_reported_source",
            "event_observations",
            type_="unique",
        )
        op.drop_column("event_observations", "platform_message_id")
        op.drop_column("event_observations", "reported_source_event_id")
    op.drop_index("ix_source_events_correlation_time", table_name="source_events")
    op.drop_column("source_events", "correlation_version")
    op.drop_column("source_events", "correlation_fingerprint")
