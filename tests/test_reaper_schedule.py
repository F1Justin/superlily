import asyncio
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from superlily_core.settings import Settings

app_module = import_module("superlily_core.app")


@pytest.mark.parametrize("interval", [0, 61])
def test_artifact_reaper_interval_is_bounded(interval):
    with pytest.raises(ValueError, match="artifact_reaper_interval_seconds"):
        Settings(artifact_reaper_interval_seconds=interval)


def test_artifact_reaper_environment_does_not_slow_lease_reaping(monkeypatch):
    monkeypatch.setenv("SUPERLILY_ARTIFACT_REAPER_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("SUPERLILY_TOOL_REAPER_INTERVAL_SECONDS", "1")
    settings = Settings.from_env()
    assert settings.artifact_reaper_interval_seconds == 30
    assert settings.tool_reaper_interval_seconds == 1


@pytest.mark.parametrize("artifact_enabled", [False, True])
async def test_reapers_have_independent_schedules(app, monkeypatch, tmp_path, artifact_enabled):
    app.state.settings = replace(
        app.state.settings,
        artifact_root=str(tmp_path) if artifact_enabled else "",
        artifact_secret_pepper="test-pepper-" * 4 if artifact_enabled else "",
        tool_execution_mode="canary",
        tool_reaper_interval_seconds=1,
        artifact_reaper_interval_seconds=30,
    )
    artifact = AsyncMock()
    attempts = AsyncMock()
    invocations = AsyncMock()
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(app_module, "reap_expired_artifacts", artifact)
    monkeypatch.setattr(app_module, "reap_expired_attempts", attempts)
    monkeypatch.setattr(app_module, "reap_expired_invocations", invocations)
    monkeypatch.setattr(app_module, "asyncio", SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await app_module._run_artifact_reaper(app, app.state.database)
    assert artifact.await_count == int(artifact_enabled)
    sleep.assert_awaited_once_with(30)
    attempts.assert_not_awaited()
    sleep.reset_mock()
    with pytest.raises(asyncio.CancelledError):
        await app_module._run_tool_reaper(app, app.state.database)
    attempts.assert_awaited_once()
    invocations.assert_awaited_once()
    sleep.assert_awaited_once_with(1)
