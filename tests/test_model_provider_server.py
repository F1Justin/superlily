import httpx


async def test_resident_provider_requires_trigger_auth_and_only_receives_target_id(
    monkeypatch,
) -> None:
    from superlily_model_provider import server

    monkeypatch.setenv("SUPERLILY_MODEL_PROVIDER_CORE_URL", "http://lily-core:8000")
    monkeypatch.setenv("SUPERLILY_MODEL_PROVIDER_TOKEN", "model-provider-secret")
    monkeypatch.setenv("SUPERLILY_DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv(
        "SUPERLILY_MODEL_PROVIDER_TRIGGER_TOKEN",
        "trigger-secret-that-is-at-least-32-bytes",
    )
    observed = {}

    async def fake_run_attempt(target_id: str, **kwargs):
        observed["target_id"] = target_id
        observed.update(kwargs)
        return {"state": "shadow_complete"}

    monkeypatch.setattr(server, "run_attempt", fake_run_attempt)
    app = server.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://provider",
    ) as client:
        assert (await client.get("/health/ready")).status_code == 200
        denied = await client.post(
            "/v1/attempts",
            json={
                "schema_version": "1.0",
                "target_type": "run",
                "target_id": "11111111-1111-1111-1111-111111111111",
            },
        )
        assert denied.status_code == 401
        accepted = await client.post(
            "/v1/attempts",
            headers={
                "Authorization": "Bearer trigger-secret-that-is-at-least-32-bytes"
            },
            json={
                "schema_version": "1.0",
                "target_type": "tool_loop",
                "target_id": "22222222-2222-2222-2222-222222222222",
            },
        )
    assert accepted.status_code == 200
    assert observed == {
        "target_id": "22222222-2222-2222-2222-222222222222",
        "tool_loop": True,
        "core_url": "http://lily-core:8000",
        "core_token": "model-provider-secret",
        "api_key": "deepseek-secret",
        "base_url": "https://api.deepseek.com",
        "timeout_seconds": 30.0,
    }
