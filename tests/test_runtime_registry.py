from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
import weakref

from superlily_core.command_registry import runtime_registry_snapshot_hash


def _load_runtime_registry():
    path = Path("bridges/lily_nonebot/lily_core_bridge/runtime_registry.py")
    spec = spec_from_file_location("lily_runtime_registry_test", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect_runtime_registry = _load_runtime_registry().collect_runtime_registry


def _checker(name: str, **attributes):
    checker_type = type(name, (), {})
    checker = checker_type()
    for key, value in attributes.items():
        setattr(checker, key, value)
    return SimpleNamespace(call=checker)


def test_runtime_registry_collects_commands_and_regexes_deterministically() -> None:
    command_matcher = SimpleNamespace(
        module="plugins.demo",
        type="message",
        priority=10,
        block=True,
        permission=SimpleNamespace(checkers=[]),
        rule=SimpleNamespace(checkers=[_checker("CommandRule", cmds=(("demo",), ("alias",)))]),
    )
    regex_matcher = SimpleNamespace(
        module="plugins.demo",
        type="message",
        priority=20,
        block=False,
        permission=SimpleNamespace(checkers=[]),
        rule=SimpleNamespace(checkers=[_checker("RegexRule", regex="^demo\\d+$")]),
    )

    class FakeAlconna:
        command = "train"
        prefixes = [""]

        def _get_shortcuts(self):
            return {"列车信息": SimpleNamespace(prefixes=[], flags=0)}

    alconna = FakeAlconna()
    alconna_matcher = SimpleNamespace(
        module="plugins.demo",
        type="message",
        priority=30,
        block=True,
        permission=SimpleNamespace(checkers=[]),
        rule=SimpleNamespace(checkers=[_checker("AlconnaRule", command=weakref.ref(alconna))]),
    )
    plugin = SimpleNamespace(
        id_="demo",
        module_name="plugins.demo",
        matcher=[command_matcher, regex_matcher, alconna_matcher],
        metadata=SimpleNamespace(name="Demo"),
    )

    first = collect_runtime_registry([plugin], command_starts=("",), command_separators=(".",))
    second = collect_runtime_registry([plugin], command_starts=("",), command_separators=(".",))

    assert first == second
    assert len(first["snapshot_hash"]) == 64
    assert first["snapshot_hash"] == runtime_registry_snapshot_hash(first["plugins"], first["candidates"])
    assert first["plugins"] == [
        {
            "plugin_id": "demo",
            "module_name": "plugins.demo",
            "display_name": "Demo",
            "matcher_count": 3,
            "classified_matcher_count": 3,
        }
    ]
    assert {item["kind"] for item in first["candidates"]} == {"command", "token", "regex"}
    assert any(item["triggers"] == ["alias", "demo"] for item in first["candidates"])
    assert any(item["triggers"] == ["train", "列车信息"] for item in first["candidates"])
    assert all(item["complete"] is True for item in first["candidates"])


def test_runtime_registry_marks_composite_or_permissioned_matchers_incomplete() -> None:
    matcher = SimpleNamespace(
        module="plugins.demo",
        type="message",
        priority=10,
        block=True,
        permission=SimpleNamespace(checkers=[object()]),
        rule=SimpleNamespace(
            checkers=[
                _checker("CommandRule", cmds=(("demo",),)),
                _checker("CustomRule"),
            ]
        ),
    )
    plugin = SimpleNamespace(
        id_="demo",
        module_name="plugins.demo",
        matcher=[matcher],
        metadata=None,
    )

    snapshot = collect_runtime_registry([plugin], command_starts=("",), command_separators=(".",))

    assert snapshot["candidates"] == [
        {
            "plugin_id": "demo",
            "module_name": "plugins.demo",
            "matcher_type": "message",
            "kind": "command",
            "triggers": ["demo"],
            "priority": 10,
            "block": True,
            "ignore_case": None,
            "regex_flags": None,
            "complete": False,
            "rule_checker_count": 2,
            "unknown_rule_checkers": ["CustomRule"],
            "permission_checker_count": 1,
        }
    ]


def test_runtime_registry_hash_uses_core_canonical_order_for_multiple_plugins() -> None:
    command_matcher = SimpleNamespace(
        module="plugins.alpha",
        type="message",
        priority=10,
        block=True,
        permission=SimpleNamespace(checkers=[]),
        rule=SimpleNamespace(checkers=[_checker("CommandRule", cmds=(("alpha",),))]),
    )
    alpha = SimpleNamespace(
        id_="alpha",
        module_name="plugins.alpha",
        matcher=[command_matcher],
        metadata=None,
    )
    zed = SimpleNamespace(
        id_="zed",
        module_name="plugins.zed",
        matcher=[],
        metadata=None,
    )

    snapshot = collect_runtime_registry(
        [alpha, zed], command_starts=("",), command_separators=(".",)
    )

    assert snapshot["snapshot_hash"] == runtime_registry_snapshot_hash(
        snapshot["plugins"], snapshot["candidates"]
    )
