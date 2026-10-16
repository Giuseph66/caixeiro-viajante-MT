from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

JobType = Literal["tsp", "mtsp", "overlap"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "interrupted"]
RouteObjective = Literal["distance", "time"]
OverlapPolicy = Literal["P1", "P2", "P3"]


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_type: JobType
    objective: RouteObjective = "distance"
    vehicle_count: int = Field(default=1, ge=1, le=3)
    origin_ibge_code: int | None = Field(default=None, ge=1000000, le=9999999)
    destination_ibge_code: int | None = Field(default=None, ge=1000000, le=9999999)
    overlap_policy: OverlapPolicy = "P1"
    repetition_policy: Literal["allow", "reduce", "forbid"] = "allow"
    time_limit_s: float = Field(default=60.0, gt=0, le=300)
    workers: int = Field(default=8, ge=1, le=8)
    variants: int = Field(default=8, ge=1, le=8)
    seed: int = Field(default=20261008, ge=0, le=2**31 - 1)
    lambdas: list[float] = Field(default_factory=lambda: [0.0, 0.25, 0.5, 1.0, 2.0])

    @model_validator(mode="before")
    @classmethod
    def fill_agent_default(cls, values: Any) -> Any:
        if isinstance(values, dict) and values.get("job_type") in ("mtsp", "overlap"):
            values = dict(values)
            values.setdefault("vehicle_count", 2)
        return values

    @model_validator(mode="after")
    def validate_lambdas(self) -> JobCreateRequest:
        if self.job_type == "tsp" and self.vehicle_count != 1:
            raise ValueError("Open TSP deve usar exatamente um agente")
        if self.job_type in ("mtsp", "overlap") and self.vehicle_count not in (2, 3):
            raise ValueError("Open mTSP deve usar dois ou três agentes")
        if self.destination_ibge_code is not None and self.vehicle_count != 1:
            raise ValueError("Escolha um destino fixo somente para uma rota com um agente")
        if (
            self.origin_ibge_code is not None
            and self.destination_ibge_code == self.origin_ibge_code
        ):
            raise ValueError("Origem e destino precisam ser municípios diferentes")
        if self.job_type == "mtsp" and self.overlap_policy != "P1":
            raise ValueError("As políticas P2 e P3 exigem busca de rotas compartilhadas")
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
