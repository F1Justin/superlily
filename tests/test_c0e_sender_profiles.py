import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from superlily_contracts.models import SenderRef


ROOT = Path(__file__).parents[1]
BRIDGES = [
    ("bridges/lily_nonebot/lily_core_bridge/__init__.py", "_observe_event"),
    ("bridges/nekro/superlily_bridge/__init__.py", "_observe_user_message"),
]


@pytest.fixture(params=BRIDGES)
def profile_mapping(request):
    path, handler_name = request.param
    module = ast.parse((ROOT / path).read_text())
    helper = next(node for node in module.body if isinstance(node, ast.FunctionDef)
                  and node.name == "_sender_profile_text")
    handler = next(node for node in module.body if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == handler_name)
    fields = next(node for node in ast.walk(handler) if isinstance(node, ast.Dict)
                  and {"title", "level"}.issubset(
                      {key.value for key in node.keys if isinstance(key, ast.Constant)}))
    selected = [(key, value) for key, value in zip(fields.keys, fields.values, strict=True)
                if isinstance(key, ast.Constant) and key.value in {"title", "level"}]
    namespace = {"Any": Any}
    # Exercise the production mapping without registering live NoneBot hooks or starting reporters.
    exec(compile(ast.Module(body=[helper], type_ignores=[]), path, "exec"), namespace)
    expression = compile(ast.Expression(ast.Dict(
        keys=[key for key, _ in selected], values=[value for _, value in selected],
        lineno=fields.lineno, col_offset=fields.col_offset,
    )), path, "eval")

    def evaluate(sender):
        return eval(expression, namespace, {
            "sender_obj": SimpleNamespace(**sender), "current_sender": sender,
        })

    return evaluate


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        ({"title": " 群之龙王 ", "level": " 42 "}, {"title": "群之龙王", "level": "42"}),
        ({}, {"title": None, "level": None}),
        ({"title": "", "level": None}, {"title": None, "level": None}),
        ({"title": "成员", "level": 0}, {"title": "成员", "level": "0"}),
        ({"title": {"unexpected": "value"}, "level": False}, {"title": None, "level": None}),
        ({"title": "长" * 600, "level": "1" * 600}, {"title": "长" * 512, "level": "1" * 512}),
    ],
)
def test_message_sender_fields_preserve_only_supplied_bounded_values(profile_mapping, sender, expected):
    fields = profile_mapping(sender)
    assert fields == expected
    contract = SenderRef.model_validate({"id": "123", **fields})
    assert contract.title == expected["title"]
    assert contract.level == expected["level"]
