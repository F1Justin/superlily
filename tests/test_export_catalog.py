from __future__ import annotations

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from test_history_archive_migration import _run_alembic


def test_metadata_catalog_names_and_consumer_permissions():
    url = os.environ['SUPERLILY_TEST_DATABASE_URL']
    parsed = make_url(url)
    assert parsed.host in {'127.0.0.1', 'localhost'} and parsed.database.endswith('_test')
    _run_alembic({**os.environ, 'SUPERLILY_DATABASE_URL': url}, 'upgrade', 'head')

    async def check():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as c:
                await c.execute(text("""INSERT INTO archive.conversation_mappings
                    (id,source_system,source_conversation_key,source_conversation_type,
                     platform,conversation_type,conversation_id,mapping_version,mapping_status,active,source_metadata_json)
                    VALUES ('catalog-map','nekro.chat_message','old-group','group','qq','group','catalog-old',1,'mapped',true,'{}')"""))
                await c.execute(text("""INSERT INTO conversation_name_observations
                    (id,platform,conversation_type,conversation_id,name_value,observed_at,
                     source_system,source_record_type,source_record_id,observation_method,provenance_json)
                    VALUES ('catalog-name','qq','group','catalog-new','已记录群名',now(),
                     'test','test','catalog-name','test','{}')"""))
                rows = (await c.execute(text("SELECT conversation_id,conversation_name FROM archive.conversation_catalog_v1 WHERE conversation_id LIKE 'catalog-%'"))).all()
                assert dict(rows) == {'catalog-old': None, 'catalog-new': '已记录群名'}
                await c.execute(text('CREATE ROLE chat_exporter'))
                await c.execute(text('GRANT USAGE ON SCHEMA archive TO chat_exporter'))
                await c.execute(text('GRANT SELECT ON archive.conversation_catalog_v1, archive.identity_name_timeline_v1 TO chat_exporter'))
                await c.execute(text('SET LOCAL ROLE chat_exporter'))
                assert len((await c.execute(text("SELECT * FROM archive.conversation_catalog_v1 WHERE conversation_id LIKE 'catalog-%'"))).all()) == 2
                assert not await c.scalar(text("SELECT has_table_privilege(current_user,'public.identity_name_observations','SELECT')"))
                assert not await c.scalar(text("SELECT has_table_privilege(current_user,'archive.legacy_messages','SELECT')"))
                await c.rollback()
        finally:
            await engine.dispose()
    asyncio.run(check())
