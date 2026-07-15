from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import tomllib
from collections.abc import Iterable
from typing import Any, Literal

DEFAULT_COMMAND_REGISTRY_PATH = "apps/core/config/command_registry.toml"
CommandRuleKind = Literal["command", "token", "prefix", "exact", "suffix", "contains", "regex"]
COMMAND_PERMISSIONS = {"public", "group_admin", "superuser"}


def runtime_registry_snapshot_hash(
    plugins: Iterable[dict[str, Any]],
    candidates: Iterable[dict[str, Any]],
) -> str:
    def canonical_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        encoded = {
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")): row
            for row in rows
        }
        return [encoded[key] for key in sorted(encoded)]

    material = {
        "plugins": canonical_rows(plugins),
        "candidates": canonical_rows(candidates),
    }
    encoded = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CommandRule:
    id: str
    kind: CommandRuleKind
    triggers: tuple[str, ...]
    target_instance_id: str
    source_plugin: str
    confidence: int = 95
    permission: str = "public"
    sensitive: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class CommandMatch:
    rule_id: str
    kind: CommandRuleKind
    trigger: str
    target_instance_id: str
    source_plugin: str
    confidence: int
    permission: str
    sensitive: bool
    description: str

    def as_feature(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "kind": self.kind,
            "trigger": self.trigger,
            "target_instance_id": self.target_instance_id,
            "source_plugin": self.source_plugin,
            "confidence": self.confidence,
            "permission": self.permission,
            "sensitive": self.sensitive,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class CommandRegistry:
    version: str
    rules: tuple[CommandRule, ...]

    @classmethod
    def empty(cls, version: str = "empty") -> "CommandRegistry":
        return cls(version=version, rules=())

    def match(self, text: str | None) -> CommandMatch | None:
        if text is None:
            return None
        raw = text.strip()
        normalized = raw.casefold()
        if not normalized:
            return None

        for rule in self.rules:
            for trigger in rule.triggers:
                if rule.kind == "prefix" and normalized.startswith(trigger.strip().casefold()):
                    return _match_from_rule(rule, trigger)
                if rule.kind == "command" and normalized.startswith(trigger.strip().casefold()):
                    return self._longest_command_match(normalized)
                if rule.kind == "token" and _matches_token(normalized, trigger):
                    return _match_from_rule(rule, trigger)
                if rule.kind == "exact" and normalized == trigger.strip().casefold():
                    return _match_from_rule(rule, trigger)
                if rule.kind == "suffix" and normalized.endswith(trigger.strip().casefold()):
                    return _match_from_rule(rule, trigger)
                if rule.kind == "contains" and trigger.strip().casefold() in normalized:
                    return _match_from_rule(rule, trigger)
                if rule.kind == "regex" and re.match(trigger, raw, flags=re.IGNORECASE | re.DOTALL):
                    return _match_from_rule(rule, trigger)
        return None

    def _longest_command_match(self, normalized: str) -> CommandMatch:
        """Mirror NoneBot's global command trie without changing rule priority.

        Once registry order reaches the first matching command rule, command
        matchers take the longest matching command prefix across the registry.
        Equal-length ties retain registry/trigger order.
        """

        matches = [
            (rule, trigger)
            for rule in self.rules
            if rule.kind == "command"
            for trigger in rule.triggers
            if normalized.startswith(trigger.strip().casefold())
        ]
        rule, trigger = max(matches, key=lambda item: len(item[1].strip()))
        return _match_from_rule(rule, trigger)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "rules": [
                {
                    "id": rule.id,
                    "kind": rule.kind,
                    "triggers": list(rule.triggers),
                    "target_instance_id": rule.target_instance_id,
                    "source_plugin": rule.source_plugin,
                    "confidence": rule.confidence,
                    "permission": rule.permission,
                    "sensitive": rule.sensitive,
                    "description": rule.description,
                }
                for rule in self.rules
            ],
        }

    def active_for_runtime_plugins(self, plugins: Iterable[dict[str, Any]]) -> "CommandRegistry":
        aliases = runtime_plugin_aliases(plugins)
        return CommandRegistry(
            version=self.version,
            rules=tuple(rule for rule in self.rules if source_plugin_loaded(rule.source_plugin, aliases)),
        )


def _match_from_rule(rule: CommandRule, trigger: str) -> CommandMatch:
    return CommandMatch(
        rule_id=rule.id,
        kind=rule.kind,
        trigger=trigger,
        target_instance_id=rule.target_instance_id,
        source_plugin=rule.source_plugin,
        confidence=rule.confidence,
        permission=rule.permission,
        sensitive=rule.sensitive,
        description=rule.description,
    )


def _matches_token(normalized_text: str, trigger: str) -> bool:
    normalized_trigger = trigger.strip().casefold()
    if not normalized_trigger:
        return False
    if normalized_text == normalized_trigger:
        return True
    if not normalized_text.startswith(normalized_trigger):
        return False
    return normalized_text[len(normalized_trigger)].isspace()


def runtime_plugin_aliases(plugins: Iterable[dict[str, Any]]) -> set[str]:
    aliases: set[str] = set()
    for plugin in plugins:
        for raw in (plugin.get("plugin_id"), plugin.get("module_name")):
            if not raw:
                continue
            value = str(raw).casefold()
            aliases.add(value)
            aliases.add(value.rsplit(".", 1)[-1])
    return aliases


def source_plugin_loaded(source_plugin: str, aliases: set[str]) -> bool:
    source = source_plugin.casefold()
    candidates = {source, source.rsplit(".", 1)[-1]}
    if source.startswith("plugins."):
        candidates.add(source.removeprefix("plugins."))
    if source.startswith("builtin."):
        candidates.add(source.removeprefix("builtin."))
    return bool(candidates & aliases)


def match_runtime_candidates(candidates: Iterable[dict[str, Any]], text: str | None) -> list[dict[str, Any]]:
    if text is None:
        return []
    raw = text.strip()
    if not raw:
        return []
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        kind = candidate.get("kind")
        ignore_case = bool(candidate.get("ignore_case"))
        candidate_raw = raw.casefold() if ignore_case else raw
        for raw_trigger in candidate.get("triggers", []):
            trigger = str(raw_trigger)
            candidate_trigger = trigger.casefold() if ignore_case else trigger
            matched = False
            if kind in {"command", "prefix"}:
                matched = candidate_raw.startswith(candidate_trigger)
            elif kind == "token":
                matched = candidate_raw == candidate_trigger or (
                    candidate_raw.startswith(candidate_trigger)
                    and len(candidate_raw) > len(candidate_trigger)
                    and candidate_raw[len(candidate_trigger)].isspace()
                )
            elif kind == "exact":
                matched = candidate_raw == candidate_trigger
            elif kind == "suffix":
                matched = candidate_raw.endswith(candidate_trigger)
            elif kind == "contains":
                matched = candidate_trigger in candidate_raw
            elif kind == "regex":
                try:
                    flags = int(candidate.get("regex_flags") or 0)
                    matched = re.search(trigger, raw[:4_096], flags=flags) is not None
                except (re.error, TypeError, ValueError):
                    matched = False
            if matched:
                matches.append(
                    {
                        "plugin_id": candidate.get("plugin_id"),
                        "module_name": candidate.get("module_name"),
                        "kind": kind,
                        "trigger": trigger,
                        "complete": candidate.get("complete") is True,
                        "rule_checker_count": candidate.get("rule_checker_count"),
                        "unknown_rule_checkers": candidate.get("unknown_rule_checkers", []),
                        "permission_checker_count": candidate.get("permission_checker_count"),
                    }
                )
                break
    # NoneBot stores on_command prefixes in a trie and exposes only the
    # longest matching command to CommandRule.  Do not report every shorter
    # directory-derived command as another simultaneous runtime match.
    command_lengths = [len(item["trigger"]) for item in matches if item.get("kind") == "command"]
    if command_lengths:
        longest = max(command_lengths)
        matches = [
            item
            for item in matches
            if item.get("kind") != "command" or len(item["trigger"]) == longest
        ]
    return matches


def match_runtime_candidate(
    candidates: Iterable[dict[str, Any]],
    text: str | None,
) -> dict[str, Any] | None:
    matches = match_runtime_candidates(candidates, text)
    return matches[0] if matches else None


def runtime_match_supports_command(runtime_match: dict[str, Any], command_match: CommandMatch) -> bool:
    aliases = runtime_plugin_aliases([runtime_match])
    return (
        source_plugin_loaded(command_match.source_plugin, aliases)
        and runtime_match.get("kind") == command_match.kind
        and str(runtime_match.get("trigger", "")).strip().casefold()
        == command_match.trigger.strip().casefold()
    )


def runtime_candidate_trigger_reviewed(
    registry: CommandRegistry,
    candidate: dict[str, Any],
    trigger: Any,
) -> bool:
    normalized_trigger = str(trigger).strip().casefold()
    aliases = runtime_plugin_aliases([candidate])
    return any(
        source_plugin_loaded(rule.source_plugin, aliases)
        and candidate.get("kind") == rule.kind
        and normalized_trigger in {item.strip().casefold() for item in rule.triggers}
        for rule in registry.rules
    )


def _load_triggers(raw: Any, *, rule_id: str) -> tuple[str, ...]:
    if isinstance(raw, str):
        triggers = (raw,)
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        triggers = tuple(raw)
    else:
        raise ValueError(f"command registry rule {rule_id!r} must define string triggers")
    if not triggers or any(not item.strip() for item in triggers):
        raise ValueError(f"command registry rule {rule_id!r} has an empty trigger")
    return triggers


def _registry_path(path: str | Path) -> Path:
    active = Path(path)
    if active.is_absolute():
        return active
    return Path.cwd() / active


@lru_cache(maxsize=8)
def load_command_registry(path: str | Path = DEFAULT_COMMAND_REGISTRY_PATH) -> CommandRegistry:
    active_path = _registry_path(path)
    data = tomllib.loads(active_path.read_text(encoding="utf-8"))
    version = str(data.get("version", "unknown"))
    default_target = str(data.get("default_target_instance_id", "lily-command"))
    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError("command registry rules must be a list")

    rules: list[CommandRule] = []
    for index, raw_rule in enumerate(raw_rules):
        if not isinstance(raw_rule, dict):
            raise ValueError(f"command registry rule at index {index} must be an object")
        if raw_rule.get("enabled", True) is False:
            continue
        rule_id = str(raw_rule.get("id") or f"rule-{index}")
        kind = str(raw_rule.get("kind", "prefix"))
        if kind not in {"command", "token", "prefix", "exact", "suffix", "contains", "regex"}:
            raise ValueError(f"command registry rule {rule_id!r} has unsupported kind {kind!r}")
        confidence = int(raw_rule.get("confidence", 95))
        if confidence < 0 or confidence > 100:
            raise ValueError(f"command registry rule {rule_id!r} confidence must be 0..100")
        permission = str(raw_rule.get("permission", "public"))
        if permission not in COMMAND_PERMISSIONS:
            raise ValueError(f"command registry rule {rule_id!r} has unsupported permission {permission!r}")
        rules.append(
            CommandRule(
                id=rule_id,
                kind=kind,  # type: ignore[arg-type]
                triggers=_load_triggers(raw_rule.get("triggers"), rule_id=rule_id),
                target_instance_id=str(raw_rule.get("target_instance_id") or default_target),
                source_plugin=str(raw_rule.get("source_plugin", "")),
                confidence=confidence,
                permission=permission,
                sensitive=bool(raw_rule.get("sensitive", False)),
                description=str(raw_rule.get("description", "")),
            )
        )
    return CommandRegistry(version=version, rules=tuple(rules))
