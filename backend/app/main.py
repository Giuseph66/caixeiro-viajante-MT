from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.jobs import router as jobs_router
from app.api.routes.health import router as health_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.resources import router as resources_router
from app.core.config import PROJECT_ROOT, get_settings
from app.jobs.worker import LocalExperimentWorker
from app.persistence.database import SessionLocal
from sqlalchemy.orm import Session


def create_app(
    *,
    session_factory: Callable[[], Session] | None = None,
    job_worker: Any | None = None,
    start_worker: bool = True,
) -> FastAPI:
    settings = get_settings()

    if session_factory is None:
        session_factory = SessionLocal
    if job_worker is None and start_worker:
        job_worker = LocalExperimentWorker(session_factory, PROJECT_ROOT)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if start_worker and application.state.job_worker is not None:
            application.state.job_worker.start()
        try:
            yield
        finally:
            if start_worker and application.state.job_worker is not None:
                application.state.job_worker.shutdown()

    application = FastAPI(title=settings.project_name, version="0.2.0", lifespan=lifespan)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )
    application.state.session_factory = session_factory
    application.state.job_worker = job_worker
    application.include_router(health_router)
    application.include_router(resources_router)
    application.include_router(jobs_router, prefix="/api/jobs")
    application.include_router(jobs_router, prefix="/api/experiments")
    application.include_router(metrics_router)
    return application


app = create_app()
