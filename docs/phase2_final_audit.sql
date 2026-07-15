\set ON_ERROR_STOP on
\if :{?window_start}
\else
\set window_start '2026-07-15 02:15:49+00'
\endif
\if :{?window_end}
\else
\set window_end '2026-07-16 02:15:49+00'
\endif
\if :{?grace_seconds}
\else
\set grace_seconds 30
\endif

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
WHERE ed.policy_version <> 'qq-v3-policy-v4'
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
SELECT 'unsafe_reply_target_actionable', count(*)
FROM event_decisions ed
JOIN window_sources w ON w.id = ed.source_event_id
WHERE ed.features_json->>'has_reply_link' = 'true'
  AND ed.features_json->>'reply_target_status' IN ('resolved_other', 'ambiguous', 'conflict')
  AND ed.decision_type IN ('command', 'talk');

\echo 'Claim invariant violations: every count must be zero'
WITH window_sources AS (
    SELECT id, platform, conversation_type, conversation_id
    FROM source_events
    WHERE first_received_at >= :'window_start'::timestamptz
      AND first_received_at < :'window_end'::timestamptz
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
SELECT 'allow_coordination_mismatch', count(*)
FROM event_claims ec
JOIN window_sources w ON w.id = ec.source_event_id
WHERE ec.enforced
  AND ec.action = 'allow'
  AND (ec.features_json->'coordination'->'observed_peer_instance_ids')::jsonb
      <> (ec.features_json->'coordination'->'enforced_deny_instance_ids')::jsonb
UNION ALL
SELECT 'allow_without_prior_peer_deny', count(*)
FROM event_claims allow_claim
JOIN window_sources w ON w.id = allow_claim.source_event_id
WHERE allow_claim.enforced
  AND allow_claim.action = 'allow'
  AND NOT EXISTS (
      SELECT 1
      FROM event_claims deny_claim
      WHERE deny_claim.source_event_id = allow_claim.source_event_id
        AND deny_claim.enforced
        AND deny_claim.action = 'deny'
        AND deny_claim.created_at <= allow_claim.created_at
  );

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
SELECT ec.mode, ec.action, ec.reason, ec.enforced, count(*)
FROM event_claims ec
JOIN window_sources w ON w.id = ec.source_event_id
GROUP BY ec.mode, ec.action, ec.reason, ec.enforced
ORDER BY count(*) DESC, ec.mode, ec.action, ec.reason, ec.enforced;

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
        array_agg(DISTINCT r.instance_id) FILTER (WHERE r.success) AS successful_instances,
        array_agg(DISTINCT r.instance_id) FILTER (WHERE NOT r.success) AS failed_instances
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
        coalesce(rs.failed_instances, ARRAY[]::varchar[]) AS failed_instances,
        CASE
            WHEN wd.decision_type NOT IN ('command', 'talk') OR wd.target_instance_id IS NULL
                THEN CASE
                    WHEN cardinality(coalesce(rs.successful_instances, ARRAY[]::varchar[])) > 0
                      OR cardinality(coalesce(rs.failed_instances, ARRAY[]::varchar[])) > 0
                    THEN 'unexpected_response'
                    ELSE 'matched_no_response'
                END
            WHEN wd.target_instance_id = ANY(coalesce(rs.successful_instances, ARRAY[]::varchar[]))
                THEN CASE
                    WHEN cardinality(coalesce(rs.successful_instances, ARRAY[]::varchar[])) > 1
                    THEN 'matched_with_extra'
                    ELSE 'matched'
                END
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
        array_agg(DISTINCT r.instance_id) FILTER (WHERE r.success) AS successful_instances,
        array_agg(DISTINCT r.instance_id) FILTER (WHERE NOT r.success) AS failed_instances
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
        coalesce(rs.failed_instances, ARRAY[]::varchar[]) AS failed_instances,
        CASE
            WHEN wd.decision_type NOT IN ('command', 'talk') OR wd.target_instance_id IS NULL
                THEN CASE
                    WHEN cardinality(coalesce(rs.successful_instances, ARRAY[]::varchar[])) > 0
                      OR cardinality(coalesce(rs.failed_instances, ARRAY[]::varchar[])) > 0
                    THEN 'unexpected_response'
                    ELSE 'matched_no_response'
                END
            WHEN wd.target_instance_id = ANY(coalesce(rs.successful_instances, ARRAY[]::varchar[]))
                THEN CASE
                    WHEN cardinality(coalesce(rs.successful_instances, ARRAY[]::varchar[])) > 1
                    THEN 'matched_with_extra'
                    ELSE 'matched'
                END
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
    failed_instances,
    first_received_at
FROM classified
WHERE outcome NOT IN ('matched', 'matched_no_response')
ORDER BY first_received_at;

\echo 'Response exceptions for manual review'
SELECT id, instance_id, trigger_source_event_id, success, error, received_at
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
        '$.** ? (@.type() == "object").keyvalue() ? (@.key like_regex "(url|uri|link)$" flag "i")'
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
