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


def test_nekro_does_not_ack_before_plugin_signal_aggregation() -> None:
    bridge_path = ROOT / "bridges" / "nekro" / "superlily_bridge" / "__init__.py"
    module = ast.parse(bridge_path.read_text(encoding="utf-8"))
    handler = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "observe_user_message"
    )
    called_attributes = {
        node.func.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    returned_attributes = {
        node.value.attr
        for node in ast.walk(handler)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Attribute)
    }

    # Nekro only aggregates plugin signals after this hook returns, and a
    # later FORCE_TRIGGER may override BLOCK_TRIGGER.  Claim ACK here would
    # falsely certify that suppression is already installed.
    assert "acknowledge_claim" not in called_attributes
    assert "BLOCK_TRIGGER" in returned_attributes


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
