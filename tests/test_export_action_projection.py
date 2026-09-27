import json
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import test_history_archive_migration as archive


@pytest.mark.asyncio
async def test_postgres_projection_preserves_operation_media_and_target_identity():
    url = os.getenv('SUPERLILY_TEST_DATABASE_URL')
    if not url:
        pytest.skip('isolated PostgreSQL required')
    assert url.rsplit('/', 1)[-1].endswith('_test')
    archive._run_alembic({**os.environ, 'SUPERLILY_DATABASE_URL': url}, 'upgrade', 'head')
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await archive._insert_archive_behavior_fixture_postgres(connection)
            await connection.execute(text('''
                UPDATE public.event_observations SET segments_json=CAST(:segments AS json)
                WHERE id=:id
            '''), {'id': archive.BEHAVIOR_OBSERVATION_A_ID,
                   'segments': json.dumps([{'type':'image','data':{'summary':'[动画表情]','sub_type':1}}])})
            await connection.execute(text('''
                INSERT INTO public.platform_action_observations
                (id, observation_id, observer_instance_id, action_index, action_kind,
                 operation, actor_principal_id, target_platform_message_id,
                 target_conversation_id, target_conversation_type, target_source_event_id,
                 resolver_status, value_json, capture_status, occurred_at)
                VALUES ('projection-action', :observation, :instance, 0, 'reaction',
                        'remove', 'actor', 'target-local', 'history-group-a', 'group', :target,
                        'resolved', CAST(:value AS json), 'complete', now())
            '''), {'observation': archive.BEHAVIOR_OBSERVATION_B_ID,
                   'instance': archive.BEHAVIOR_INSTANCE_B_ID,
                   'value': json.dumps({'emoji_id': '212', 'count': 2}),
                   'target': archive.BEHAVIOR_SOURCE_EVENT_A_ID})
            actions = json.loads(await connection.scalar(text('''
                SELECT actions_json FROM archive.message_timeline_v2
                WHERE kind='core_observation' AND source_record_id=:id LIMIT 1
            '''), {'id': archive.BEHAVIOR_OBSERVATION_B_ID}))
            assert actions[0]['operation'] == 'remove'
            assert actions[0]['value']['count'] == 2
            assert actions[0]['target_event'] == archive.BEHAVIOR_SOURCE_EVENT_A_ID
            assert actions[0]['target_sender'] == 'sender-history-a'
            assert actions[0]['target_segments'][0]['data']['summary'] == '[动画表情]'
            priority = await connection.scalar(text('''
                SELECT display_priority FROM archive.message_timeline_v2
                WHERE kind='core_observation' AND source_record_id=:id LIMIT 1
            '''), {'id': archive.BEHAVIOR_OBSERVATION_A_ID})
            assert priority < 0
            await connection.rollback()
    finally:
        await engine.dispose()
