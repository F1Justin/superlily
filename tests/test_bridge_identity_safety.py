import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _load_payloads(name: str, relative_path: str):
    spec = spec_from_file_location(name, ROOT / relative_path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_nekro_ack_follows_authoritative_outbound_guard_install() -> None:
    bridge_path = ROOT / "bridges" / "nekro" / "superlily_bridge" / "__init__.py"
    source = bridge_path.read_text(encoding="utf-8")
    module = ast.parse(source)
    handler = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "observe_user_message"
    )
    handler_source = ast.get_source_segment(source, handler)
    assert handler_source is not None
    returned_attributes = {
        node.value.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Attribute)
    }
    send_guard = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "suppress_denied_send"
    )
    send_guard_source = ast.get_source_segment(source, send_guard)
    installer = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "_install_claim_suppression"
    )
    installer_source = ast.get_source_segment(source, installer)

    assert handler_source.index("_install_claim_suppression") < handler_source.index(
        "acknowledge_claim"
    )
    assert "if authoritative and suppression is not None" in handler_source
    assert "BLOCK_TRIGGER" in returned_attributes
    assert send_guard_source is not None and "MockApiException" in send_guard_source
    assert "_match_claim_suppression" in send_guard_source
    assert installer_source is not None and "prior_send_seen" in installer_source
    assert "return suppression, not prior_send_seen" in installer_source


@pytest.fixture(params=["lily", "nekro"])
def bridge_payloads(request):
    if request.param == "lily":
        return _load_payloads(
            "superlily_lily_identity_safety",
            "bridges/lily_nonebot/lily_core_bridge/payloads.py",
        )
    return _load_payloads(
        "superlily_nekro_identity_safety",
        "bridges/nekro/superlily_bridge/payloads.py",
    )


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "https://example.test/resource",
        "mqqapi://qzoneschema/feed",
        "file:///home/user/private",
        "napcat-resource:opaque-id",
        "mailto:user@example.test",
        "data:image/png;base64,secret",
        "base64://secret",
    ],
)
def test_bridge_native_identity_rejects_every_uri_scheme(bridge_payloads, unsafe_value: str) -> None:
    identity = bridge_payloads.native_message_identity(
        {
            "message_id": "ordinary-id",
            "msg_uid": unsafe_value,
        }
    )

    assert identity == {
        "schema": "onebot_v11.qq.native_identity.v1",
        "message_id": "ordinary-id",
    }


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "https://example.test/resource",
        "mqqapi://qzoneschema/feed",
        "file:///home/user/private",
        "napcat-resource:opaque-id",
        "mailto:user@example.test",
        "data:image/png;base64,secret",
        "base64://secret",
    ],
)
def test_lily_attachment_platform_id_rejects_every_uri_scheme(unsafe_value: str) -> None:
    payloads = _load_payloads(
        "superlily_lily_attachment_safety",
        "bridges/lily_nonebot/lily_core_bridge/payloads.py",
    )

    _, _, attachments = payloads.message_segments(
        [{"type": "image", "data": {"file": unsafe_value}}]
    )

    assert attachments[0]["platform_id"] is None
