"""Private browser artifacts and short-lived generated briefs, never raw WebIQ grounding."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

PRIVATE_ROOT = Path(__file__).resolve().parents[1] / "private_replay"
TRANSIENT_TTL = 900
TRANSIENT: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
NO_STORE = {"Cache-Control": "private, no-store"}


async def _expire_context():
    while True:
        for key, (expires_at, _) in list(TRANSIENT.items()):
            if expires_at <= time.time():
                del TRANSIENT[key]
        await asyncio.sleep(1)


@asynccontextmanager
async def _lifespan(_app):
    async with asyncio.TaskGroup() as tasks:
        expiry = tasks.create_task(_expire_context())
        try:
            yield
        finally:
            expiry.cancel()
            TRANSIENT.clear()


router = APIRouter(prefix="/api/replay", lifespan=_lifespan)


class TransientEvidenceRequest(BaseModel):
    run_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    evidence: dict[str, Any]


@router.post("/transient")
async def save_transient_evidence(req: TransientEvidenceRequest):
    if len(json.dumps(req.evidence)) > 150_000:
        raise HTTPException(413, "Replay context exceeds the bounded evidence size.")
    if set(req.evidence) - {"synthesis"}:
        raise HTTPException(422, "Only generated synthesis may be replayed transiently; source grounding is workflow-only.")
    summary = req.evidence.get("synthesis")
    if summary is not None and (
        not isinstance(summary, dict) or set(summary) - {"summary", "recommendation", "gaps"}
        or any(not isinstance(summary.get(key, ""), str) for key in ("summary", "recommendation"))
        or not isinstance(summary.get("gaps", []), list)
        or any(not isinstance(gap, str) for gap in summary.get("gaps", []))
    ):
        raise HTTPException(422, "Invalid generated synthesis replay shape.")
    now = time.time()
    if req.expires_at <= now:
        raise HTTPException(410, "Temporary evidence expired; run fresh research to view it.")
    for key in list(TRANSIENT):
        if TRANSIENT[key][0] <= now:
            del TRANSIENT[key]
    TRANSIENT[req.run_id] = (min(req.expires_at, now + TRANSIENT_TTL), req.evidence)
    TRANSIENT.move_to_end(req.run_id)
    while len(TRANSIENT) > 32:
        TRANSIENT.popitem(last=False)
    return JSONResponse({"status": "temporary", "expires_at": TRANSIENT[req.run_id][0]}, headers=NO_STORE)


@router.get("/evidence/{run_id}")
async def evidence(run_id: str):
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise HTTPException(404, "Invalid evidence identifier.")
    path = PRIVATE_ROOT / f"run-{run_id}.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    if saved is not None:
        saved = {key: saved[key] for key in ("method", "sources", "screenshots", "baseline", "run_id", "community_actions") if key in saved}
    transient = TRANSIENT.get(run_id)
    if transient and transient[0] <= time.time():
        del TRANSIENT[run_id]
        transient = None
    if saved is None and transient is None:
        raise HTTPException(404, "No retained visual evidence; this recording is metadata-only.")
    return JSONResponse({
        "saved": saved, "temporary": transient[1] if transient else None,
        "temporary_status": "available" if transient else "expired_or_not_retained",
        "temporary_expires_at": transient[0] if transient else None,
    }, headers=NO_STORE)


@router.get("/assets/{filename}")
def asset(filename: str):
    if not re.fullmatch(r"[a-f0-9]{64}\.png", filename):
        raise HTTPException(404, "Invalid capture identifier.")
    path = PRIVATE_ROOT / "assets" / filename
    if not path.is_file():
        raise HTTPException(404, "The saved capture is unavailable.")
    return FileResponse(path, media_type="image/png", headers=NO_STORE)
