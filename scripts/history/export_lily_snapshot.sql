\set ON_ERROR_STOP on
COPY (
    SELECT row_to_json(export_row)::text
    FROM (
        SELECT
            message.id::text AS id,
            message.session_persist_id::text AS session_persist_id,
            to_char(message.time, 'YYYY-MM-DD HH24:MI:SS.US') AS time,
            message.type,
            message.message_id,
            message.message,
            message.plain_text,
            scene.scene_id,
            scene.scene_type,
            bot.self_id AS bot_id,
            CASE
                WHEN message.type = 'message_sent' THEN bot.self_id
                ELSE source_user.user_id
            END AS sender_id,
            CASE
                WHEN message.type = 'message_sent' THEN bot.self_id
                ELSE COALESCE(
                    session.member_data ->> 'card',
                    session.member_data ->> 'nickname',
                    source_user.user_data ->> 'card',
                    source_user.user_data ->> 'nickname',
                    source_user.user_data ->> 'name'
                )
            END AS sender_name,
            session.member_data,
            source_user.user_data
        FROM nonebot_plugin_chatrecorder_messagerecord_v2 AS message
        LEFT JOIN nonebot_plugin_uninfo_sessionmodel AS session
          ON session.id = message.session_persist_id
        LEFT JOIN nonebot_plugin_uninfo_botmodel AS bot
          ON bot.id = session.bot_persist_id
        LEFT JOIN nonebot_plugin_uninfo_scenemodel AS scene
          ON scene.id = session.scene_persist_id
        LEFT JOIN nonebot_plugin_uninfo_usermodel AS source_user
          ON source_user.id = session.user_persist_id
        ORDER BY message.id
    ) AS export_row
) TO STDOUT WITH (
    FORMAT CSV,
    DELIMITER E'\x03',
    QUOTE E'\x01',
    ESCAPE E'\x02'
);
