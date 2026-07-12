from __future__ import annotations

import hashlib
import json
from itertools import product
import re
from typing import Any, Iterable
import weakref


def _canonical_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    encoded = {
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")): row
        for row in rows
    }
    return [encoded[key] for key in sorted(encoded)]


def _command_triggers(
    commands: Iterable[Iterable[Any]],
    command_starts: tuple[str, ...],
    command_separators: tuple[str, ...],
) -> list[str]:
    triggers: set[str] = set()
    for command in commands:
        parts = tuple(str(part) for part in command)
        if len(parts) == 1:
            triggers.update(f"{start}{parts[0]}" for start in command_starts)
        else:
            triggers.update(
                f"{start}{separator.join(parts)}"
                for start, separator in product(command_starts, command_separators)
            )
    return sorted(triggers)


def _alconna_triggers(checker: Any) -> tuple[list[str], list[tuple[str, int]], bool]:
    command_ref = getattr(checker, "command", None)
    command = command_ref() if isinstance(command_ref, weakref.ReferenceType) else command_ref
    if command is None:
        return [], [], False

    prefixes = [str(item) for item in getattr(command, "prefixes", ()) if isinstance(item, str)] or [""]
    name = str(getattr(command, "command", "") or "")
    literal = {f"{prefix}{name}" for prefix in prefixes if name}
    regex_shortcuts: list[tuple[str, int]] = []
    try:
        shortcuts = command._get_shortcuts()
    except Exception:
        shortcuts = {}
        shortcuts_complete = False
    else:
        shortcuts_complete = True
    if isinstance(shortcuts, dict):
        for raw_key, shortcut in shortcuts.items():
            key = str(raw_key)
            shortcut_prefixes = [
                str(item) for item in getattr(shortcut, "prefixes", ()) if isinstance(item, str)
            ] or [""]
            if re.search(r"[\\.^$*+?{}\[\]|()]", key):
                for prefix in shortcut_prefixes:
                    regex_shortcuts.append(
                        (f"^{re.escape(prefix)}(?:{key})", int(getattr(shortcut, "flags", 0) or 0))
                    )
            else:
                literal.update(f"{prefix}{key}" for prefix in shortcut_prefixes)
    return (
        sorted(item for item in literal if item.strip()),
        sorted(set(regex_shortcuts)),
        shortcuts_complete,
    )


def _candidate_payload(
    plugin: Any,
    matcher: Any,
    *,
    kind: str,
    triggers: list[str],
    ignore_case: bool | None = None,
    regex_flags: int | None = None,
    complete: bool,
    rule_checker_count: int,
    unknown_rule_checkers: list[str],
    permission_checker_count: int,
) -> dict[str, Any]:
    matcher_type = str(getattr(matcher, "type", "") or "message")
    return {
        "plugin_id": str(plugin.id_),
        "module_name": str(plugin.module_name),
        "matcher_type": matcher_type,
        "kind": kind,
        "triggers": triggers,
        "priority": getattr(matcher, "priority", None),
        "block": getattr(matcher, "block", None),
        "ignore_case": ignore_case,
        "regex_flags": regex_flags,
        "complete": complete,
        "rule_checker_count": rule_checker_count,
        "unknown_rule_checkers": unknown_rule_checkers,
        "permission_checker_count": permission_checker_count,
    }


def _checker_label(checker: Any) -> str:
    class_name = checker.__class__.__name__
    if class_name != "function":
        return class_name
    module = str(getattr(checker, "__module__", "") or "")
    qualname = str(getattr(checker, "__qualname__", getattr(checker, "__name__", "function")))
    return f"{module}.{qualname}".strip(".")[:512]


def _matcher_candidates(
    plugin: Any,
    matcher: Any,
    command_starts: tuple[str, ...],
    command_separators: tuple[str, ...],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    rule = getattr(matcher, "rule", None)
    rule_checkers = tuple(getattr(rule, "checkers", ()))
    known_checker_names = {
        "AlconnaRule",
        "CommandRule",
        "ShellCommandRule",
        "FullmatchRule",
        "StartswithRule",
        "EndswithRule",
        "KeywordsRule",
        "RegexRule",
    }
    checker_labels = sorted(
        _checker_label(getattr(dependent, "call", None))
        for dependent in rule_checkers
        if getattr(dependent, "call", None) is not None
    )
    unknown_rule_checkers = sorted(
        label for label in checker_labels if label.rsplit(".", 1)[-1] not in known_checker_names
    )
    permission_checker_count = len(getattr(getattr(matcher, "permission", None), "checkers", ()))
    complete = (
        len(rule_checkers) == 1
        and not unknown_rule_checkers
        and permission_checker_count == 0
    )
    candidate_context = {
        "complete": complete,
        "rule_checker_count": len(rule_checkers),
        "unknown_rule_checkers": unknown_rule_checkers,
        "permission_checker_count": permission_checker_count,
    }
    for dependent in rule_checkers:
        checker = getattr(dependent, "call", None)
        if checker is None:
            continue
        checker_name = checker.__class__.__name__
        kind: str | None = None
        triggers: list[str] = []
        ignore_case: bool | None = None
        regex_flags: int | None = None
        if checker_name == "AlconnaRule":
            literal, regex_shortcuts, shortcuts_complete = _alconna_triggers(checker)
            alconna_context = dict(candidate_context)
            if not shortcuts_complete:
                alconna_context["complete"] = False
                alconna_context["unknown_rule_checkers"] = sorted(
                    {*unknown_rule_checkers, "AlconnaShortcutsUnavailable"}
                )
            literal = [item for item in literal if len(item) <= 2_048]
            if literal:
                candidates.append(
                    _candidate_payload(
                        plugin,
                        matcher,
                        kind="token",
                        triggers=literal,
                        **alconna_context,
                    )
                )
            for regex, flags in regex_shortcuts:
                if len(regex) > 2_048:
                    continue
                candidates.append(
                    _candidate_payload(
                        plugin,
                        matcher,
                        kind="regex",
                        triggers=[regex],
                        regex_flags=flags,
                        **alconna_context,
                    )
                )
            continue
        if checker_name in {"CommandRule", "ShellCommandRule"}:
            kind = "command"
            triggers = _command_triggers(
                getattr(checker, "cmds", ()),
                command_starts,
                command_separators,
            )
        elif checker_name == "FullmatchRule":
            kind = "exact"
            triggers = [str(item) for item in getattr(checker, "msg", ())]
            ignore_case = bool(getattr(checker, "ignorecase", False))
        elif checker_name == "StartswithRule":
            kind = "prefix"
            triggers = [str(item) for item in getattr(checker, "msg", ())]
            ignore_case = bool(getattr(checker, "ignorecase", False))
        elif checker_name == "EndswithRule":
            kind = "suffix"
            triggers = [str(item) for item in getattr(checker, "msg", ())]
            ignore_case = bool(getattr(checker, "ignorecase", False))
        elif checker_name == "KeywordsRule":
            kind = "contains"
            triggers = [str(item) for item in getattr(checker, "keywords", ())]
        elif checker_name == "RegexRule":
            kind = "regex"
            regex = getattr(checker, "regex", None)
            if regex:
                triggers = [str(getattr(regex, "pattern", regex))]
                regex_flags = int(getattr(regex, "flags", getattr(checker, "flags", 0)) or 0)
        triggers = sorted(
            {
                item.strip()
                for item in triggers
                if item and item.strip() and len(item.strip()) <= 2_048
            }
        )
        if kind is None or not triggers:
            continue
        candidates.append(
            _candidate_payload(
                plugin,
                matcher,
                kind=kind,
                triggers=triggers,
                ignore_case=ignore_case,
                regex_flags=regex_flags,
                **candidate_context,
            )
        )
    return candidates


def collect_runtime_registry(
    plugins: Iterable[Any] | None = None,
    *,
    command_starts: Iterable[str] | None = None,
    command_separators: Iterable[str] | None = None,
) -> dict[str, Any]:
    if plugins is None:
        from nonebot.plugin import get_loaded_plugins

        plugins = get_loaded_plugins()
    if command_starts is None or command_separators is None:
        try:
            from nonebot import get_driver

            config = get_driver().config
            command_starts = command_starts or config.command_start
            command_separators = command_separators or config.command_sep
        except Exception:
            command_starts = command_starts or ("",)
            command_separators = command_separators or (".",)
    active_starts = tuple(sorted(str(item) for item in command_starts)) or ("",)
    active_separators = tuple(sorted(str(item) for item in command_separators)) or (".",)

    plugin_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for plugin in sorted(plugins, key=lambda item: (str(item.id_), str(item.module_name))):
        matchers = sorted(
            getattr(plugin, "matcher", ()),
            key=lambda item: (
                str(getattr(item, "module", "")),
                int(getattr(item, "priority", 0) or 0),
                str(getattr(item, "type", "")),
            ),
        )
        candidates_by_matcher = [
            _matcher_candidates(plugin, matcher, active_starts, active_separators)
            for matcher in matchers
        ]
        plugin_candidates = [candidate for matcher_candidates in candidates_by_matcher for candidate in matcher_candidates]
        candidates.extend(plugin_candidates)
        metadata = getattr(plugin, "metadata", None)
        display_name = getattr(metadata, "name", None) if metadata is not None else None
        plugin_rows.append(
            {
                "plugin_id": str(plugin.id_),
                "module_name": str(plugin.module_name),
                "display_name": str(display_name) if display_name else None,
                "matcher_count": len(matchers),
                "classified_matcher_count": sum(bool(item) for item in candidates_by_matcher),
            }
        )

    plugin_rows = _canonical_rows(plugin_rows)
    candidate_rows = _canonical_rows(candidates)
    material = {"plugins": plugin_rows, "candidates": candidate_rows}
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        **material,
        "snapshot_hash": hashlib.sha256(encoded).hexdigest(),
    }
