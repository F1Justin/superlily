"""Expose action direction and targets; prefer complete media observations."""

from alembic import op

revision = "0035_export_action_details"
down_revision = "0034_qq_media_archive"
branch_labels = None
depends_on = None

# Keep column order and privileges stable for existing read-only consumers.
VIEW_SQL = """
CREATE OR REPLACE VIEW archive.message_timeline_v2 AS
        SELECT
            timeline.*,
            CASE
                WHEN timeline.kind = 'legacy_message' THEN 'message'
                WHEN timeline.kind = 'core_response' THEN 'message.response'
                ELSE COALESCE((
                    SELECT source_event.event_type
                    FROM public.source_events AS source_event
                    WHERE source_event.id = timeline.source_event_id
                ), 'unknown')
            END AS event_type,
            CASE
                WHEN timeline.kind = 'core_observation'
                THEN (
                    SELECT source_event.correlation_fingerprint
                    FROM public.source_events AS source_event
                    WHERE source_event.id = timeline.source_event_id
                )
                ELSE NULL
            END AS correlation_fingerprint,
            CASE
                WHEN timeline.kind = 'legacy_message'
                    THEN 'legacy:' || timeline.id
                WHEN timeline.kind = 'core_response'
                    THEN 'response:' || timeline.id
                ELSE 'event:' || COALESCE(
                    (
                        SELECT source_event.correlation_fingerprint
                        FROM public.source_events AS source_event
                        WHERE source_event.id = timeline.source_event_id
                    ),
                    timeline.source_event_id,
                    timeline.id
                )
            END AS display_key,
            CASE
                WHEN timeline.kind <> 'core_observation' THEN 0
                ELSE
                    CASE WHEN timeline.text IS NULL OR timeline.text = '' THEN 100 ELSE 0 END
                    + CASE WHEN timeline.sender_name IS NULL THEN 10 ELSE 0 END
                    + CASE WHEN timeline.relation_type = 'reply_to' THEN 0 ELSE 1 END
                    - 1000 * (SELECT count(*)::integer
                              FROM jsonb_array_elements(COALESCE(NULLIF(timeline.segments_json, '')::jsonb, '[]'::jsonb)) AS segment
                              WHERE (segment->'data' ?| ARRAY['summary', 'sub_type', 'emoji_id']
                                     OR segment ?| ARRAY['summary', 'sub_type', 'emoji_id']))
            END AS display_priority,
            COALESCE((
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'kind', action.action_kind,
                        'operation', action.operation,
                        'actor', action.actor_principal_id,
                        'subject', action.subject_principal_id,
                        'value', action.value_json,
                        'observer', action.observer_instance_id,
                        'capture_status', action.capture_status,
                        'target_event', action.target_source_event_id,
                        'target_msg', action.target_platform_message_id,
                        'target_text', target.text,
                        'target_sender', target.sender_id,
                        'target_segments', target.segments_json::jsonb
                    ) ORDER BY action.occurred_at, action.id
                )
                FROM public.platform_action_observations AS action
                JOIN public.event_observations AS action_observation
                  ON action_observation.id = action.observation_id
                LEFT JOIN LATERAL (
                    SELECT observation.text, observation.sender_id, observation.segments_json
                    FROM public.event_observations AS observation
                    WHERE observation.source_event_id = action.target_source_event_id
                    ORDER BY EXISTS (
                        SELECT 1 FROM jsonb_array_elements(observation.segments_json::jsonb) AS segment
                        WHERE (segment->'data' ?| ARRAY['summary', 'sub_type', 'emoji_id']
                                     OR segment ?| ARRAY['summary', 'sub_type', 'emoji_id'])
                    ) DESC, (observation.text IS NULL),
                       (observation.sender_name IS NULL), observation.received_at, observation.id
                    LIMIT 1
                ) AS target ON true
                WHERE timeline.kind = 'core_observation'
                  AND action_observation.source_event_id = timeline.source_event_id
            ), '[]'::jsonb)::text AS actions_json,
            CASE
                WHEN timeline.kind = 'core_observation'
                 AND timeline.relation_type = 'reply_to'
                 AND timeline.target_source_event_id IS NOT NULL
                THEN (
                    SELECT target_observation.sender_id
                    FROM public.event_observations AS target_observation
                    WHERE target_observation.source_event_id =
                          timeline.target_source_event_id
                    ORDER BY (target_observation.text IS NULL),
                             (target_observation.sender_name IS NULL),
                             target_observation.received_at,
                             target_observation.id
                    LIMIT 1
                )
                WHEN timeline.kind = 'core_observation'
                 AND timeline.relation_type = 'reply_to'
                 AND timeline.target_platform_message_id IS NOT NULL
                THEN (
                    SELECT target_observation.sender_id
                    FROM public.event_observations AS target_observation
                    JOIN public.source_events AS target_event
                      ON target_event.id = target_observation.source_event_id
                    WHERE target_observation.instance_id = timeline.instance_id
                      AND target_observation.platform_message_id =
                          timeline.target_platform_message_id
                      AND target_event.conversation_type = COALESCE(
                          timeline.target_conversation_type,
                          timeline.conversation_type
                      )
                      AND target_event.conversation_id = COALESCE(
                          timeline.target_conversation_id,
                          timeline.conversation_id
                      )
                      AND target_event.occurred_at <= timeline.occurred_at
                    ORDER BY (target_observation.text IS NULL),
                             (target_observation.sender_name IS NULL),
                             target_event.occurred_at DESC,
                             target_observation.received_at,
                             target_observation.id
                    LIMIT 1
                )
                WHEN timeline.kind = 'legacy_message'
                 AND COALESCE(
                     NULLIF(timeline.reply_hint_json, '')::jsonb
                     ->> 'target_platform_message_id',
                     ''
                 ) <> ''
                THEN (
                    SELECT target.sender_id
                    FROM archive.legacy_messages AS target
                    WHERE target.source_system = timeline.source_system
                      AND target.source_conversation_key =
                          timeline.source_conversation_key
                      AND target.bot_id IS NOT DISTINCT FROM timeline.bot_id
                      AND target.platform_message_id = (
                          NULLIF(timeline.reply_hint_json, '')::jsonb
                          ->> 'target_platform_message_id'
                      )
                      AND target.occurred_at <= timeline.occurred_at
                    ORDER BY target.occurred_at DESC, target.id DESC
                    LIMIT 1
                )
                ELSE NULL
            END AS reply_target_sender_id,
            CASE
                WHEN timeline.kind = 'core_observation'
                 AND timeline.relation_type = 'reply_to'
                 AND timeline.target_source_event_id IS NOT NULL
                THEN (
                    SELECT target_observation.text
                    FROM public.event_observations AS target_observation
                    WHERE target_observation.source_event_id =
                          timeline.target_source_event_id
                    ORDER BY (target_observation.text IS NULL),
                             (target_observation.sender_name IS NULL),
                             target_observation.received_at,
                             target_observation.id
                    LIMIT 1
                )
                WHEN timeline.kind = 'core_observation'
                 AND timeline.relation_type = 'reply_to'
                 AND timeline.target_platform_message_id IS NOT NULL
                THEN (
                    SELECT target_observation.text
                    FROM public.event_observations AS target_observation
                    JOIN public.source_events AS target_event
                      ON target_event.id = target_observation.source_event_id
                    WHERE target_observation.instance_id = timeline.instance_id
                      AND target_observation.platform_message_id =
                          timeline.target_platform_message_id
                      AND target_event.conversation_type = COALESCE(
                          timeline.target_conversation_type,
                          timeline.conversation_type
                      )
                      AND target_event.conversation_id = COALESCE(
                          timeline.target_conversation_id,
                          timeline.conversation_id
                      )
                      AND target_event.occurred_at <= timeline.occurred_at
                    ORDER BY (target_observation.text IS NULL),
                             (target_observation.sender_name IS NULL),
                             target_event.occurred_at DESC,
                             target_observation.received_at,
                             target_observation.id
                    LIMIT 1
                )
                WHEN timeline.kind = 'legacy_message'
                 AND COALESCE(
                     NULLIF(timeline.reply_hint_json, '')::jsonb
                     ->> 'target_platform_message_id',
                     ''
                 ) <> ''
                THEN (
                    SELECT target.content_text
                    FROM archive.legacy_messages AS target
                    WHERE target.source_system = timeline.source_system
                      AND target.source_conversation_key =
                          timeline.source_conversation_key
                      AND target.bot_id IS NOT DISTINCT FROM timeline.bot_id
                      AND target.platform_message_id = (
                          NULLIF(timeline.reply_hint_json, '')::jsonb
                          ->> 'target_platform_message_id'
                      )
                      AND target.occurred_at <= timeline.occurred_at
                    ORDER BY target.occurred_at DESC, target.id DESC
                    LIMIT 1
                )
                ELSE NULL
            END AS reply_target_text
        FROM archive.message_timeline_v1 AS timeline
"""

PREVIOUS_VIEW_SQL = """
CREATE OR REPLACE VIEW archive.message_timeline_v2 AS
        SELECT
            timeline.*,
            CASE
                WHEN timeline.kind = 'legacy_message' THEN 'message'
                WHEN timeline.kind = 'core_response' THEN 'message.response'
                ELSE COALESCE((
                    SELECT source_event.event_type
                    FROM public.source_events AS source_event
                    WHERE source_event.id = timeline.source_event_id
                ), 'unknown')
            END AS event_type,
            CASE
                WHEN timeline.kind = 'core_observation'
                THEN (
                    SELECT source_event.correlation_fingerprint
                    FROM public.source_events AS source_event
                    WHERE source_event.id = timeline.source_event_id
                )
                ELSE NULL
            END AS correlation_fingerprint,
            CASE
                WHEN timeline.kind = 'legacy_message'
                    THEN 'legacy:' || timeline.id
                WHEN timeline.kind = 'core_response'
                    THEN 'response:' || timeline.id
                ELSE 'event:' || COALESCE(
                    (
                        SELECT source_event.correlation_fingerprint
                        FROM public.source_events AS source_event
                        WHERE source_event.id = timeline.source_event_id
                    ),
                    timeline.source_event_id,
                    timeline.id
                )
            END AS display_key,
            CASE
                WHEN timeline.kind <> 'core_observation' THEN 0
                ELSE
                    CASE WHEN timeline.text IS NULL OR timeline.text = '' THEN 100 ELSE 0 END
                    + CASE WHEN timeline.sender_name IS NULL THEN 10 ELSE 0 END
                    + CASE WHEN timeline.relation_type = 'reply_to' THEN 0 ELSE 1 END
            END AS display_priority,
            COALESCE((
                SELECT jsonb_agg(
                    jsonb_build_object(
                        'kind', action.action_kind,
                        'actor', action.actor_principal_id,
                        'subject', action.subject_principal_id,
                        'value', action.value_json,
                        'target_msg', action.target_platform_message_id,
                        'target_text', CASE
                            WHEN action.action_kind = 'recall'
                             AND action.target_source_event_id IS NOT NULL
                            THEN (
                                SELECT target_observation.text
                                FROM public.event_observations AS target_observation
                                WHERE target_observation.source_event_id =
                                      action.target_source_event_id
                                ORDER BY (target_observation.text IS NULL),
                                         target_observation.received_at,
                                         target_observation.id
                                LIMIT 1
                            )
                            ELSE NULL
                        END
                    ) ORDER BY action.occurred_at, action.id
                )
                FROM public.platform_action_observations AS action
                JOIN public.event_observations AS action_observation
                  ON action_observation.id = action.observation_id
                WHERE timeline.kind = 'core_observation'
                  AND action_observation.source_event_id = timeline.source_event_id
            ), '[]'::jsonb)::text AS actions_json,
            CASE
                WHEN timeline.kind = 'core_observation'
                 AND timeline.relation_type = 'reply_to'
                 AND timeline.target_source_event_id IS NOT NULL
                THEN (
                    SELECT target_observation.sender_id
                    FROM public.event_observations AS target_observation
                    WHERE target_observation.source_event_id =
                          timeline.target_source_event_id
                    ORDER BY (target_observation.text IS NULL),
                             (target_observation.sender_name IS NULL),
                             target_observation.received_at,
                             target_observation.id
                    LIMIT 1
                )
                WHEN timeline.kind = 'core_observation'
                 AND timeline.relation_type = 'reply_to'
                 AND timeline.target_platform_message_id IS NOT NULL
                THEN (
                    SELECT target_observation.sender_id
                    FROM public.event_observations AS target_observation
                    JOIN public.source_events AS target_event
                      ON target_event.id = target_observation.source_event_id
                    WHERE target_observation.instance_id = timeline.instance_id
                      AND target_observation.platform_message_id =
                          timeline.target_platform_message_id
                      AND target_event.conversation_type = COALESCE(
                          timeline.target_conversation_type,
                          timeline.conversation_type
                      )
                      AND target_event.conversation_id = COALESCE(
                          timeline.target_conversation_id,
                          timeline.conversation_id
                      )
                      AND target_event.occurred_at <= timeline.occurred_at
                    ORDER BY (target_observation.text IS NULL),
                             (target_observation.sender_name IS NULL),
                             target_event.occurred_at DESC,
                             target_observation.received_at,
                             target_observation.id
                    LIMIT 1
                )
                WHEN timeline.kind = 'legacy_message'
                 AND COALESCE(
                     NULLIF(timeline.reply_hint_json, '')::jsonb
                     ->> 'target_platform_message_id',
                     ''
                 ) <> ''
                THEN (
                    SELECT target.sender_id
                    FROM archive.legacy_messages AS target
                    WHERE target.source_system = timeline.source_system
                      AND target.source_conversation_key =
                          timeline.source_conversation_key
                      AND target.bot_id IS NOT DISTINCT FROM timeline.bot_id
                      AND target.platform_message_id = (
                          NULLIF(timeline.reply_hint_json, '')::jsonb
                          ->> 'target_platform_message_id'
                      )
                      AND target.occurred_at <= timeline.occurred_at
                    ORDER BY target.occurred_at DESC, target.id DESC
                    LIMIT 1
                )
                ELSE NULL
            END AS reply_target_sender_id,
            CASE
                WHEN timeline.kind = 'core_observation'
                 AND timeline.relation_type = 'reply_to'
                 AND timeline.target_source_event_id IS NOT NULL
                THEN (
                    SELECT target_observation.text
                    FROM public.event_observations AS target_observation
                    WHERE target_observation.source_event_id =
                          timeline.target_source_event_id
                    ORDER BY (target_observation.text IS NULL),
                             (target_observation.sender_name IS NULL),
                             target_observation.received_at,
                             target_observation.id
                    LIMIT 1
                )
                WHEN timeline.kind = 'core_observation'
                 AND timeline.relation_type = 'reply_to'
                 AND timeline.target_platform_message_id IS NOT NULL
                THEN (
                    SELECT target_observation.text
                    FROM public.event_observations AS target_observation
                    JOIN public.source_events AS target_event
                      ON target_event.id = target_observation.source_event_id
                    WHERE target_observation.instance_id = timeline.instance_id
                      AND target_observation.platform_message_id =
                          timeline.target_platform_message_id
                      AND target_event.conversation_type = COALESCE(
                          timeline.target_conversation_type,
                          timeline.conversation_type
                      )
                      AND target_event.conversation_id = COALESCE(
                          timeline.target_conversation_id,
                          timeline.conversation_id
                      )
                      AND target_event.occurred_at <= timeline.occurred_at
                    ORDER BY (target_observation.text IS NULL),
                             (target_observation.sender_name IS NULL),
                             target_event.occurred_at DESC,
                             target_observation.received_at,
                             target_observation.id
                    LIMIT 1
                )
                WHEN timeline.kind = 'legacy_message'
                 AND COALESCE(
                     NULLIF(timeline.reply_hint_json, '')::jsonb
                     ->> 'target_platform_message_id',
                     ''
                 ) <> ''
                THEN (
                    SELECT target.content_text
                    FROM archive.legacy_messages AS target
                    WHERE target.source_system = timeline.source_system
                      AND target.source_conversation_key =
                          timeline.source_conversation_key
                      AND target.bot_id IS NOT DISTINCT FROM timeline.bot_id
                      AND target.platform_message_id = (
                          NULLIF(timeline.reply_hint_json, '')::jsonb
                          ->> 'target_platform_message_id'
                      )
                      AND target.occurred_at <= timeline.occurred_at
                    ORDER BY target.occurred_at DESC, target.id DESC
                    LIMIT 1
                )
                ELSE NULL
            END AS reply_target_text
        FROM archive.message_timeline_v1 AS timeline
"""


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '3s'")
        op.execute(VIEW_SQL)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '3s'")
        op.execute(PREVIOUS_VIEW_SQL)
