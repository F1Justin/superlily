\set ON_ERROR_STOP on
\if :{?window_start}
\else
\echo 'ERROR: pass the completed policy-v5 window_start explicitly'
SELECT 1 / 0 AS missing_required_window_start;
\endif
\if :{?window_end}
\else
\echo 'ERROR: pass window_end explicitly; it must be at least 24 hours after window_start'
SELECT 1 / 0 AS missing_required_window_end;
\endif
\if :{?grace_seconds}
\else
\set grace_seconds 30
\endif

\echo 'Window duration check (must return 1)'
SELECT 1 / CASE
    WHEN :'window_end'::timestamptz - :'window_start'::timestamptz >= interval '24 hours'
    THEN 1
    ELSE 0
END AS at_least_24_hours;

\echo 'Phase 2 window totals'
WITH window_sources AS (
    SELECT id
    FROM source_events
    WHERE first_received_at >= :'window_start'::timestamptz
      AND first_received_at < :'window_end'::timestamptz
)
SELECT
    (SELECT count(*) FROM window_sources) AS sources,
    (SELECT count(*) FROM event_observations eo JOIN window_sources w ON w.id = eo.source_event_id)
        AS observations,
    (SELECT count(*) FROM event_decisions ed JOIN window_sources w ON w.id = ed.source_event_id)
        AS decisions,
    (SELECT count(*) FROM event_claims ec JOIN window_sources w ON w.id = ec.source_event_id)
        AS claims,
    (SELECT count(*) FROM responses r
     WHERE r.received_at >= :'window_start'::timestamptz
       AND r.received_at < :'window_end'::timestamptz) AS responses;

\echo 'Canonical invariant violations: every count must be zero'
WITH window_sources AS (
    SELECT id
    FROM source_events
    WHERE first_received_at >= :'window_start'::timestamptz
      AND first_received_at < :'window_end'::timestamptz
), deciding_observations AS (
    SELECT
        ed.source_event_id,
        ed.features_json,
        ed.decision_type,
        eo.text,
        eo.segments_json,
        eo.metadata_json,
        eo.bot_id,
        coalesce(
            (
                SELECT (segment->>'type') = 'text'
                FROM jsonb_array_elements(eo.segments_json::jsonb)
                     WITH ORDINALITY AS parts(segment, position)
                WHERE NOT (
                    segment->>'type' = 'reply'
                    OR (
                        segment->>'type' = 'text'
                        AND btrim(coalesce(segment->'data'->>'text', '')) = ''
                    )
                    OR (
                        segment->>'type' = 'at'
                        AND coalesce(
                            segment->'data'->>'qq',
                            segment->'data'->>'target',
                            segment->'data'->>'target_platform_userid',
                            segment->>'qq',
                            segment->>'target',
                            segment->>'target_platform_userid'
                        ) = eo.bot_id
                        AND (
                            coalesce(eo.metadata_json->>'to_me', 'false') IN ('true', '1')
                            OR coalesce(eo.metadata_json->>'is_tome', 'false') IN ('true', '1')
                        )
                    )
                )
                ORDER BY position
                LIMIT 1
            ),
            btrim(coalesce(eo.text, '')) <> ''
        ) AS derived_command_eligible
    FROM event_decisions ed
    JOIN window_sources w ON w.id = ed.source_event_id
    JOIN event_observations eo ON eo.id = ed.deciding_observation_id
)
SELECT 'decision_count' AS violation, count(*)
FROM (
    SELECT w.id
    FROM window_sources w
    LEFT JOIN event_decisions ed ON ed.source_event_id = w.id
    GROUP BY w.id
    HAVING count(ed.id) <> 1
) q
UNION ALL
SELECT 'same_instance_duplicate', count(*)
FROM (
    SELECT eo.source_event_id, eo.instance_id
    FROM event_observations eo
    JOIN window_sources w ON w.id = eo.source_event_id
    GROUP BY eo.source_event_id, eo.instance_id
    HAVING count(*) > 1
) q
UNION ALL
SELECT 'v3_more_than_two', count(*)
FROM (
    SELECT eo.source_event_id
    FROM event_observations eo
    JOIN source_events se ON se.id = eo.source_event_id
    JOIN window_sources w ON w.id = eo.source_event_id
    WHERE se.correlation_version = 'qq-message-v3'
    GROUP BY eo.source_event_id
    HAVING count(*) > 2
) q
UNION ALL
SELECT 'v3_pair_wrong_instances', count(*)
FROM (
    SELECT eo.source_event_id
    FROM event_observations eo
    JOIN source_events se ON se.id = eo.source_event_id
    JOIN window_sources w ON w.id = eo.source_event_id
    WHERE se.correlation_version = 'qq-message-v3'
    GROUP BY eo.source_event_id
    HAVING count(*) = 2
       AND array_agg(DISTINCT eo.instance_id ORDER BY eo.instance_id)
           <> ARRAY['lily-command', 'nekro-agent']::varchar[]
) q
UNION ALL
SELECT 'native_time_conflict', count(*)
FROM (
    SELECT eo.source_event_id
    FROM event_observations eo
    JOIN window_sources w ON w.id = eo.source_event_id
    WHERE eo.metadata_json->'native_identity'->>'time' IS NOT NULL
    GROUP BY eo.source_event_id
    HAVING count(DISTINCT eo.metadata_json->'native_identity'->>'time') > 1
) q
UNION ALL
SELECT 'strong_fingerprint_split', count(*)
FROM (
    SELECT se.correlation_fingerprint
    FROM source_events se
    WHERE se.correlation_fingerprint IS NOT NULL
      AND EXISTS (
          SELECT 1
          FROM source_events window_source
          JOIN window_sources w ON w.id = window_source.id
          WHERE window_source.correlation_fingerprint = se.correlation_fingerprint
      )
    GROUP BY se.correlation_fingerprint
    HAVING count(DISTINCT se.id) > 1
) q
UNION ALL
SELECT 'reported_source_identity_conflict', count(*)
FROM (
    SELECT eo.instance_id, eo.reported_source_event_id
    FROM event_observations eo
    JOIN window_sources w ON w.id = eo.source_event_id
    GROUP BY eo.instance_id, eo.reported_source_event_id
    HAVING count(DISTINCT eo.source_event_id) > 1
) q
UNION ALL
SELECT 'idempotency_identity_conflict', count(*)
FROM (
    SELECT eo.instance_id, eo.idempotency_key
    FROM event_observations eo
    JOIN window_sources w ON w.id = eo.source_event_id
    GROUP BY eo.instance_id, eo.idempotency_key
    HAVING count(DISTINCT eo.source_event_id) > 1
) q
UNION ALL
SELECT 'qq_message_reported_source_not_v2', count(*)
FROM event_observations eo
JOIN source_events se ON se.id = eo.source_event_id
JOIN window_sources w ON w.id = eo.source_event_id
WHERE se.platform = 'qq'
  AND se.event_type = 'message'
  AND eo.reported_source_event_id !~ '^qq:source:v2:[0-9a-f]{64}$'
UNION ALL
SELECT 'bot_actionable', count(DISTINCT ed.source_event_id)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
JOIN event_observations eo ON eo.source_event_id = w.id
WHERE eo.sender_id IN ('3643287298', '2022692714')
  AND ed.decision_type IN ('command', 'talk')
UNION ALL
SELECT 'non_v3_policy', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.policy_version <> 'qq-v3-policy-v5'
UNION ALL
SELECT 'command_eligible_missing_or_invalid', count(*)
FROM deciding_observations decision
WHERE jsonb_typeof((decision.features_json::jsonb)->'command_eligible') IS DISTINCT FROM 'boolean'
UNION ALL
SELECT 'command_eligible_structure_mismatch', count(*)
FROM deciding_observations decision
WHERE ((decision.features_json::jsonb)->'command_eligible')
      IS DISTINCT FROM to_jsonb(decision.derived_command_eligible)
UNION ALL
SELECT 'command_ineligible_actionable_command', count(*)
FROM deciding_observations decision
WHERE decision.derived_command_eligible IS FALSE
  AND decision.decision_type = 'command'
UNION ALL
SELECT 'group_mode_missing_or_invalid', count(*)
FROM event_decisions ed
JOIN source_events se ON se.id = ed.source_event_id
JOIN window_sources w ON w.id = ed.source_event_id
WHERE se.conversation_type = 'group'
  AND coalesce(ed.features_json->>'conversation_mode', '') NOT IN (
      'command_only', 'conversation_only', 'full', 'observe_only'
  )
UNION ALL
SELECT 'command_only_talk', count(*)
FROM event_decisions ed
JOIN source_events se ON se.id = ed.source_event_id
JOIN window_sources w ON w.id = ed.source_event_id
WHERE se.conversation_type = 'group'
  AND ed.features_json->>'conversation_mode' = 'command_only'
  AND ed.decision_type = 'talk'
UNION ALL
SELECT 'command_only_command_wrong_target', count(*)
FROM event_decisions ed
JOIN source_events se ON se.id = ed.source_event_id
JOIN window_sources w ON w.id = ed.source_event_id
WHERE se.conversation_type = 'group'
  AND ed.features_json->>'conversation_mode' = 'command_only'
  AND ed.decision_type = 'command'
  AND ed.target_instance_id IS DISTINCT FROM 'lily-command'
UNION ALL
SELECT 'conversation_only_command', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'conversation_mode' = 'conversation_only'
  AND ed.decision_type = 'command'
UNION ALL
SELECT 'conversation_only_talk_wrong_target', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'conversation_mode' = 'conversation_only'
  AND ed.decision_type = 'talk'
  AND ed.target_instance_id IS DISTINCT FROM 'nekro-agent'
UNION ALL
SELECT 'observe_only_actionable', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'conversation_mode' = 'observe_only'
  AND ed.decision_type IN ('command', 'talk')
UNION ALL
SELECT 'full_action_wrong_target', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'conversation_mode' = 'full'
  AND (
      (ed.decision_type = 'command' AND ed.target_instance_id IS DISTINCT FROM 'lily-command')
      OR (ed.decision_type = 'talk' AND ed.target_instance_id IS DISTINCT FROM 'nekro-agent')
  )
UNION ALL
SELECT 'talk_enabled_reply_to_nekro_wrong_route', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'conversation_mode' IN ('conversation_only', 'full')
  AND ed.features_json->>'has_reply_link' = 'true'
  AND ed.features_json->>'reply_target_status' = 'resolved_bot'
  AND ed.features_json->>'reply_target_instance_id' = 'nekro-agent'
  AND (
      ed.decision_type IS DISTINCT FROM 'talk'
      OR ed.target_instance_id IS DISTINCT FROM 'nekro-agent'
      OR ed.reason IS DISTINCT FROM 'reply_to_talk_response'
  )
UNION ALL
SELECT 'talk_disabled_reply_to_nekro_actionable', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'conversation_mode' IN ('command_only', 'observe_only')
  AND ed.features_json->>'has_reply_link' = 'true'
  AND ed.features_json->>'reply_target_status' = 'resolved_bot'
  AND ed.features_json->>'reply_target_instance_id' = 'nekro-agent'
  AND ed.decision_type IN ('command', 'talk')
UNION ALL
SELECT 'reply_to_lily_actionable', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'has_reply_link' = 'true'
  AND ed.features_json->>'reply_target_status' = 'resolved_bot'
  AND ed.features_json->>'reply_target_instance_id' = 'lily-command'
  AND ed.decision_type IN ('command', 'talk')
UNION ALL
SELECT 'ambiguous_or_conflicting_reply_actionable', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'has_reply_link' = 'true'
  AND ed.features_json->>'reply_target_status' IN ('ambiguous', 'conflict')
  AND ed.decision_type IN ('command', 'talk')
UNION ALL
SELECT 'other_or_unresolved_reply_without_summon_actionable', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'has_reply_link' = 'true'
  AND ed.features_json->>'reply_target_status' IN ('resolved_other', 'unresolved')
  AND coalesce((ed.features_json->>'summons_talk_bot')::boolean, false) IS FALSE
  AND coalesce(jsonb_array_length((ed.features_json::jsonb)->'mentioned_bot_instance_ids'), 0) = 0
  AND ed.decision_type IN ('command', 'talk')
UNION ALL
SELECT 'talk_enabled_summoned_reply_wrong_route', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'has_reply_link' = 'true'
  AND ed.features_json->>'reply_target_status' IN ('resolved_other', 'unresolved')
  AND ed.features_json->>'conversation_mode' IN ('conversation_only', 'full')
  AND (
      coalesce((ed.features_json->>'summons_talk_bot')::boolean, false)
      OR coalesce(jsonb_array_length((ed.features_json::jsonb)->'mentioned_bot_instance_ids'), 0) > 0
  )
  AND (
      ed.decision_type IS DISTINCT FROM 'talk'
      OR ed.target_instance_id IS DISTINCT FROM 'nekro-agent'
      OR ed.reason IS DISTINCT FROM 'summons_talk_bot_with_reply'
  )
UNION ALL
SELECT 'talk_disabled_reply_actionable', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'has_reply_link' = 'true'
  AND ed.features_json->>'conversation_mode' IN ('command_only', 'observe_only')
  AND ed.decision_type IN ('command', 'talk')
UNION ALL
SELECT 'private_recipient_wrong_route', count(*)
FROM event_decisions ed
JOIN source_events se ON se.id = ed.source_event_id
JOIN window_sources w ON w.id = ed.source_event_id
WHERE se.conversation_type = 'private'
  AND (
      (ed.decision_type = 'talk' AND (
          ed.target_instance_id IS DISTINCT FROM 'nekro-agent'
          OR ed.features_json->>'observing_instance_id' IS DISTINCT FROM 'nekro-agent'
      ))
      OR (ed.decision_type = 'command' AND (
          ed.target_instance_id IS DISTINCT FROM 'lily-command'
          OR ed.features_json->>'observing_instance_id' IS DISTINCT FROM 'lily-command'
      ))
      OR (
          ed.features_json->>'observing_instance_id' IS DISTINCT FROM 'nekro-agent'
          AND ed.decision_type = 'talk'
      )
  );

\echo 'Claim invariant violations: every count must be zero'
WITH window_sources AS (
    SELECT id, platform, conversation_type, conversation_id
    FROM source_events
    WHERE first_received_at >= :'window_start'::timestamptz
      AND first_received_at < :'window_end'::timestamptz
), enforced_allow_coordination AS (
    SELECT
        allow_claim.id,
        allow_claim.source_event_id,
        allow_claim.instance_id,
        ARRAY(
            SELECT DISTINCT observation.instance_id
            FROM event_observations observation
            WHERE observation.source_event_id = allow_claim.source_event_id
              AND observation.instance_id IS DISTINCT FROM allow_claim.instance_id
              AND observation.received_at <= allow_claim.created_at
            ORDER BY observation.instance_id
        )::varchar[] AS observed_peer_instance_ids,
        ARRAY(
            SELECT DISTINCT deny_claim.instance_id
            FROM event_claims deny_claim
            WHERE deny_claim.source_event_id = allow_claim.source_event_id
              AND deny_claim.instance_id IS DISTINCT FROM allow_claim.instance_id
              AND deny_claim.enforced
              AND deny_claim.action = 'deny'
              AND deny_claim.created_at <= allow_claim.created_at
              AND deny_claim.acknowledged_at IS NOT NULL
              AND deny_claim.acknowledged_at <= allow_claim.created_at
            ORDER BY deny_claim.instance_id
        )::varchar[] AS acknowledged_deny_instance_ids
    FROM event_claims allow_claim
    JOIN window_sources w ON w.id = allow_claim.source_event_id
    WHERE allow_claim.enforced
      AND allow_claim.action = 'allow'
), suppress_all_coordination AS (
    SELECT
        ed.source_event_id,
        ARRAY(
            SELECT DISTINCT observation.instance_id
            FROM event_observations observation
            WHERE observation.source_event_id = ed.source_event_id
            ORDER BY observation.instance_id
        )::varchar[] AS observed_instance_ids,
        ARRAY(
            SELECT DISTINCT deny_claim.instance_id
            FROM event_claims deny_claim
            WHERE deny_claim.source_event_id = ed.source_event_id
              AND deny_claim.enforced
              AND deny_claim.action = 'deny'
              AND deny_claim.reason = 'decision_suppress_all:reply_to_other_observed'
              AND deny_claim.acknowledged_at IS NOT NULL
              AND deny_claim.acknowledged_at >= deny_claim.created_at
              AND deny_claim.features_json->'gates'->>'suppression_scope' = 'all_instances'
            ORDER BY deny_claim.instance_id
        )::varchar[] AS acknowledged_deny_instance_ids
    FROM event_decisions ed
    JOIN source_events source ON source.id = ed.source_event_id
    JOIN window_sources w ON w.id = ed.source_event_id
    WHERE w.platform = 'qq'
      AND w.conversation_type = 'group'
      AND w.conversation_id = '708309706'
      AND source.correlation_version = 'qq-message-v3'
      AND ed.policy_version = 'qq-v3-policy-v6'
      AND ed.reason = 'reply_to_other_observed'
      AND ed.features_json->>'reply_target_status' = 'resolved_other'
      AND coalesce((ed.features_json->>'summons_talk_bot')::boolean, false) IS FALSE
      AND coalesce((ed.features_json->>'mentions_observing_bot')::boolean, false) IS FALSE
      AND EXISTS (
          SELECT 1
          FROM event_observations observation
          WHERE observation.source_event_id = ed.source_event_id
      )
)
SELECT 'enforced_outside_canary' AS violation, count(*)
FROM event_claims ec
JOIN window_sources w ON w.id = ec.source_event_id
WHERE ec.enforced
  AND NOT (
      w.platform = 'qq'
      AND w.conversation_type = 'group'
      AND w.conversation_id = '708309706'
  )
UNION ALL
SELECT 'multiple_enforced_allow', count(*)
FROM (
    SELECT ec.source_event_id
    FROM event_claims ec
    JOIN window_sources w ON w.id = ec.source_event_id
    WHERE ec.enforced AND ec.action = 'allow'
    GROUP BY ec.source_event_id
    HAVING count(*) > 1
) q
UNION ALL
SELECT 'allow_without_exact_prior_acknowledged_peer_denies', count(*)
FROM enforced_allow_coordination coordination
WHERE coordination.observed_peer_instance_ids
      IS DISTINCT FROM coordination.acknowledged_deny_instance_ids
   OR cardinality(coordination.observed_peer_instance_ids) = 0
UNION ALL
SELECT 'invalid_suppression_acknowledgement', count(*)
FROM event_claims claim
JOIN window_sources w ON w.id = claim.source_event_id
WHERE claim.acknowledged_at IS NOT NULL
  AND (
      claim.action IS DISTINCT FROM 'deny'
      OR claim.enforced IS DISTINCT FROM true
      OR claim.acknowledged_at < claim.created_at
  )
UNION ALL
SELECT 'suppress_all_has_allow_or_wrong_enforced_claim', count(*)
FROM event_claims claim
JOIN suppress_all_coordination suppression
  ON suppression.source_event_id = claim.source_event_id
WHERE claim.enforced
  AND (
      claim.action IS DISTINCT FROM 'deny'
      OR claim.reason IS DISTINCT FROM 'decision_suppress_all:reply_to_other_observed'
      OR claim.features_json->'gates'->>'suppression_scope' IS DISTINCT FROM 'all_instances'
  )
UNION ALL
SELECT 'suppress_all_without_exact_acknowledged_denies', count(*)
FROM suppress_all_coordination suppression
WHERE suppression.observed_instance_ids
      IS DISTINCT FROM suppression.acknowledged_deny_instance_ids
   OR cardinality(suppression.observed_instance_ids) = 0
UNION ALL
SELECT 'allow_coordination_feature_drift', count(*)
FROM event_claims allow_claim
JOIN enforced_allow_coordination coordination ON coordination.id = allow_claim.id
WHERE (((allow_claim.features_json::jsonb)->'coordination')->'observed_peer_instance_ids')
          IS DISTINCT FROM to_jsonb(coordination.observed_peer_instance_ids)
   OR (((allow_claim.features_json::jsonb)->'coordination')->'acknowledged_deny_instance_ids')
          IS DISTINCT FROM to_jsonb(coordination.acknowledged_deny_instance_ids);

\echo 'Decision and claim distributions'
WITH window_sources AS (
    SELECT id
    FROM source_events
    WHERE first_received_at >= :'window_start'::timestamptz
      AND first_received_at < :'window_end'::timestamptz
)
SELECT ed.policy_version, ed.decision_type, ed.reason, count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
GROUP BY ed.policy_version, ed.decision_type, ed.reason
ORDER BY count(*) DESC, ed.policy_version, ed.decision_type, ed.reason;

WITH window_sources AS (
    SELECT id
    FROM source_events
    WHERE first_received_at >= :'window_start'::timestamptz
      AND first_received_at < :'window_end'::timestamptz
)
SELECT ed.features_json->>'conversation_mode' AS conversation_mode, count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
GROUP BY ed.features_json->>'conversation_mode'
ORDER BY count(*) DESC, conversation_mode;

WITH window_sources AS (
    SELECT id
    FROM source_events
    WHERE first_received_at >= :'window_start'::timestamptz
      AND first_received_at < :'window_end'::timestamptz
)
SELECT ec.mode, ec.action, ec.reason, ec.enforced,
       ec.acknowledged_at IS NOT NULL AS acknowledged, count(*)
FROM event_claims ec
JOIN window_sources w ON w.id = ec.source_event_id
GROUP BY ec.mode, ec.action, ec.reason, ec.enforced, ec.acknowledged_at IS NOT NULL
ORDER BY count(*) DESC, ec.mode, ec.action, ec.reason, ec.enforced, acknowledged;

\echo 'Response trigger invariant violations: every count must be zero'
WITH window_responses AS (
    SELECT r.*
    FROM responses r
    WHERE r.received_at >= :'window_start'::timestamptz
      AND r.received_at < :'window_end'::timestamptz
), resolved_triggers AS (
    SELECT r.*, se.id AS resolved_source_id,
           CASE
               WHEN r.platform = 'qq' AND r.conversation_type = 'group'
               THEN regexp_replace(r.conversation_id, '^group_', '')
               WHEN r.platform = 'qq' AND r.conversation_type = 'private'
               THEN regexp_replace(r.conversation_id, '^private_', '')
               ELSE r.conversation_id
           END AS canonical_response_conversation_id
    FROM window_responses r
    LEFT JOIN source_events se ON se.id = r.trigger_source_event_id
)
SELECT 'duplicate_same_instance_successful_response' AS violation, count(*)
FROM (
    SELECT trigger_source_event_id, instance_id
    FROM window_responses
    WHERE success
      AND trigger_source_event_id IS NOT NULL
    GROUP BY trigger_source_event_id, instance_id
    HAVING count(*) > 1
) duplicates
UNION ALL
SELECT 'trigger_observation_mismatch', count(*)
FROM window_responses r
JOIN event_observations eo ON eo.id = r.trigger_observation_id
WHERE eo.instance_id IS DISTINCT FROM r.instance_id
   OR eo.source_event_id IS DISTINCT FROM r.trigger_source_event_id
UNION ALL
SELECT 'resolved_trigger_context_mismatch', count(*)
FROM resolved_triggers r
WHERE r.resolved_source_id IS NOT NULL
  AND (
      r.platform IS DISTINCT FROM (
          SELECT se.platform FROM source_events se WHERE se.id = r.resolved_source_id
      )
      OR r.conversation_type IS DISTINCT FROM (
          SELECT se.conversation_type FROM source_events se WHERE se.id = r.resolved_source_id
      )
      OR r.canonical_response_conversation_id IS DISTINCT FROM (
          SELECT se.conversation_id FROM source_events se WHERE se.id = r.resolved_source_id
      )
      OR NOT EXISTS (
          SELECT 1
          FROM event_observations eo
          WHERE eo.source_event_id = r.resolved_source_id
            AND eo.instance_id = r.instance_id
      )
  )
UNION ALL
SELECT 'trigger_resolution_status_mismatch', count(*)
FROM resolved_triggers r
WHERE coalesce(r.metadata_json->>'trigger_resolution_status', '') NOT IN (
          'none', 'observation', 'reported_source', 'canonical_source', 'unresolved'
      )
   OR (
      r.metadata_json->>'trigger_resolution_status' = 'none'
      AND (r.trigger_observation_id IS NOT NULL OR r.trigger_source_event_id IS NOT NULL)
   )
   OR (
      r.metadata_json->>'trigger_resolution_status' IN (
          'observation', 'reported_source', 'canonical_source'
      )
      AND r.resolved_source_id IS NULL
   )
   OR (
      r.metadata_json->>'trigger_resolution_status' = 'unresolved'
      AND (r.trigger_source_event_id IS NULL OR r.resolved_source_id IS NOT NULL)
   )
UNION ALL
SELECT 'trigger_attribution_context_mismatch', count(*)
FROM resolved_triggers r
WHERE r.resolved_source_id IS NOT NULL
  AND (
      (r.instance_id = 'lily-command'
       AND r.metadata_json->>'trigger_attribution' IS DISTINCT FROM 'event_context')
      OR (
          r.instance_id = 'nekro-agent'
          AND (
              (r.metadata_json->>'completion_status' = 'suppressed'
               AND r.metadata_json->>'trigger_attribution'
                   IS DISTINCT FROM 'claim_suppression')
              OR (r.metadata_json->>'completion_status' IS DISTINCT FROM 'suppressed'
                  AND r.metadata_json->>'trigger_attribution'
                      IS DISTINCT FROM 'task_context')
          )
      )
  )
UNION ALL
SELECT 'completion_status_mismatch', count(*)
FROM window_responses r
WHERE (r.success AND r.metadata_json->>'completion_status' IS DISTINCT FROM 'succeeded')
   OR (
      NOT r.success
      AND coalesce(r.metadata_json->>'completion_status', '') NOT IN (
          'failed', 'ambiguous', 'suppressed'
      )
   );

\echo 'Decision outcome distribution and exceptional rows'
WITH window_decisions AS (
    SELECT ed.*, se.first_received_at, se.conversation_type, se.conversation_id
    FROM event_decisions ed
    JOIN source_events se ON se.id = ed.source_event_id
    WHERE se.first_received_at >= :'window_start'::timestamptz
      AND se.first_received_at < :'window_end'::timestamptz
), response_sets AS (
    SELECT
        wd.source_event_id,
        array_agg(r.instance_id) FILTER (WHERE r.success) AS successful_instances,
        array_agg(r.instance_id) FILTER (
            WHERE NOT r.success
              AND r.metadata_json->>'completion_status' = 'ambiguous'
        ) AS ambiguous_instances,
        array_agg(r.instance_id) FILTER (
            WHERE NOT r.success
              AND coalesce(r.metadata_json->>'completion_status', '') NOT IN (
                  'ambiguous', 'suppressed'
              )
        ) AS failed_instances
    FROM window_decisions wd
    LEFT JOIN responses r
      ON r.trigger_source_event_id = wd.source_event_id
     AND r.received_at >= :'window_start'::timestamptz - make_interval(secs => :grace_seconds)
     AND r.received_at < :'window_end'::timestamptz + make_interval(secs => :grace_seconds)
    GROUP BY wd.source_event_id
), classified AS (
    SELECT
        wd.*,
        coalesce(rs.successful_instances, ARRAY[]::varchar[]) AS successful_instances,
        coalesce(rs.ambiguous_instances, ARRAY[]::varchar[]) AS ambiguous_instances,
        coalesce(rs.failed_instances, ARRAY[]::varchar[]) AS failed_instances,
        CASE
            WHEN wd.decision_type NOT IN ('command', 'talk') OR wd.target_instance_id IS NULL
                THEN CASE
                    WHEN cardinality(coalesce(rs.successful_instances, ARRAY[]::varchar[])) > 0
                      OR cardinality(coalesce(rs.ambiguous_instances, ARRAY[]::varchar[])) > 0
                      OR cardinality(coalesce(rs.failed_instances, ARRAY[]::varchar[])) > 0
                    THEN 'unexpected_response'
                    ELSE 'matched_no_response'
                END
            WHEN wd.target_instance_id = ANY(coalesce(rs.successful_instances, ARRAY[]::varchar[]))
                THEN CASE
                    WHEN cardinality(array_positions(
                        coalesce(rs.successful_instances, ARRAY[]::varchar[]),
                        wd.target_instance_id
                    )) > 1
                    THEN 'duplicate_successful_target_response'
                    WHEN EXISTS (
                        SELECT 1
                        FROM unnest(coalesce(rs.successful_instances, ARRAY[]::varchar[])) instance_id
                        WHERE instance_id IS DISTINCT FROM wd.target_instance_id
                    )
                    THEN 'matched_with_extra'
                    ELSE 'matched'
                END
            WHEN wd.target_instance_id = ANY(coalesce(rs.ambiguous_instances, ARRAY[]::varchar[]))
                THEN 'ambiguous_completion'
            WHEN wd.target_instance_id = ANY(coalesce(rs.failed_instances, ARRAY[]::varchar[]))
                THEN 'failed'
            WHEN cardinality(coalesce(rs.successful_instances, ARRAY[]::varchar[])) > 0
                THEN 'wrong_instance'
            WHEN extract(epoch FROM (least(now(), :'window_end'::timestamptz) - wd.first_received_at))
                 < :grace_seconds
                THEN 'pending'
            ELSE 'missed'
        END AS outcome
    FROM window_decisions wd
    JOIN response_sets rs ON rs.source_event_id = wd.source_event_id
)
SELECT outcome, count(*)
FROM classified
GROUP BY outcome
ORDER BY outcome;

WITH window_decisions AS (
    SELECT ed.*, se.first_received_at, se.conversation_type, se.conversation_id
    FROM event_decisions ed
    JOIN source_events se ON se.id = ed.source_event_id
    WHERE se.first_received_at >= :'window_start'::timestamptz
      AND se.first_received_at < :'window_end'::timestamptz
), response_sets AS (
    SELECT
        wd.source_event_id,
        array_agg(r.instance_id) FILTER (WHERE r.success) AS successful_instances,
        array_agg(r.instance_id) FILTER (
            WHERE NOT r.success
              AND r.metadata_json->>'completion_status' = 'ambiguous'
        ) AS ambiguous_instances,
        array_agg(r.instance_id) FILTER (
            WHERE NOT r.success
              AND coalesce(r.metadata_json->>'completion_status', '') NOT IN (
                  'ambiguous', 'suppressed'
              )
        ) AS failed_instances
    FROM window_decisions wd
    LEFT JOIN responses r
      ON r.trigger_source_event_id = wd.source_event_id
     AND r.received_at >= :'window_start'::timestamptz - make_interval(secs => :grace_seconds)
     AND r.received_at < :'window_end'::timestamptz + make_interval(secs => :grace_seconds)
    GROUP BY wd.source_event_id
), classified AS (
    SELECT
        wd.*,
        coalesce(rs.successful_instances, ARRAY[]::varchar[]) AS successful_instances,
        coalesce(rs.ambiguous_instances, ARRAY[]::varchar[]) AS ambiguous_instances,
        coalesce(rs.failed_instances, ARRAY[]::varchar[]) AS failed_instances,
        CASE
            WHEN wd.decision_type NOT IN ('command', 'talk') OR wd.target_instance_id IS NULL
                THEN CASE
                    WHEN cardinality(coalesce(rs.successful_instances, ARRAY[]::varchar[])) > 0
                      OR cardinality(coalesce(rs.ambiguous_instances, ARRAY[]::varchar[])) > 0
                      OR cardinality(coalesce(rs.failed_instances, ARRAY[]::varchar[])) > 0
                    THEN 'unexpected_response'
                    ELSE 'matched_no_response'
                END
            WHEN wd.target_instance_id = ANY(coalesce(rs.successful_instances, ARRAY[]::varchar[]))
                THEN CASE
                    WHEN cardinality(array_positions(
                        coalesce(rs.successful_instances, ARRAY[]::varchar[]),
                        wd.target_instance_id
                    )) > 1
                    THEN 'duplicate_successful_target_response'
                    WHEN EXISTS (
                        SELECT 1
                        FROM unnest(coalesce(rs.successful_instances, ARRAY[]::varchar[])) instance_id
                        WHERE instance_id IS DISTINCT FROM wd.target_instance_id
                    )
                    THEN 'matched_with_extra'
                    ELSE 'matched'
                END
            WHEN wd.target_instance_id = ANY(coalesce(rs.ambiguous_instances, ARRAY[]::varchar[]))
                THEN 'ambiguous_completion'
            WHEN wd.target_instance_id = ANY(coalesce(rs.failed_instances, ARRAY[]::varchar[]))
                THEN 'failed'
            WHEN cardinality(coalesce(rs.successful_instances, ARRAY[]::varchar[])) > 0
                THEN 'wrong_instance'
            WHEN extract(epoch FROM (least(now(), :'window_end'::timestamptz) - wd.first_received_at))
                 < :grace_seconds
                THEN 'pending'
            ELSE 'missed'
        END AS outcome
    FROM window_decisions wd
    JOIN response_sets rs ON rs.source_event_id = wd.source_event_id
)
SELECT
    source_event_id,
    conversation_type || ':' || conversation_id AS conversation,
    decision_type,
    target_instance_id,
    reason,
    outcome,
    successful_instances,
    ambiguous_instances,
    failed_instances,
    first_received_at
FROM classified
WHERE outcome NOT IN ('matched', 'matched_no_response')
ORDER BY first_received_at;

\echo 'Response exceptions for manual review'
SELECT id, instance_id, trigger_source_event_id, success,
       metadata_json->>'completion_status' AS completion_status,
       error, received_at
FROM responses
WHERE received_at >= :'window_start'::timestamptz
  AND received_at < :'window_end'::timestamptz
  AND (trigger_source_event_id IS NULL OR NOT success)
ORDER BY received_at;

\echo 'Structured-data and raw-retention violations: every count must be zero'
WITH window_sources AS (
    SELECT id
    FROM source_events
    WHERE first_received_at >= :'window_start'::timestamptz
      AND first_received_at < :'window_end'::timestamptz
), documents(scope, document) AS (
    SELECT 'observation_metadata', eo.metadata_json::jsonb
    FROM event_observations eo JOIN window_sources w ON w.id = eo.source_event_id
    UNION ALL
    SELECT 'observation_segments', eo.segments_json::jsonb
    FROM event_observations eo JOIN window_sources w ON w.id = eo.source_event_id
    UNION ALL
    SELECT 'observation_attachments', eo.attachments_json::jsonb
    FROM event_observations eo JOIN window_sources w ON w.id = eo.source_event_id
    UNION ALL
    SELECT 'response_metadata', r.metadata_json::jsonb
    FROM responses r
    WHERE r.received_at >= :'window_start'::timestamptz
      AND r.received_at < :'window_end'::timestamptz
    UNION ALL
    SELECT 'response_segments', r.segments_json::jsonb
    FROM responses r
    WHERE r.received_at >= :'window_start'::timestamptz
      AND r.received_at < :'window_end'::timestamptz
    UNION ALL
    SELECT 'response_attachments', r.attachments_json::jsonb
    FROM responses r
    WHERE r.received_at >= :'window_start'::timestamptz
      AND r.received_at < :'window_end'::timestamptz
    UNION ALL
    SELECT 'reference_raw', el.raw_json::jsonb
    FROM event_links el JOIN window_sources w ON w.id = el.from_source_event_id
), sensitive AS (
    SELECT scope, item
    FROM documents
    CROSS JOIN LATERAL jsonb_path_query(
        document,
        '$.** ? (@.type() == "object").keyvalue() ? (@.key like_regex "(^|_)(access_?token|api_?key|authorization|cookie|credential|database_?(dsn|url)|dsn|password|private_?key|secret|session|ticket|token)($|_)" flag "i")'
    ) item
), urls AS (
    SELECT scope, item
    FROM documents
    CROSS JOIN LATERAL jsonb_path_query(
        document,
        '$.** ? (@.type() == "object").keyvalue() ? (@.key like_regex "(url|uri|link|href|src|file|platform_id)$" flag "i")'
    ) item
), uri_strings AS (
    SELECT scope, scalar #>> '{}' AS value
    FROM documents
    CROSS JOIN LATERAL jsonb_path_query(
        document,
        '$.** ? (@.type() == "string")'
    ) scalar
    WHERE scalar #>> '{}' ~* '^[a-z][a-z0-9+.-]*://'
), file_or_platform_ids AS (
    SELECT scope, item
    FROM documents
    CROSS JOIN LATERAL jsonb_path_query(
        document,
        '$.** ? (@.type() == "object").keyvalue() ? (@.key like_regex "(file|platform_id)$" flag "i")'
    ) item
)
SELECT 'unredacted_sensitive_values' AS violation, count(*)
FROM sensitive
WHERE item->>'value' <> '[REDACTED]'
UNION ALL
SELECT 'url_queries_fragments_or_userinfo', count(*)
FROM urls
WHERE item->>'value' LIKE '%?%'
   OR item->>'value' LIKE '%#%'
   OR item->>'value' ~ '^[A-Za-z][A-Za-z0-9+.-]*://[^/[:space:]]+@'
UNION ALL
SELECT 'custom_uri_queries_fragments_or_userinfo', count(*)
FROM uri_strings
WHERE value LIKE '%?%'
   OR value LIKE '%#%'
   OR value ~ '^[A-Za-z][A-Za-z0-9+.-]*://[^/[:space:]]+@'
UNION ALL
SELECT 'file_or_platform_id_uri_residue', count(*)
FROM file_or_platform_ids
WHERE item->>'value' LIKE '%?%'
   OR item->>'value' LIKE '%#%'
   OR item->>'value' ~ '^[A-Za-z][A-Za-z0-9+.-]*://[^/[:space:]]+@'
UNION ALL
SELECT 'local_file_uri_retained', count(*)
FROM file_or_platform_ids
WHERE item->>'value' ~* '^file://'
UNION ALL
SELECT 'observation_raw_non_null', count(*)
FROM event_observations eo
JOIN window_sources w ON w.id = eo.source_event_id
WHERE eo.raw_json IS NOT NULL AND json_typeof(eo.raw_json) <> 'null'
UNION ALL
SELECT 'response_raw_non_null', count(*)
FROM responses r
WHERE r.received_at >= :'window_start'::timestamptz
  AND r.received_at < :'window_end'::timestamptz
  AND r.raw_json IS NOT NULL
  AND json_typeof(r.raw_json) <> 'null';

\echo 'Instances and reporter counters'
SELECT
    id,
    reported_status,
    last_heartbeat_at,
    metadata_json->>'queue_depth' AS queue_depth,
    metadata_json->>'dropped' AS dropped,
    metadata_json->>'claim_failures' AS claim_failures,
    metadata_json->'capabilities'->>'profile' AS capability_profile
FROM bot_instances
WHERE id IN ('lily-command', 'nekro-agent')
ORDER BY id;

\echo 'Latest runtime command registry snapshot'
SELECT
    instance_id,
    snapshot_hash,
    observed_at,
    received_at,
    json_array_length(plugins_json) AS plugins,
    json_array_length(candidates_json) AS candidates
FROM command_registry_snapshots
ORDER BY received_at DESC
LIMIT 1;
