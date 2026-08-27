\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chat_exporter') THEN
        RAISE EXCEPTION 'required role chat_exporter does not exist';
    END IF;
    IF current_user = 'chat_exporter' THEN
        RAISE EXCEPTION 'run this cutover as the Superlily database owner';
    END IF;
END
$$;

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM chat_exporter;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM chat_exporter;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA archive FROM chat_exporter;
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA archive FROM chat_exporter;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE SELECT ON TABLES FROM chat_exporter;
ALTER DEFAULT PRIVILEGES IN SCHEMA archive
    REVOKE SELECT ON TABLES FROM chat_exporter;

GRANT USAGE ON SCHEMA archive TO chat_exporter;
GRANT SELECT ON archive.message_timeline_v2,
                archive.conversation_mappings
TO chat_exporter;

SELECT
    has_schema_privilege('chat_exporter', 'archive', 'USAGE') AS archive_usage,
    has_table_privilege(
        'chat_exporter', 'archive.message_timeline_v2', 'SELECT'
    ) AS timeline_select,
    has_table_privilege(
        'chat_exporter', 'archive.conversation_mappings', 'SELECT'
    ) AS mappings_select,
    has_table_privilege(
        'chat_exporter', 'archive.legacy_messages', 'SELECT'
    ) AS legacy_table_select,
    has_table_privilege(
        'chat_exporter', 'public.source_events', 'SELECT'
    ) AS core_table_select;
