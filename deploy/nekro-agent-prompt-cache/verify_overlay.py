import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, "/app")

from nekro_agent.models.db_exec_code import ExecStopType
from nekro_agent.services.agent.resolver import fix_code_content
from nekro_agent.services.sandbox import runner


def verify_injected_method_import_normalization() -> None:
    exact = "from lily_core_bridge import submit_rendered_markdown\nsubmit_rendered_markdown('ok')\n"
    assert fix_code_content(exact) == "\nsubmit_rendered_markdown('ok')\n"

    untouched = [
        "from lily_core_bridge import submit_rendered_markdown as render\n",
        "from lily_core_bridge import unknown_method\n",
        "from lily_core_bridge import (\n    submit_rendered_markdown,\n)\n",
        "from lily_core_bridge import submit_rendered_markdown; print('x')\n",
        "from lily_core_bridge import submit_rendered_markdown  # keep\n",
    ]
    for source in untouched:
        assert fix_code_content(source) == source


class FakeContainer:
    def __init__(self, client, container_id):
        self.client = client
        self.id = container_id

    async def delete(self, force=False):
        FakeDocker.deletes.append((self.id, self.client.closed, force))
        if self.client.closed:
            raise RuntimeError("Session is closed")


class FakeContainers:
    def __init__(self, client):
        self.client = client

    async def run(self, *, name, config):
        del name, config
        FakeDocker.next_id += 1
        return FakeContainer(self.client, f"container-{FakeDocker.next_id}")

    def container(self, container_id):
        return FakeContainer(self.client, container_id)


class FakeDocker:
    next_id = 0
    deletes = []

    def __init__(self):
        self.closed = False
        self.containers = FakeContainers(self)

    async def close(self):
        self.closed = True


async def _cancel_cleanup(chat_key: str) -> None:
    task = runner.chat_key_sandbox_cleanup_task_map.pop(chat_key, None)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def verify_container_lifecycle() -> None:
    chat_key = "overlay-verification"
    runner.chat_key_sandbox_map.clear()
    runner.chat_key_sandbox_container_map.clear()
    runner.chat_key_sandbox_cleanup_task_map.clear()
    FakeDocker.deletes.clear()

    async def successful_run(container, timeout):
        del timeout
        await container.delete()
        return "ok", ExecStopType.NORMAL

    async def failed_run(container, timeout):
        del container, timeout
        raise RuntimeError("execution failed")

    async def timed_out_run(container, timeout):
        del timeout
        await container.delete()
        return "timeout", ExecStopType.TIMEOUT

    async def create_record(**kwargs):
        del kwargs

    code_run_data = SimpleNamespace(code_content="pass", thought_chain="")
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        upload_path = temp_path / "uploads"
        (upload_path / chat_key).mkdir(parents=True)

        common_patches = (
            patch.object(runner.aiodocker, "Docker", FakeDocker),
            patch.object(runner, "HOST_SHARED_DIR", temp_path / "shared"),
            patch.object(runner, "HOST_PACKAGE_DIR", temp_path / "packages"),
            patch.object(runner, "HOST_PIP_CACHE_DIR", temp_path / "pip-cache"),
            patch.object(runner, "USER_UPLOAD_DIR", upload_path),
            patch.object(runner, "get_api_caller_code", AsyncMock(return_value="")),
            patch.object(runner.DBExecCode, "create", create_record),
        )
        for active_patch in common_patches:
            active_patch.start()
        try:
            runner.chat_key_sandbox_container_map[chat_key] = "stale-container"
            with patch.object(runner, "run_container_with_timeout", successful_run):
                for _ in range(2):
                    result = await runner.run_code_in_sandbox(code_run_data, chat_key, 1000)
                    assert result == ("ok", "ok", ExecStopType.NORMAL.value)
                    assert chat_key not in runner.chat_key_sandbox_container_map

            assert ("stale-container", False, True) in FakeDocker.deletes
            assert not any(client_closed for _, client_closed, _ in FakeDocker.deletes)

            with patch.object(runner, "run_container_with_timeout", timed_out_run):
                result = await runner.run_code_in_sandbox(code_run_data, chat_key, 1000)
                assert result == ("timeout", "timeout", ExecStopType.TIMEOUT.value)
                assert chat_key not in runner.chat_key_sandbox_container_map

            with patch.object(runner, "run_container_with_timeout", failed_run):
                try:
                    await runner.run_code_in_sandbox(code_run_data, chat_key, 1000)
                except RuntimeError as error:
                    assert str(error) == "execution failed"
                else:
                    raise AssertionError("sandbox execution failure was swallowed")
            assert chat_key not in runner.chat_key_sandbox_container_map
            assert FakeDocker.deletes[-1][1:] == (False, True)
        finally:
            await _cancel_cleanup(chat_key)
            for active_patch in reversed(common_patches):
                active_patch.stop()


async def verify_live_container_lifecycle() -> None:
    chat_key = f"overlay-live-{int(time.time())}"
    upload_dir = runner.USER_UPLOAD_DIR / chat_key
    shared_dir = runner.HOST_SHARED_DIR / f"sandbox_{chat_key}"
    upload_dir.mkdir(parents=True)

    async def create_record(**kwargs):
        del kwargs

    code_run_data = SimpleNamespace(code_content="print('lifecycle-ok')", thought_chain="")
    try:
        with (
            patch.object(runner, "get_api_caller_code", AsyncMock(return_value=f"FROM_CHAT_KEY = {chat_key!r}\n")),
            patch.object(runner.DBExecCode, "create", create_record),
        ):
            for _ in range(2):
                final_output, raw_output, stop_type = await runner.run_code_in_sandbox(code_run_data, chat_key, 1000)
                assert final_output == "lifecycle-ok"
                assert raw_output == "lifecycle-ok"
                assert stop_type == ExecStopType.NORMAL.value
                assert chat_key not in runner.chat_key_sandbox_container_map
    finally:
        await _cancel_cleanup(chat_key)
        shutil.rmtree(shared_dir, ignore_errors=True)
        shutil.rmtree(upload_dir, ignore_errors=True)


def main() -> None:
    if "--live" in sys.argv:
        asyncio.run(verify_live_container_lifecycle())
        return
    verify_injected_method_import_normalization()
    asyncio.run(verify_container_lifecycle())


if __name__ == "__main__":
    main()
