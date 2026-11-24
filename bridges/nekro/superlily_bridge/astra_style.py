"""Find Lily's actual replies when the current history contains no style sample."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from tortoise.expressions import Q

from nekro_agent.core.os_env import OsEnv
from nekro_agent.models.db_chat_message import DBChatMessage


def _history_headers(visible):
    for part in visible:
        if part.get("type") != "input_text":
            continue
        text = part["text"]
        if text.startswith("Reply Focus ("):
            text = text.partition("Quoted message:\n")[2].partition("\nCurrent request:")[0]
        yield text


def _visible_body(header):
    body = header.partition(" 说: ")[2] or header.partition(" said: ")[2]
    body = re.sub(r"<Image:[^>]*>|\[Image:[^\]]*\]", "", body).strip()
    return body and not body.startswith(("INTERNAL_", "(content too long, omitted)"))


async def _has_lily_reply(chat_key, visible, bot_id):
    ids = set()
    for header in _history_headers(visible):
        if not _visible_body(header):
            continue
        # Match the outer record header, not a quoted user's text or nickname.
        match = re.match(r"^(?:\(msg_id:([^ )]+)[^)]*\))?\[[^]\n]*? id:(\d+)\]", header)
        if match:
            if bot_id and match[2] == str(bot_id):
                return True
            if match[1]:
                ids.add(match[1])
        elif match := re.match(r"^\(msg_id:([^ )]+)\)", header):
            ids.add(match[1])
    if not ids:
        return False
    return (
        await DBChatMessage.filter(
            chat_key=chat_key, message_id__in=list(ids), sender_id="-1", is_recalled=False
        )
        .exclude(sender_name="SYSTEM")
        .exists()
    )


def _reply_text(row):
    parts = []
    for segment in row.parse_content_data():
        if segment.type not in ("text", "image"):
            continue
        text = (segment.text or "").strip()
        if segment.type == "image" and (not text or text.startswith("[Image:")):
            index = (
                Path(OsEnv.DATA_DIR).resolve() / "plugin_data/Superlily.core_bridge/render-history.sqlite3"
            )
            if index.is_file():
                with closing(sqlite3.connect(f"{index.as_uri()}?mode=ro", uri=True)) as db:
                    result = db.execute(
                        "SELECT body FROM transcripts WHERE chat=? AND message=? AND filename=?",
                        (row.chat_key, row.message_id, segment.file_name),
                    ).fetchone()
                text = result[0] if result else ""
        if text and not text.startswith(("INTERNAL_", "[Image:", "图片转述：")):
            parts.append(text)
    return "\n".join(parts).strip()


def _before(row):
    return Q(send_timestamp__lt=row.send_timestamp) | Q(send_timestamp=row.send_timestamp, id__lt=row.id)


async def with_lily_examples(chat_key, visible, origin, bot_id):
    if await _has_lily_reply(chat_key, visible, bot_id):
        return visible
    cutoff = await DBChatMessage.filter(chat_key=chat_key, message_id=origin).order_by("-id").first()
    if cutoff is None:
        return visible
    turns = {}
    replies = (
        DBChatMessage.filter(chat_key=chat_key, sender_id="-1", is_recalled=False)
        .exclude(message_id="")
        .exclude(sender_name="SYSTEM")
    )
    while True:
        rows = await replies.filter(_before(cutoff)).order_by("-send_timestamp", "-id").limit(24)
        for row in rows:
            body = _reply_text(row)
            if not body:
                continue
            turn = row.ext_data_obj.ref_msg_id
            if not turn:
                previous = (
                    await DBChatMessage.filter(_before(row), chat_key=chat_key)
                    .exclude(sender_id="-1")
                    .order_by("-send_timestamp", "-id")
                    .first()
                )
                turn = previous.message_id if previous else row.message_id
            if turn not in turns and len(turns) == 3:
                break
            turns.setdefault(turn, []).append(body)
        else:
            if len(rows) == 24:
                cutoff = rows[-1]
                continue
        break
    if not turns:
        return visible
    samples = ["\n".join(reversed(parts)) for parts in reversed(list(turns.values()))]
    return [{"type": "input_text", "text": "莉莉以往的回复（语气示例）\n\n" + "\n\n".join(samples)}, *visible]
