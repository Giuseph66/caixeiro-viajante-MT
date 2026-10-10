from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.persistence.repositories.experiment_runs import count_by_status
from app.schemas.api import JobMetricsResponse

router = APIRouter(prefix="/api/metrics", tags=["métricas"])


@router.get("", response_model=JobMetricsResponse)
def read_metrics(session: Session = Depends(get_session)) -> JobMetricsResponse:
    counts = count_by_status(session)
    return JobMetricsResponse(
        counts_by_status=counts,
        queued=counts.get("queued", 0),
        running=counts.get("running", 0),
        total=sum(counts.values()),
    )
