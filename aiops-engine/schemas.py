"""Schéma plat et strict : sortie du LLM validée avant toute décision."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Action = Literal["restart_deployment", "scale_deployment", "set_memory_limit",
                 "rollback_image", "escalate"]


class Diagnosis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cause: str = Field(max_length=160)
    confidence: float = Field(ge=0, le=1)
    action: Action
    replicas: int = Field(default=0, ge=0, le=100)
    memory_limit: str = Field(default="", max_length=8)
    image_tag: str = Field(default="", max_length=15)
