\set ON_ERROR_STOP on
COPY (
    SELECT row_to_json(export_row)::text
    FROM (
        SELECT
            id::text AS id,
            sender_id,
            sender_name,
            sender_nickname,
            is_tome,
            is_recalled,
            adapter_key,
            message_id,
            chat_key,
            chat_type,
            platform_userid,
            content_text,
            content_data,
            raw_cq_code,
            ext_data,
            send_timestamp,
            create_time,
            update_time
        FROM chat_message AS message
        ORDER BY message.id
    ) AS export_row
) TO STDOUT WITH (
    FORMAT CSV,
    DELIMITER E'\x03',
    QUOTE E'\x01',
    ESCAPE E'\x02'
);
