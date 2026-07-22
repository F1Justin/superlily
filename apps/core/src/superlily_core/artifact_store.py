"""Core 管理的本地 artifact quarantine 与内容寻址对象存储。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import zlib


_QUARANTINE_KEY_RE = re.compile(
    r"^quarantine/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\.part$"
)
_OBJECT_KEY_RE = re.compile(r"^objects/sha256/[0-9a-f]{2}/([0-9a-f]{64})$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ArtifactStoreError(RuntimeError):
    """不包含本地绝对路径或上传内容的有界存储错误。"""

    def __init__(self, code: str, safe_detail: str) -> None:
        super().__init__(safe_detail)
        self.code = code
        self.safe_detail = safe_detail


@dataclass(frozen=True, slots=True)
class StoredUpload:
    content_sha256: str
    byte_size: int
    mime_type: str
    width_pixels: int
    height_pixels: int


class ArtifactStore:
    """只接受 Core 自己生成的相对 key，不解析调用方路径。"""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    def _initialize_sync(self) -> None:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root_stat = self.root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise ArtifactStoreError("unsafe_root", "artifact root is not a private directory")
        os.chmod(self.root, 0o700, follow_symlinks=False)
        for relative in ("quarantine", "objects", "objects/sha256", "locks"):
            directory = self.root / relative
            directory.mkdir(mode=0o700, exist_ok=True)
            directory_stat = directory.lstat()
            if not stat.S_ISDIR(directory_stat.st_mode) or stat.S_ISLNK(directory_stat.st_mode):
                raise ArtifactStoreError(
                    "unsafe_store_layout", "artifact store contains an unsafe directory entry"
                )
            os.chmod(directory, 0o700, follow_symlinks=False)

    def _path_for_key(self, key: str) -> Path:
        if not (_QUARANTINE_KEY_RE.fullmatch(key) or _OBJECT_KEY_RE.fullmatch(key)):
            raise ArtifactStoreError("invalid_storage_key", "artifact storage key is invalid")
        return self.root.joinpath(*key.split("/"))

    @staticmethod
    def quarantine_key(artifact_id: str) -> str:
        key = f"quarantine/{artifact_id}.part"
        if not _QUARANTINE_KEY_RE.fullmatch(key):
            raise ArtifactStoreError("invalid_artifact_id", "artifact identifier is invalid")
        return key

    @staticmethod
    def object_key(content_sha256: str) -> str:
        key = f"objects/sha256/{content_sha256[:2]}/{content_sha256}"
        if not _OBJECT_KEY_RE.fullmatch(key):
            raise ArtifactStoreError("invalid_content_hash", "artifact content hash is invalid")
        return key

    @asynccontextmanager
    async def object_lock(self, content_sha256: str):
        """跨协程、进程与 Core 副本串行化同一 digest 的发布和清理。"""

        await self.initialize()
        if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
            raise ArtifactStoreError("invalid_content_hash", "artifact content hash is invalid")
        path = self.root / "locks" / f"{content_sha256}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                await asyncio.shield(
                    asyncio.to_thread(fcntl.flock, fd, fcntl.LOCK_UN)
                )
            finally:
                os.close(fd)

    @staticmethod
    def digest_from_object_key(key: str) -> str:
        match = _OBJECT_KEY_RE.fullmatch(key)
        if match is None:
            raise ArtifactStoreError("invalid_storage_key", "artifact storage key is invalid")
        return match.group(1)

    @staticmethod
    def _write_all(fd: int, content: bytes) -> None:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("artifact write made no progress")
            view = view[written:]

    @staticmethod
    def _read_exact(source: object, size: int) -> bytes:
        value = source.read(size)  # type: ignore[attr-defined]
        if len(value) != size:
            raise ArtifactStoreError("invalid_png", "artifact contains a truncated PNG chunk")
        return value

    @classmethod
    def _inspect_png_file(cls, path: Path, byte_size: int) -> tuple[int, int]:
        """逐块验证 PNG framing/CRC；不解码像素，因此不会触发解压炸弹。"""

        if byte_size < 45:
            raise ArtifactStoreError("invalid_png", "artifact is not a structurally valid PNG")
        width = height = 0
        seen_ihdr = seen_idat = seen_iend = False
        with path.open("rb", buffering=0) as source:
            if cls._read_exact(source, 8) != _PNG_SIGNATURE:
                raise ArtifactStoreError("invalid_png", "artifact is not a PNG")
            while source.tell() < byte_size:
                length = struct.unpack(">I", cls._read_exact(source, 4))[0]
                chunk_type = cls._read_exact(source, 4)
                if length > byte_size - source.tell() - 4:
                    raise ArtifactStoreError("invalid_png", "PNG chunk length exceeds the file")
                if not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type):
                    raise ArtifactStoreError("invalid_png", "PNG chunk type is invalid")
                crc = zlib.crc32(chunk_type)
                remaining = length
                ihdr = bytearray()
                while remaining:
                    part = cls._read_exact(source, min(remaining, 1024 * 1024))
                    if chunk_type == b"IHDR":
                        ihdr.extend(part)
                    crc = zlib.crc32(part, crc)
                    remaining -= len(part)
                expected_crc = struct.unpack(">I", cls._read_exact(source, 4))[0]
                if crc & 0xFFFFFFFF != expected_crc:
                    raise ArtifactStoreError("invalid_png_crc", "PNG chunk checksum is invalid")
                if not seen_ihdr:
                    if chunk_type != b"IHDR" or length != 13:
                        raise ArtifactStoreError("invalid_png", "PNG must begin with IHDR")
                    width, height, bit_depth, color_type, compression, filtering, interlace = (
                        struct.unpack(">IIBBBBB", ihdr)
                    )
                    valid_depths = {
                        0: {1, 2, 4, 8, 16},
                        2: {8, 16},
                        3: {1, 2, 4, 8},
                        4: {8, 16},
                        6: {8, 16},
                    }
                    if (
                        width < 1
                        or height < 1
                        or bit_depth not in valid_depths.get(color_type, set())
                        or compression != 0
                        or filtering != 0
                        or interlace not in {0, 1}
                    ):
                        raise ArtifactStoreError("invalid_png_ihdr", "PNG image header is invalid")
                    seen_ihdr = True
                elif chunk_type == b"IHDR":
                    raise ArtifactStoreError("invalid_png", "PNG contains more than one IHDR")
                if chunk_type == b"IDAT":
                    if seen_iend:
                        raise ArtifactStoreError("invalid_png", "PNG data follows IEND")
                    seen_idat = True
                if chunk_type == b"IEND":
                    if length != 0 or not seen_idat or seen_iend:
                        raise ArtifactStoreError("invalid_png", "PNG IEND is invalid")
                    seen_iend = True
                    if source.tell() != byte_size:
                        raise ArtifactStoreError("invalid_png", "PNG contains trailing bytes")
                    break
        if not (seen_ihdr and seen_idat and seen_iend):
            raise ArtifactStoreError("invalid_png", "PNG is missing required chunks")
        return width, height

    async def write_quarantine(
        self,
        key: str,
        chunks: AsyncIterable[bytes],
        *,
        max_bytes: int,
        max_width_pixels: int,
        max_height_pixels: int,
        content_length: int | None,
    ) -> StoredUpload:
        await self.initialize()
        path = self._path_for_key(key)
        if content_length is not None and (content_length < 1 or content_length > max_bytes):
            raise ArtifactStoreError(
                "content_length_out_of_bounds", "artifact Content-Length exceeds its reservation"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise ArtifactStoreError(
                "quarantine_conflict", "artifact quarantine entry already exists"
            ) from exc
        digest = hashlib.sha256()
        byte_size = 0
        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise ArtifactStoreError(
                        "invalid_upload_chunk", "artifact upload yielded a non-byte chunk"
                    )
                if not chunk:
                    continue
                byte_size += len(chunk)
                if byte_size > max_bytes:
                    raise ArtifactStoreError(
                        "artifact_too_large", "artifact upload exceeds its reservation"
                    )
                digest.update(chunk)
                await asyncio.to_thread(self._write_all, fd, chunk)
            if content_length is not None and byte_size != content_length:
                raise ArtifactStoreError(
                    "content_length_mismatch", "artifact body does not match Content-Length"
                )
            if byte_size < 1:
                raise ArtifactStoreError("empty_artifact", "artifact body must not be empty")
            await asyncio.to_thread(os.fsync, fd)
            width, height = await asyncio.to_thread(self._inspect_png_file, path, byte_size)
            if width > max_width_pixels or height > max_height_pixels:
                raise ArtifactStoreError(
                    "artifact_dimensions_exceeded",
                    "PNG dimensions exceed the reviewed artifact policy",
                )
        except BaseException:
            os.close(fd)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        os.close(fd)
        os.chmod(path, 0o600, follow_symlinks=False)
        return StoredUpload(
            content_sha256=digest.hexdigest(),
            byte_size=byte_size,
            mime_type="image/png",
            width_pixels=width,
            height_pixels=height,
        )

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        byte_size = 0
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                byte_size += len(chunk)
        return digest.hexdigest(), byte_size

    async def finalize(
        self,
        quarantine_key: str,
        *,
        content_sha256: str,
        byte_size: int,
    ) -> str:
        await self.initialize()
        source = self._path_for_key(quarantine_key)
        target_key = self.object_key(content_sha256)
        target = self._path_for_key(target_key)
        await asyncio.to_thread(target.parent.mkdir, 0o700, True, True)
        parent_stat = target.parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
            raise ArtifactStoreError(
                "unsafe_store_layout", "artifact object prefix is not a safe directory"
            )
        os.chmod(target.parent, 0o700, follow_symlinks=False)
        try:
            source_stat = source.lstat()
        except FileNotFoundError as exc:
            raise ArtifactStoreError(
                "quarantine_missing", "artifact quarantine content is missing"
            ) from exc
        if not stat.S_ISREG(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode):
            raise ArtifactStoreError(
                "unsafe_quarantine_entry", "artifact quarantine entry is not a regular file"
            )
        if source_stat.st_size != byte_size:
            raise ArtifactStoreError(
                "quarantine_size_mismatch", "artifact quarantine size changed before finalize"
            )
        try:
            await asyncio.to_thread(os.link, source, target, follow_symlinks=False)
            os.chmod(target, 0o600, follow_symlinks=False)
        except FileExistsError:
            target_stat = target.lstat()
            if not stat.S_ISREG(target_stat.st_mode) or stat.S_ISLNK(target_stat.st_mode):
                raise ArtifactStoreError(
                    "unsafe_object_entry", "artifact object entry is not a regular file"
                )
            existing_hash, existing_size = await asyncio.to_thread(self._hash_file, target)
            if existing_hash != content_sha256 or existing_size != byte_size:
                raise ArtifactStoreError(
                    "object_collision", "existing content-addressed object is inconsistent"
                )
        await asyncio.to_thread(source.unlink)
        return target_key

    async def object_exists(self, key: str, *, byte_size: int) -> bool:
        await self.initialize()
        path = self._path_for_key(key)
        try:
            entry = await asyncio.to_thread(path.lstat)
        except FileNotFoundError:
            return False
        return (
            stat.S_ISREG(entry.st_mode)
            and not stat.S_ISLNK(entry.st_mode)
            and entry.st_size == byte_size
            and stat.S_IMODE(entry.st_mode) & 0o077 == 0
        )

    async def read_object(
        self,
        key: str,
        *,
        byte_size: int,
        content_sha256: str,
    ) -> bytes:
        """Read an authoritative content-addressed object and re-verify its digest."""

        await self.initialize()
        if self.digest_from_object_key(key) != content_sha256:
            raise ArtifactStoreError("object_identity_mismatch", "artifact object identity is invalid")
        path = self._path_for_key(key)
        try:
            entry = await asyncio.to_thread(path.lstat)
        except FileNotFoundError as exc:
            raise ArtifactStoreError("object_missing", "artifact object is missing") from exc
        if (
            not stat.S_ISREG(entry.st_mode)
            or stat.S_ISLNK(entry.st_mode)
            or entry.st_size != byte_size
            or stat.S_IMODE(entry.st_mode) & 0o077
        ):
            raise ArtifactStoreError("unsafe_object_entry", "artifact object failed authority checks")
        content = await asyncio.to_thread(path.read_bytes)
        if len(content) != byte_size or hashlib.sha256(content).hexdigest() != content_sha256:
            raise ArtifactStoreError("object_integrity_failure", "artifact object failed integrity checks")
        return content

    async def remove(self, key: str) -> bool:
        await self.initialize()
        path = self._path_for_key(key)
        try:
            entry = await asyncio.to_thread(path.lstat)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
            raise ArtifactStoreError(
                "unsafe_storage_entry", "artifact storage entry is not a regular file"
            )
        await asyncio.to_thread(path.unlink)
        return True

    async def remove_if_older(self, key: str, *, cutoff_timestamp: float) -> bool:
        """调用方持有 digest lock 时，按最新 mtime 再判断并删除 orphan。"""

        await self.initialize()
        path = self._path_for_key(key)
        try:
            entry = await asyncio.to_thread(path.lstat)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
            raise ArtifactStoreError(
                "unsafe_storage_entry", "artifact storage entry is not a regular file"
            )
        if entry.st_mtime > cutoff_timestamp:
            return False
        await asyncio.to_thread(path.unlink)
        return True

    @staticmethod
    def _list_objects_sync(root: Path) -> list[tuple[str, float]]:
        objects: list[tuple[str, float]] = []
        base = root / "objects" / "sha256"
        if not base.exists():
            return objects
        for prefix in os.scandir(base):
            if not prefix.is_dir(follow_symlinks=False) or not re.fullmatch(
                r"[0-9a-f]{2}", prefix.name
            ):
                continue
            for entry in os.scandir(prefix.path):
                key = f"objects/sha256/{prefix.name}/{entry.name}"
                if entry.is_file(follow_symlinks=False) and _OBJECT_KEY_RE.fullmatch(key):
                    objects.append((key, entry.stat(follow_symlinks=False).st_mtime))
        return objects

    async def list_objects(self) -> list[tuple[str, float]]:
        await self.initialize()
        return await asyncio.to_thread(self._list_objects_sync, self.root)
