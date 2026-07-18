\set ON_ERROR_STOP on
\if :{?window_start}
\else
\echo 'ERROR: pass the historical policy-v5 window_start explicitly'
SELECT 1 / 0 AS missing_required_window_start;
\endif
\if :{?window_end}
\else
\echo 'ERROR: pass the historical policy-v5 window_end explicitly'
SELECT 1 / 0 AS missing_required_window_end;
\endif

\echo 'Policy v6 reply-to-other counterfactual summary'
WITH candidates AS (
    SELECT
        ed.source_event_id,
        se.conversation_id,
        se.correlation_version,
        ed.policy_version,
        ed.confidence AS historical_confidence,
        ed.features_json,
        (
            SELECT count(DISTINCT observation.instance_id)
            FROM event_observations observation
            WHERE observation.source_event_id = ed.source_event_id
        ) AS observer_count,
        EXISTS (
            SELECT 1
            FROM responses response
            WHERE response.trigger_source_event_id = ed.source_event_id
              AND response.success
        ) AS had_successful_response
    FROM event_decisions ed
    JOIN source_events se ON se.id = ed.source_event_id
    WHERE se.first_received_at >= :'window_start'::timestamptz
      AND se.first_received_at < :'window_end'::timestamptz
      AND ed.reason = 'reply_to_other_observed'
), classified AS (
    SELECT
        candidate.*,
        (
            candidate.correlation_version = 'qq-message-v3'
            AND candidate.observer_count >= 1
            AND candidate.features_json->>'reply_target_status' = 'resolved_other'
            AND coalesce(
                (candidate.features_json->>'summons_talk_bot')::boolean,
                false
            ) IS FALSE
            AND coalesce(
                (candidate.features_json->>'mentions_observing_bot')::boolean,
                false
            ) IS FALSE
        ) AS policy_v6_suppress_all_eligible
    FROM candidates candidate
)
SELECT metric, value
FROM (
    SELECT 1 AS position, 'historical_candidates' AS metric, count(*)::bigint AS value
    FROM classified
    UNION ALL
    SELECT 2, 'features_complete', count(*)
    FROM classified
    WHERE features_json::jsonb ? 'summons_talk_bot'
      AND features_json::jsonb ? 'mentions_observing_bot'
      AND features_json::jsonb ? 'reply_target_status'
    UNION ALL
    SELECT 3, 'single_observer_candidates', count(*)
    FROM classified
    WHERE observer_count = 1
    UNION ALL
    SELECT 4, 'multi_observer_candidates', count(*)
    FROM classified
    WHERE observer_count > 1
    UNION ALL
    SELECT 5, 'policy_v6_suppress_all_eligible_sources', count(*)
    FROM classified
    WHERE policy_v6_suppress_all_eligible
    UNION ALL
    SELECT 6, 'counterfactual_deny_claims', coalesce(sum(observer_count), 0)::bigint
    FROM classified
    WHERE policy_v6_suppress_all_eligible
    UNION ALL
    SELECT 7, 'eligible_sources_with_historical_successful_response', count(*)
    FROM classified
    WHERE policy_v6_suppress_all_eligible
      AND had_successful_response
    UNION ALL
    SELECT 8, 'historical_exact_canary_candidates', count(*)
    FROM classified
    WHERE conversation_id = '708309706'
) summary
ORDER BY position;

\echo 'Backtest integrity violations: every count must be zero'
WITH candidates AS (
    SELECT ed.features_json
    FROM event_decisions ed
    JOIN source_events se ON se.id = ed.source_event_id
    WHERE se.first_received_at >= :'window_start'::timestamptz
      AND se.first_received_at < :'window_end'::timestamptz
      AND ed.reason = 'reply_to_other_observed'
)
SELECT 'missing_required_decision_features' AS violation, count(*)
FROM candidates
WHERE NOT (
    features_json::jsonb ? 'summons_talk_bot'
    AND features_json::jsonb ? 'mentions_observing_bot'
    AND features_json::jsonb ? 'reply_target_status'
)
UNION ALL
SELECT 'reply_to_other_reason_with_wrong_status', count(*)
FROM candidates
WHERE features_json->>'reply_target_status' IS DISTINCT FROM 'resolved_other'
UNION ALL
SELECT 'reply_to_other_reason_with_summon_or_known_mention', count(*)
FROM candidates
WHERE coalesce((features_json->>'summons_talk_bot')::boolean, false)
   OR coalesce((features_json->>'mentions_observing_bot')::boolean, false);

\echo 'Historical successful responses that policy v6 would suppress when enforcement is enabled'
WITH candidates AS (
    SELECT
        ed.source_event_id,
        se.conversation_id,
        deciding.text,
        (
            SELECT count(DISTINCT observation.instance_id)
            FROM event_observations observation
            WHERE observation.source_event_id = ed.source_event_id
        ) AS observer_count,
        ed.features_json
    FROM event_decisions ed
    JOIN source_events se ON se.id = ed.source_event_id
    JOIN event_observations deciding ON deciding.id = ed.deciding_observation_id
    WHERE se.first_received_at >= :'window_start'::timestamptz
      AND se.first_received_at < :'window_end'::timestamptz
      AND se.correlation_version = 'qq-message-v3'
      AND ed.reason = 'reply_to_other_observed'
)
SELECT
    candidate.source_event_id,
    candidate.conversation_id,
    candidate.observer_count,
    left(candidate.text, 200) AS text_preview,
    array_agg(DISTINCT response.instance_id ORDER BY response.instance_id) AS response_instances
FROM candidates candidate
JOIN responses response
  ON response.trigger_source_event_id = candidate.source_event_id
 AND response.success
WHERE candidate.observer_count >= 1
  AND candidate.features_json->>'reply_target_status' = 'resolved_other'
  AND coalesce((candidate.features_json->>'summons_talk_bot')::boolean, false) IS FALSE
  AND coalesce((candidate.features_json->>'mentions_observing_bot')::boolean, false) IS FALSE
GROUP BY
    candidate.source_event_id,
    candidate.conversation_id,
    candidate.observer_count,
    candidate.text
ORDER BY candidate.source_event_id;
