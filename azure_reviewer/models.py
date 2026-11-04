from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["low", "medium", "high", "critical"]
SEVERITIES: tuple[str, ...] = get_args(Severity)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    severity: Severity
    file: str
    line: int | None
    explanation: str
    suggestion: str
    documentation_ids: list[str]


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str
    findings: list[Finding]


class Chunk(BaseModel):
    id: str
    content: str
    source: str
    page: int | None = None


class Skipped(BaseModel):
    path: str
    reason: str


class Report(BaseModel):
    status: Literal["completed", "empty", "failed"] = "completed"
    summary: str = ""
    level: str = "medium"
    deployment: str | None = None
    base: str | None = None
    head: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
    context: list[Chunk] = Field(default_factory=list)
