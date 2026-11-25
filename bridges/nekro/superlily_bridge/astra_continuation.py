"""A private answer-writing turn using the original group's Lily model and preset."""

import ast
import re

CONTINUATION_PROMPT = (
    "这是 Astra 的解答，请你参考 Astra 的解答，对照原题，用自己的话组织回答并回复原消息，不要省略内容。"
)


def reply_body(response: str) -> tuple[str, bool]:
    """Accept prose or the normal Lily output call. Never run generated code."""
    body = response.strip()
    if not body:
        raise ValueError("莉莉续答为空")
    if not re.search(r"(?:submit_rendered_markdown|send_msg_text)\s*\(", body):
        return body, False
    fence = re.search(r"^```(?:py|python)?[ \t]*\n(.*)^```[ \t]*$", body, re.S | re.M)
    script = fence[1] if fence else body
    statements = ast.parse(script).body
    strings = {}
    for statement in statements[:-1]:
        if (
            not isinstance(statement, ast.Assign)
            or len(statement.targets) != 1
            or not isinstance(statement.targets[0], ast.Name)
            or not isinstance(statement.value, ast.Constant)
            or not isinstance(statement.value.value, str)
        ):
            raise ValueError("莉莉的排版调用需要明确的正文")
        strings[statement.targets[0].id] = statement.value.value
    call = statements[-1].value if statements and isinstance(statements[-1], ast.Expr) else None
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        raise ValueError("莉莉的回复缺少投递正文")
    render = call.func.id == "submit_rendered_markdown"
    if not render and call.func.id != "send_msg_text":
        raise ValueError("未知的续答格式")
    if call.keywords or len(call.args) != (1 if render else 2):
        raise ValueError("莉莉的回复参数不完整")
    arg = call.args[-1]
    text = strings.get(arg.id) if isinstance(arg, ast.Name) else getattr(arg, "value", None)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("莉莉续答为空")
    if fence:
        text = "\n\n".join(
            part.strip() for part in (body[: fence.start()], text, body[fence.end() :]) if part.strip()
        )
    return text.strip(), render


async def compose(chat_key, origin, visible, answer, sources):
    from nekro_agent.schemas.agent_ctx import AgentCtx
    from nekro_agent.models.db_chat_channel import DBChatChannel
    from nekro_agent.models.db_chat_message import DBChatMessage
    from nekro_agent.services.agent.creator import OpenAIChatMessage
    from nekro_agent.services.agent.run_agent import send_agent_request

    from . import render_prompt_policy

    channel = await DBChatChannel.get(chat_key=chat_key)
    config = await channel.get_effective_config()
    preset = await channel.get_preset()
    content = []
    for part in visible:
        if part["type"] == "input_image":
            content.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
        elif part["type"] == "input_text":
            content.append({"type": "text", "text": part["text"]})

    trigger = await DBChatMessage.filter(chat_key=chat_key, message_id=origin).order_by("-id").first()
    if trigger is not None:
        if not content:  # A ready job from before the continuation migration.
            content.append(
                {"type": "text", "text": trigger.parse_chat_history_prompt("", config, ref_mode=True)}
            )
        replies = (
            await DBChatMessage.filter(
                chat_key=chat_key,
                id__gt=trigger.id,
                is_recalled=False,
                ext_data__contains=origin,
            )
            .exclude(sender_id="-1")
            .order_by("id")
        )
        for reply in replies:
            if reply.ext_data_obj.ref_msg_id == origin:
                content.append(
                    {"type": "text", "text": reply.parse_chat_history_prompt("", config, ref_mode=True)}
                )
    if not content:
        raise ValueError("续答缺少原题")
    # Keep the answer in user-role material. Only the approved instruction and
    # the channel's actual persona are authoritative for this private turn.
    renderer = await render_prompt_policy(AgentCtx.create_by_db_chat_channel(channel))
    messages = [
        OpenAIChatMessage.from_text("system", "\n\n".join((preset.content, renderer, CONTINUATION_PROMPT))),
        OpenAIChatMessage("user", content),
        OpenAIChatMessage.from_text("user", "\n\n".join(filter(None, (answer, sources)))),
    ]
    result, _, _ = await send_agent_request(messages=messages, config=config, chat_key=chat_key)
    body, render = reply_body(result.response_content)
    return body, result.use_model, render
