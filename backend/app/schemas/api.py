from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

JobType = Literal["tsp", "mtsp", "overlap"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "interrupted"]


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: JobType
    time_limit_s: float = Field(default=60.0, gt=0, le=300)
    workers: int = Field(default=8, ge=1, le=8)
    variants: int = Field(default=8, ge=1, le=8)
    seed: int = Field(default=20261008, ge=0, le=2**31 - 1)
    lambdas: list[float] = Field(default_factory=lambda: [0.0, 0.25, 0.5, 1.0, 2.0])

    @model_validator(mode="after")
    def validate_lambdas(self) -> JobCreateRequest:
        if not self.lambdas or any(value < 0 for value in self.lambdas):
            raise ValueError("lambda deve conter valores não negativos")
        if any(value != value or value in (float("inf"), float("-inf")) for value in self.lambdas):
            raise ValueError("lambda deve conter somente valores finitos")
        if 0.0 not in self.lambdas:
            raise ValueError("lambda deve incluir zero para comparar com P1")
        if len(set(self.lambdas)) != len(self.lambdas):
            raise ValueError("Valores lambda devem ser únicos")
        return self


class JobResponse(BaseModel):
    id: UUID
    job_type: JobType
    status: JobStatus
    parameters: dict[str, Any]
    progress_percent: int
    progress_stage: str | None
    result_available: bool
    result_summary: dict[str, Any] | None
    error_summary: str | None
    log_tail: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class MunicipalityResponse(BaseModel):
    ibge_code: int
    name: str
    longitude: float
    latitude: float


class DatasetResponse(BaseModel):
    dataset_id: str
    title: str
    manifest_path: str
    manifest_sha256: str
    available: bool
    details: dict[str, Any]


class JobMetricsResponse(BaseModel):
    counts_by_status: dict[str, int]
    queued: int
    running: int
    total: int
