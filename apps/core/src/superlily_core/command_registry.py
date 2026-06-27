from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re
import tomllib
from typing import Any, Literal

DEFAULT_COMMAND_REGISTRY_PATH = "apps/core/config/command_registry.toml"
CommandRuleKind = Literal["prefix", "exact", "regex"]
COMMAND_PERMISSIONS = {"public", "group_admin", "superuser"}


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
                if rule.kind == "prefix" and _matches_prefix(normalized, trigger):
                    return _match_from_rule(rule, trigger)
                if rule.kind == "exact" and normalized == trigger.strip().casefold():
                    return _match_from_rule(rule, trigger)
                if rule.kind == "regex" and re.match(trigger, raw, flags=re.IGNORECASE | re.DOTALL):
                    return _match_from_rule(rule, trigger)
        return None

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


def _matches_prefix(normalized_text: str, trigger: str) -> bool:
    normalized_trigger = trigger.strip().casefold()
    if not normalized_trigger:
        return False
    if normalized_text == normalized_trigger:
        return True
    if not normalized_text.startswith(normalized_trigger):
        return False
    return normalized_text[len(normalized_trigger)].isspace()


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
        if kind not in {"prefix", "exact", "regex"}:
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
