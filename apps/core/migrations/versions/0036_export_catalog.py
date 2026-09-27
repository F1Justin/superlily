"""Expose a metadata-only conversation catalog for archive consumers."""
from alembic import op

revision = "0036_export_catalog"
down_revision = "0035_export_action_details"
branch_labels = None
depends_on = None

CATALOG_SQL = """
CREATE VIEW archive.conversation_catalog_v1 AS
WITH catalog AS (
    SELECT platform, conversation_type, conversation_id
    FROM archive.conversation_mappings
    WHERE active AND mapping_status = 'mapped'
    UNION
    SELECT platform, conversation_type, conversation_id
    FROM public.identity_name_observations WHERE conversation_id IS NOT NULL
    UNION
    SELECT platform, conversation_type, conversation_id
    FROM public.conversation_name_observations
    UNION
    SELECT platform, conversation_type, conversation_id
    FROM public.conversation_capture_profiles WHERE active
    UNION
    SELECT 'qq', 'group', group_id
    FROM public.qq_directory_snapshots WHERE group_id IS NOT NULL AND group_id <> ''
), names AS (
    SELECT platform, conversation_type, conversation_id, name_value, observed_at, id
    FROM public.conversation_name_observations
    UNION ALL
    SELECT 'qq', 'group', group_id, group_name, observed_at, id
    FROM public.qq_directory_snapshots WHERE NULLIF(group_name, '') IS NOT NULL
), latest_names AS (
    SELECT DISTINCT ON (platform, conversation_type, conversation_id)
        platform, conversation_type, conversation_id, name_value
    FROM names
    ORDER BY platform, conversation_type, conversation_id, observed_at DESC, id DESC
)
SELECT c.platform, c.conversation_type, c.conversation_id,
       COALESCE(n.name_value, person.name_value) AS conversation_name
FROM catalog c
LEFT JOIN latest_names n USING (platform, conversation_type, conversation_id)
LEFT JOIN LATERAL (
    SELECT name_value FROM public.identity_name_observations i
    WHERE c.conversation_type = 'private' AND i.platform = c.platform
      AND i.user_id = c.conversation_id AND i.name_kind = 'account_name'
    ORDER BY i.observed_at DESC, i.id DESC LIMIT 1
) person ON true
WHERE c.conversation_type IN ('group', 'private')
"""

def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute(CATALOG_SQL)
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chat_exporter') THEN
            GRANT SELECT ON archive.conversation_catalog_v1,
                archive.identity_name_timeline_v1 TO chat_exporter;
        END IF;
    END $$""")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'chat_exporter') THEN
            REVOKE SELECT ON archive.identity_name_timeline_v1 FROM chat_exporter;
        END IF;
    END $$""")
    op.execute("DROP VIEW archive.conversation_catalog_v1")
