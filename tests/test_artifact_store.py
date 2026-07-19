from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import struct
import zlib

import pytest

from superlily_core.artifact_store import ArtifactStore, ArtifactStoreError
from superlily_core.models import new_id


def png_bytes(width: int = 1, height: int = 1) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        checksum = zlib.crc32(kind)
        checksum = zlib.crc32(body, checksum) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    scanlines = b"".join(b"\x00" + b"\x00\x00\x00\xff" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(
        b"IDAT", zlib.compress(scanlines)
    ) + chunk(b"IEND", b"")


async def chunks(value: bytes, size: int = 7):
    for offset in range(0, len(value), size):
        yield value[offset : offset + size]


async def interrupted_chunks(value: bytes):
    yield value[:16]
    raise ConnectionError("simulated disconnected upload")


async def test_store_streams_inspects_finalizes_deduplicates_and_uses_private_modes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    store = ArtifactStore(str(root))
    body = png_bytes(2, 3)
    first_key = store.quarantine_key(new_id())
    first = await store.write_quarantine(
        first_key,
        chunks(body),
        max_bytes=4096,
        max_width_pixels=8,
        max_height_pixels=8,
        content_length=len(body),
    )
    assert first.content_sha256 == hashlib.sha256(body).hexdigest()
    assert (first.byte_size, first.mime_type, first.width_pixels, first.height_pixels) == (
        len(body),
        "image/png",
        2,
        3,
    )
    object_key = await store.finalize(
        first_key,
        content_sha256=first.content_sha256,
        byte_size=first.byte_size,
    )
    assert await store.object_exists(object_key, byte_size=len(body))

    second_key = store.quarantine_key(new_id())
    second = await store.write_quarantine(
        second_key,
        chunks(body, 11),
        max_bytes=4096,
        max_width_pixels=8,
        max_height_pixels=8,
        content_length=None,
    )
    assert await store.finalize(
        second_key,
        content_sha256=second.content_sha256,
        byte_size=second.byte_size,
    ) == object_key
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / object_key).stat().st_mode) == 0o600
    assert [key for key, _ in await store.list_objects()] == [object_key]


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda body: b"not-a-png", "invalid_png"),
        (lambda body: body[:-1], "invalid_png"),
        (
            lambda body: body[:32] + bytes([body[32] ^ 1]) + body[33:],
            "invalid_png_crc",
        ),
    ],
)
async def test_store_rejects_invalid_png_and_removes_partial_file(
    tmp_path: Path,
    mutation,
    code: str,
) -> None:
    store = ArtifactStore(str(tmp_path / "artifacts"))
    key = store.quarantine_key(new_id())
    body = mutation(png_bytes())
    with pytest.raises(ArtifactStoreError) as failure:
        await store.write_quarantine(
            key,
            chunks(body),
            max_bytes=4096,
            max_width_pixels=8,
            max_height_pixels=8,
            content_length=len(body),
        )
    assert failure.value.code == code
    assert not (store.root / key).exists()


async def test_store_rejects_bounds_symlinks_and_caller_paths(tmp_path: Path) -> None:
    store = ArtifactStore(str(tmp_path / "artifacts"))
    await store.initialize()
    oversized = store.quarantine_key(new_id())
    with pytest.raises(ArtifactStoreError, match="exceeds"):
        await store.write_quarantine(
            oversized,
            chunks(png_bytes()),
            max_bytes=10,
            max_width_pixels=8,
            max_height_pixels=8,
            content_length=len(png_bytes()),
        )
    dimensioned = store.quarantine_key(new_id())
    with pytest.raises(ArtifactStoreError, match="dimensions"):
        await store.write_quarantine(
            dimensioned,
            chunks(png_bytes(2, 1)),
            max_bytes=4096,
            max_width_pixels=1,
            max_height_pixels=1,
            content_length=None,
        )
    symlink_key = store.quarantine_key(new_id())
    os.symlink(tmp_path / "outside", store.root / symlink_key)
    with pytest.raises(ArtifactStoreError) as failure:
        await store.write_quarantine(
            symlink_key,
            chunks(png_bytes()),
            max_bytes=4096,
            max_width_pixels=8,
            max_height_pixels=8,
            content_length=None,
        )
    assert failure.value.code == "quarantine_conflict"
    with pytest.raises(ArtifactStoreError, match="key"):
        await store.remove("../../outside")


async def test_store_removes_partial_quarantine_when_stream_is_interrupted(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(str(tmp_path / "artifacts"))
    key = store.quarantine_key(new_id())
    with pytest.raises(ConnectionError, match="disconnected"):
        await store.write_quarantine(
            key,
            interrupted_chunks(png_bytes()),
            max_bytes=4096,
            max_width_pixels=8,
            max_height_pixels=8,
            content_length=None,
        )
    assert not (store.root / key).exists()
