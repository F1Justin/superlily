\set ON_ERROR_STOP on

BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';

-- R3.1 seed corpus: factual reaction observations are selected explicitly for
-- one curator and one emoji. This query does not assign feedback semantics to
-- other reactions and does not mutate Core or archive data.
COPY (
    WITH punched AS (
        SELECT
            action.target_conversation_id,
            action.target_conversation_type,
            action.target_platform_message_id,
            min(action.occurred_at) AS first_punched_at,
            max(action.occurred_at) AS last_punched_at,
            count(*) AS observed_copies
        FROM platform_action_observations AS action
        WHERE action.action_kind = 'reaction'
          AND action.actor_principal_id = '2843657817'
          AND action.value_json ->> 'emoji_id' = '128074'
          AND coalesce((action.value_json ->> 'count')::integer, 1) > 0
        GROUP BY
            action.target_conversation_id,
            action.target_conversation_type,
            action.target_platform_message_id
    ),
    corpus AS (
        SELECT
            punched.first_punched_at,
            punched.last_punched_at,
            punched.target_conversation_id AS conversation_id,
            punched.target_conversation_type AS conversation_type,
            punched.target_platform_message_id AS response_platform_message_id,
            punched.observed_copies,
            response.id AS response_id,
            response.instance_id,
            response.trigger_source_event_id,
            response.occurred_at AS response_occurred_at,
            response.text AS lily_reply,
            trigger.sender_id AS trigger_sender_id,
            trigger.sender_name AS trigger_sender_name,
            trigger.text AS trigger_text,
            trigger.segments_json::text AS trigger_segments_json
        FROM punched
        LEFT JOIN responses AS response
          ON response.platform = 'qq'
         AND response.conversation_id = punched.target_conversation_id
         AND response.conversation_type = punched.target_conversation_type
         AND response.platform_message_id = punched.target_platform_message_id
        LEFT JOIN LATERAL (
            SELECT
                observation.sender_id,
                observation.sender_name,
                observation.text,
                observation.segments_json
            FROM event_observations AS observation
            WHERE observation.source_event_id = response.trigger_source_event_id
            ORDER BY
                (observation.sender_id = '2843657817') DESC,
                observation.received_at
            LIMIT 1
        ) AS trigger ON true
    )
    SELECT
        first_punched_at,
        last_punched_at,
        conversation_id,
        conversation_type,
        response_platform_message_id,
        observed_copies,
        response_id,
        instance_id,
        trigger_source_event_id,
        response_occurred_at,
        trigger_sender_id,
        trigger_sender_name,
        trigger_text,
        trigger_segments_json,
        lily_reply,
        ''::text AS evaluation_label,
        ''::text AS evaluation_notes
    FROM corpus
    ORDER BY first_punched_at
) TO STDOUT WITH (FORMAT csv, HEADER true);

ROLLBACK;
