import pytest

from superlily_core.settings import Settings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stale_after_seconds", 0),
        ("correlation_window_seconds", -1),
        ("raw_max_bytes", 1_023),
        ("claim_mode", "unsafe"),
    ],
)
def test_settings_reject_unsafe_control_plane_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_settings_reject_reused_authentication_tokens() -> None:
    with pytest.raises(ValueError, match="unique per instance"):
        Settings(ingest_tokens={"lily-command": "same", "nekro-agent": "same"})
    with pytest.raises(ValueError, match="admin and ingest"):
        Settings(admin_token="same", ingest_tokens={"lily-command": "same"})
