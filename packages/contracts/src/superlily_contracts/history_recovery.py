from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .models import WireModel


class HistoryRecoveryProgress(WireModel):
    job_id: str = Field(min_length=1, max_length=64)
    revision: int = Field(ge=1)
    window_start: AwareDatetime
    window_end: AwareDatetime
    state: Literal["pending", "running", "scanned", "partial"]
    pages: int = Field(ge=0, le=1000)
    captured: int = Field(ge=0)
    rejected: int = Field(ge=0)
    attempts: int = Field(ge=0)
    reason: str | None = Field(default=None, max_length=256)
    cursor: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def ordered_window(self):
        if self.window_start > self.window_end:
            raise ValueError("recovery window is reversed")
        return self
