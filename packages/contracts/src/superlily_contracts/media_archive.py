from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from .models import WireModel


class QQMediaItem(WireModel):
    path: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=128)
    kind: Literal["forward", "node", "image", "record", "video", "file", "unknown"]
    state: Literal["expanded", "observed", "archived", "metadata_only", "unavailable", "limited", "failed"]
    platform_id: str | None = Field(default=None, max_length=512)
    name: str | None = Field(default=None, max_length=512)
    media_type: str | None = Field(default=None, max_length=256)
    sender_id: str | None = Field(default=None, max_length=256)
    sender_name: str | None = Field(default=None, max_length=512)
    occurred_at: AwareDatetime | None = None
    text: str | None = Field(default=None, max_length=16000)
    segments: list[dict] = Field(default_factory=list, max_length=128)
    omitted_fields: list[str] = Field(default_factory=list, max_length=128)
    size_bytes: int | None = Field(default=None, ge=0, le=9_223_372_036_854_775_807)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=128)

    @field_validator("segments")
    @classmethod
    def safe_segments(cls, value):
        for segment in value:
            if set(segment) != {"type", "data"} or not isinstance(segment["type"], str) or not isinstance(segment["data"], dict):
                raise ValueError("invalid archived segment")
            for key, item in segment["data"].items():
                if key not in {"text", "id", "qq", "name", "summary", "sub_type"} or not isinstance(item, (str, int, bool)):
                    raise ValueError("unsafe archived segment field")
                if isinstance(item, str) and len(item) > 16000:
                    raise ValueError("archived segment too large")
        return value

    @model_validator(mode="after")
    def archive_evidence(self):
        if self.state == "archived" and (self.sha256 is None or self.size_bytes is None):
            raise ValueError("archived media requires verified content")
        if self.state != "archived" and self.sha256 is not None:
            raise ValueError("content references require archived state")
        return self


class QQMediaReport(WireModel):
    job_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1, le=3)
    parent_source_event_id: str = Field(min_length=1, max_length=512)
    parent_message_id: str = Field(min_length=1, max_length=512)
    state: Literal["complete", "partial"]
    reason: str | None = Field(default=None, max_length=128)
    items: list[QQMediaItem] = Field(max_length=128)

    @model_validator(mode="after")
    def unique_paths(self):
        if len({item.path for item in self.items}) != len(self.items):
            raise ValueError("duplicate media tree path")
        return self
