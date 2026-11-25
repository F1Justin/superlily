"""Bridge Astra jobs into Nekro's existing QQ and WebUI facilities."""

import asyncio
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile

from nekro_agent.adapters.onebot_v11.core.bot import get_bot
from nekro_agent.adapters.utils import adapter_utils
from nekro_agent.api.core import logger
from nekro_agent.core.os_env import PROMPT_LOG_DIR
from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_exec_code import DBExecCode, ExecStopType

from .astra import MODEL


def progress(chat_key, origin, stage):
    logger.info(f"Astra {stage} · {chat_key} · 原消息 {origin}")


async def _sparkle(origin, enabled):
    result = await get_bot().call_api(
        "set_msg_emoji_like", message_id=int(origin), emoji_id=str(ord("✨")), set=enabled
    )
    if isinstance(result, dict) and (result.get("result") is False or result.get("errCode", 0)):
        raise RuntimeError("QQ 未确认星光表情")


async def add_sparkle(chat_key, origin):
    await _sparkle(origin, True)


async def clear_reaction(chat_key, origin):
    adapter = await adapter_utils.get_adapter_for_chat(chat_key)
    thinking = await adapter.set_message_reaction(origin, False)
    await _sparkle(origin, False)
    return thinking


def _log_path(chat_key, origin):
    digest = hashlib.sha256(f"{chat_key}/{origin}".encode()).hexdigest()
    return Path(PROMPT_LOG_DIR) / f"astra_{digest}.json"


async def save_prompt(chat_key, origin, request, result):
    channel = await DBChatChannel.get_channel(chat_key=chat_key)
    if not (await channel.get_effective_config()).SAVE_PROMPTS_LOG:
        return
    messages = [{"role": "system", "content": request["instructions"]}]
    for msg in request["input"]:
        content = []
        for part in msg["content"]:
            if part["type"] == "input_text":
                content.append({"type": "text", "text": part["text"]})
            elif part["type"] == "input_image":
                content.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
        messages.append({"role": msg["role"], "content": content})
    answer = "\n".join(
        p["text"]
        for item in result["output"]
        for p in item.get("content", [])
        if p.get("type") == "output_text"
    )
    actions = [item.get("action") for item in result["output"] if item.get("type") == "web_search_call"]
    log = {
        "request": {
            "messages": messages,
            **{k: v for k, v in request.items() if k not in ("instructions", "input")},
        },
        "response": {
            "id": result["id"],
            "content": answer,
            "usage": result["usage"],
            "web_search_actions": actions,
            "status": result["status"],
        },
    }

    def write():
        path = _log_path(chat_key, origin)
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as out:
            json.dump(log, out, ensure_ascii=False)
        os.replace(out.name, path)

    await asyncio.to_thread(write)


async def write_record(row):
    chat_key, origin = row["chat_key"], row["origin"]
    code = f"astra()  # QQ {origin}"
    if await DBExecCode.filter(chat_key=chat_key, use_model=MODEL, code_text=code).exists():
        return
    trigger = await DBChatMessage.filter(chat_key=chat_key, message_id=origin).first()
    answer = "\n".join(filter(None, (row["answer"], row["sources"])))
    started = row["queued_at"] or (trigger.send_timestamp if trigger else None)
    ended = row["ended_at"]
    if not answer and row["state"] == "done" and trigger and not row["queued_at"]:
        # Old queue entries cleared their outputs. Match only an actual reply to this origin.
        replies = await DBChatMessage.filter(
            chat_key=chat_key,
            sender_id="-1",
            is_recalled=False,
            send_timestamp__gte=trigger.send_timestamp,
            send_timestamp__lte=trigger.send_timestamp + 3600,
        ).order_by("send_timestamp")
        for reply in replies:
            if reply.ext_data_obj.ref_msg_id == origin:
                body = "\n".join(p.text for p in reply.parse_content_data() if getattr(p, "text", None))
                if body:
                    answer, ended = body, reply.send_timestamp
        if answer:
            answer = "历史发群回复\n" + answer
    usage = json.loads(row["usage"] or "{}")
    inputs, outputs = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    details = usage.get("input_tokens_details", {})
    actions = Counter(usage.get("web_search_actions", []))
    success = row["state"] == "done" and bool(row["response_id"]) and not row["error"]
    state = "已回复" if success else ("投递状态待核对" if row["state"] == "uncertain" else "深答未完成")
    summary = [f"{state} · 原消息 {origin}"]
    if row["error"]:
        summary.append(str(row["error"]))
    if usage:
        summary.append(
            f"搜索 {actions['search']} 次 · 输入 {inputs} / 输出 {outputs} token · 估算费用 ${row['usd']:.6f}"
        )
    if answer:
        summary.append(("Astra 原稿\n" if row["lily_model"] else "") + answer)
    if row["final_answer"]:
        summary.append("莉莉答复\n" + row["final_answer"])
    extra = {
        "token_input": inputs,
        "token_output": outputs,
        "token_consumption": inputs + outputs,
        "token_cached": details.get("cached_tokens", 0),
        "token_cache_write": details.get("cache_write_tokens", 0),
        "usage_cost": row["usd"] if usage else None,
        "web_search_offered": True,
        "web_search_requests": actions["search"],
        "server_tool_use": dict(actions),
        "response_id": row["response_id"],
        "origin_message_id": origin,
        "lily_model": row["lily_model"],
    }
    if _log_path(chat_key, origin).is_file():
        extra["log_path"] = str(_log_path(chat_key, origin))
    total = max(0, int((ended - started) * 1000)) if started and ended else 0
    generation = (
        max(0, int((row["generated_at"] - row["started_at"]) * 1000))
        if row["generated_at"] and row["started_at"]
        else 0
    )
    entry = await DBExecCode.create(
        chat_key=chat_key,
        code_text=code,
        use_model=MODEL,
        trigger_user_id=trigger.sender_id if trigger else "0",
        trigger_user_name=trigger.sender_name if trigger else "Astra",
        outputs="\n\n".join(summary),
        success=success,
        stop_type=ExecStopType.NORMAL if success else ExecStopType.ERROR,
        total_time_ms=total,
        generation_time_ms=generation,
        extra_data=json.dumps(extra, ensure_ascii=False),
    )
    if started:
        await DBExecCode.filter(id=entry.id).update(create_time=datetime.fromtimestamp(started, timezone.utc))
