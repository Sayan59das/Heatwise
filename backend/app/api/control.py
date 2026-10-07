"""Control endpoints for the desktop shell. They exist only when the shell launched us with a token."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/control", tags=["control"])


def _authorize(request: Request, token: str | None) -> None:
    expected: str | None = getattr(request.app.state, "control_token", None)
    if not expected:
        raise HTTPException(status_code=404, detail="Not found")  # indistinguishable from a missing route
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/shutdown")
async def shutdown(request: Request, x_thermalsense_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Ask the server to exit gracefully (flushes history, closes the sensor drivers)."""
    _authorize(request, x_thermalsense_token)
    server = getattr(request.app.state, "server", None)
    if server is None:
        raise HTTPException(status_code=409, detail="Not running under run.py; nothing to stop")
    log.info("shutdown requested by the desktop shell")
    server.should_exit = True
    return {"status": "shutting down"}
