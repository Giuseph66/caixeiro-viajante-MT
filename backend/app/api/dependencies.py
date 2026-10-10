from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session


def get_session(request: Request) -> Iterator[Session]:
    session_factory = request.app.state.session_factory
    with session_factory() as session:
        yield session


def get_job_worker(request: Request) -> Any:
    worker = request.app.state.job_worker
    if worker is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Executor de experimentos indisponível",
        )
    return worker
