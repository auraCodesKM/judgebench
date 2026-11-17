"""Event config file (`events/<slug>.yaml`, §6.1) and the default rubric (§8)."""

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class Criterion(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,59}$")
    name: str
    weight: int = Field(gt=0, le=100)


DEFAULT_RUBRIC = [
    Criterion(id="problem_relevance", name="Problem relevance and innovation", weight=15),
    Criterion(id="ai_technical_depth", name="AI technical depth", weight=20),
    Criterion(id="model_quality", name="Model quality and evaluation", weight=20),
    Criterion(id="engineering", name="Engineering and execution", weight=15),
    Criterion(id="practical_impact", name="Practical impact and usability", weight=15),
    Criterion(id="presentation", name="Presentation and demonstration", weight=15),
]
DEFAULT_RUBRIC_VERSION = "default-v1"


class Judge(BaseModel):
    name: str
    email: EmailStr | None = None  # gets edit access to the judging workbook
    telegram_user_id: int | None = None  # numeric ID; activates Telegram access


class EventConfig(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,98}$")
    name: str
    organisation: str
    deadline: datetime
    rubric: list[Criterion] = Field(default_factory=lambda: list(DEFAULT_RUBRIC))
    rubric_version: str = DEFAULT_RUBRIC_VERSION
    telegram_chat_id: int
    judges: list[Judge] = []

    @field_validator("deadline")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("deadline needs a timezone, e.g. 2026-11-23T12:00:00+05:30")
        return v

    @model_validator(mode="after")
    def _weights(self) -> "EventConfig":
        if sum(c.weight for c in self.rubric) != 100:
            raise ValueError("rubric weights must sum to 100")
        if len({c.id for c in self.rubric}) != len(self.rubric):
            raise ValueError("rubric criterion ids must be unique")
        return self


def load_event_config(path: Path) -> EventConfig:
    return EventConfig.model_validate(yaml.safe_load(path.read_text()))
