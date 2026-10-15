"""Rubrics: criteria, weights and the integer score scale the judge must use."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_CRITERIA = 12
MAX_SCALE_SPAN = 100


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    weight: float = Field(default=1.0)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not KEY_PATTERN.match(value):
            raise ValueError(
                "criterion keys use lowercase letters, digits and underscores, start with a letter, max 40 characters"
            )
        return value

    @field_validator("weight")
    @classmethod
    def _weight(cls, value: float) -> float:
        if not value > 0 or value != value or value in (float("inf"), float("-inf")):
            raise ValueError("criterion weights must be greater than zero")
        return value


class RubricDefinition(BaseModel):
    """The content of one rubric version (validated before it is stored)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scale_min: int = 1
    scale_max: int = 5
    criteria: list[Criterion] = Field(min_length=1, max_length=MAX_CRITERIA)
    instructions: str = Field(default="", max_length=4000)

    @field_validator("scale_min", "scale_max", mode="before")
    @classmethod
    def _integer_scale(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("the score scale uses whole numbers")
        return value

    @model_validator(mode="after")
    def _check(self) -> RubricDefinition:
        if self.scale_min >= self.scale_max:
            raise ValueError("the scale minimum must be lower than the maximum")
        if self.scale_max - self.scale_min > MAX_SCALE_SPAN:
            raise ValueError(f"the score scale may span at most {MAX_SCALE_SPAN} points")
        keys = [c.key for c in self.criteria]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        if duplicates:
            raise ValueError(f"criterion keys must be unique (repeated: {', '.join(duplicates)})")
        return self

    def normalized_weights(self) -> dict[str, Decimal]:
        total = sum((Decimal(str(c.weight)) for c in self.criteria), Decimal(0))
        return {c.key: Decimal(str(c.weight)) / total for c in self.criteria}

    def weighted_overall(self, scores: dict[str, int]) -> float:
        """Weighted mean of the criterion scores (weights normalized to sum to one), 2 decimals."""
        weights = self.normalized_weights()
        value = sum((weights[key] * Decimal(scores[key]) for key in weights), Decimal(0))
        return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


DEFAULT_RUBRIC = RubricDefinition(
    scale_min=1,
    scale_max=5,
    criteria=[
        Criterion(
            key="instruction_following",
            name="Instruction following",
            description=(
                "Does the response do what the system instructions and the user prompt ask, respecting every "
                "stated constraint (format, length, scope, tone)?"
            ),
        ),
        Criterion(
            key="correctness",
            name="Correctness",
            description=(
                "Are the facts, reasoning, calculations and code correct? Penalize errors and unsupported claims."
            ),
        ),
        Criterion(
            key="completeness",
            name="Completeness",
            description="Does the response address every part of the request with the necessary detail?",
        ),
        Criterion(
            key="clarity",
            name="Clarity",
            description="Is the response well organized, easy to follow and free of unnecessary padding?",
        ),
    ],
)
