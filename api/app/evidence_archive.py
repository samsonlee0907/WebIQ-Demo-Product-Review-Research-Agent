"""Private, durable internal research work products and independently captured evidence.

WebIQ search/browse responses are deliberately excluded. Generated summaries with
attribution may be retained under WebIQ Exhibit A section 4; this is not a reusable
search-content archive or a claim of publisher permission for browser captures.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterator
import uuid
import zipfile

from azure.core.exceptions import AzureError, ResourceNotFoundError
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

try:
    from .bat_research import FoundryResearchError, get_foundry_credential, redact_secrets
    from .config import is_hosted_mode
    from .screenshot_store import SCREENSHOT_STORE
except ImportError:
    from bat_research import FoundryResearchError, get_foundry_credential, redact_secrets
    from config import is_hosted_mode
    from screenshot_store import SCREENSHOT_STORE

LOGGER = logging.getLogger(__name__)
NO_STORE = {"Cache-Control": "private, no-store"}
ARCHIVE_ID = r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{32}"
DIRECT_INSPECTORS = frozenset({"live", "client_browser", "vm_playwright"})
ACTIVE_ARCHIVES: set[str] = set()
router = APIRouter()


class EvidenceStorageError(RuntimeError):
    pass


@contextmanager
def storage_operation() -> Iterator[None]:
    try:
        yield
    except (OSError, AzureError, FoundryResearchError) as exc:
        LOGGER.error("Durable research storage failed (%s).", type(exc).__name__)
        raise EvidenceStorageError("Durable evidence storage is unavailable. Please retry; research has not been silently discarded.") from exc


def validate_id(archive_id: str) -> str:
    if not re.fullmatch(ARCHIVE_ID, archive_id):
        raise HTTPException(404, "Saved research not found.")
    return archive_id


class EvidenceStore:
    """A private local directory or keyless Azure Blob container; never container disk in hosted mode."""

    def __init__(self, *, root: Path | None = None, account_url: str = "", container: str = "research-evidence"):
        self.root = root.resolve() if root is not None else None
        self.container = None
        if account_url:
            if not re.fullmatch(r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net", account_url):
                raise EvidenceStorageError("Invalid evidence storage account URL.")
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9]|-(?!-)){1,61}[a-z0-9]", container):
                raise EvidenceStorageError("Invalid evidence storage container.")
            from azure.storage.blob import BlobServiceClient

            self.container = BlobServiceClient(
                account_url, credential=get_foundry_credential(),
                retry_total=2, connection_timeout=10, read_timeout=20, logging_enable=False,
            ).get_container_client(container)
        elif self.root is None:
            raise EvidenceStorageError("Configure private Azure Blob evidence storage before starting research.")

    def _path(self, name: str) -> Path:
        if self.root is None:
            raise EvidenceStorageError("Local evidence storage is not configured.")
        target = self.root.joinpath(*name.split("/")).resolve()
        if not target.is_relative_to(self.root) or target == self.root:
            raise EvidenceStorageError("Invalid evidence object path.")
        return target

    def put(self, name: str, data: bytes) -> None:
        with storage_operation():
            if self.container is not None:
                self.container.upload_blob(name, data, overwrite=True, logging_enable=False)
            else:
                target = self._path(name)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
                try:
                    temporary.write_bytes(data)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)

    def get(self, name: str) -> bytes | None:
        with storage_operation():
            try:
                if self.container is not None:
                    return self.container.download_blob(name, logging_enable=False).readall()
                return self._path(name).read_bytes()
            except (ResourceNotFoundError, FileNotFoundError):
                return None

    def names(self, prefix: str) -> list[str]:
        with storage_operation():
            if self.container is not None:
                return [item.name for item in self.container.list_blobs(name_starts_with=prefix)]
            directory = self._path(prefix)
            if not directory.exists():
                return []
            return [path.relative_to(self.root).as_posix() for path in directory.rglob("*") if path.is_file()]

    def delete(self, name: str) -> None:
        with storage_operation():
            if self.container is not None:
                try:
                    self.container.delete_blob(name, delete_snapshots="include", logging_enable=False)
                except ResourceNotFoundError:
                    pass
            else:
                self._path(name).unlink(missing_ok=True)

    def manifest(self, archive_id: str) -> dict[str, Any]:
        validate_id(archive_id)
        if self.get(f"deleted/{archive_id}.json") is not None:
            raise HTTPException(404, "Saved research not found.")
        payload = self.get(f"runs/{archive_id}.json")
        if payload is None:
            raise HTTPException(404, "Saved research not found.")
        try:
            return json.loads(payload)
        except (ValueError, UnicodeError) as exc:
            LOGGER.error("Saved research manifest is corrupt (%s).", type(exc).__name__)
            raise EvidenceStorageError("The saved research record could not be read.") from exc


@lru_cache(maxsize=4)
def _configured_store(account_url: str, container: str, directory: str, hosted: bool) -> EvidenceStore:
    if hosted and not account_url:
        raise EvidenceStorageError("Private Azure Blob evidence storage is not configured; new research cannot start safely.")
    return EvidenceStore(account_url=account_url, container=container, root=None if account_url else Path(directory))


def get_store() -> EvidenceStore:
    return _configured_store(
        os.environ.get("PDD_EVIDENCE_BLOB_ACCOUNT_URL", "").rstrip("/"),
        os.environ.get("PDD_EVIDENCE_BLOB_CONTAINER", "research-evidence"),
        os.environ.get("PDD_EVIDENCE_DIRECTORY") or str(Path(__file__).resolve().parents[1] / "private_history"),
        is_hosted_mode(),
    )


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def browser_observation(source: dict[str, Any]) -> dict[str, Any] | None:
    if source.get("inspection_method") not in DIRECT_INSPECTORS or source.get("provider") != "browser":
        return None
    retained = {
        key: source[key] for key in (
            "url", "requested_url", "final_url", "status", "reason", "restriction_reason", "screenshot_artifact_id",
            "inspection_method", "fetched_characters", "content_truncated", "content_display_limit",
            "agent_explored", "discovery_origin", "parent_url", "discussion_blocks_observed", "exploration_steps",
            "verification_attempts", "verification_outcome",
        ) if key in source
    }
    response = source.get("retrieved_response") or {}
    retained["retrieved_response"] = {
        key: response[key] for key in ("url", "content", "headings", "status", "inspection_method") if key in response
    }
    return _clean(retained)


class EvidenceRecorder:
    def __init__(self, store: EvidenceStore, *, product_url: str, user_intent: str, execution_mode: str):
        self.store = store
        self.archive_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
        self.finished = False
        self.data: dict[str, Any] = {
            "schema_version": 1, "archive_id": self.archive_id, "run_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(), "outcome": "running",
            "product_url": redact_secrets(product_url), "user_intent": redact_secrets(user_intent),
            "execution_mode": execution_mode, "retention": "until_explicit_deletion",
            "access": "Shared internal demo: all signed-in passcode holders can view and delete saved research.",
            "policy": "Generated internal work product with attribution, plus independent browser captures. "
                      "No raw WebIQ search/browse responses. Browser text is bounded observed evidence, not a complete website archive.",
            "observations": [], "screenshots": [], "community_actions": [],
            "analysis": None, "report_markdown": "",
        }

    async def save(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=True).encode("utf-8")
        await asyncio.to_thread(self._write_guarded, f"runs/{self.archive_id}.json", payload)

    def _write_guarded(self, name: str, data: bytes) -> None:
        marker = f"deleted/{self.archive_id}.json"
        if self.store.get(marker) is not None:
            raise EvidenceStorageError("This saved research was deleted; its evidence will not be recreated.")
        self.store.put(name, data)
        # A previous app revision can still be finishing an upload during deletion.
        if self.store.get(marker) is not None:
            self.store.delete(name)
            raise EvidenceStorageError("This saved research was deleted while an upload was finishing.")

    async def start(self) -> None:
        ACTIVE_ARCHIVES.add(self.archive_id)
        try:
            await self.save()
        except BaseException:
            ACTIVE_ARCHIVES.discard(self.archive_id)
            raise

    async def observe(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        changed = False
        if event.get("run_id") and not self.data["run_id"]:
            self.data["run_id"] = event["run_id"]
            changed = True
        if kind == "screenshot":
            metadata = event.get("screenshot") or {}
            record = SCREENSHOT_STORE.get(str(metadata.get("artifact_id", "")))
            if record and record.is_live_browser_capture:
                if not record.data.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise EvidenceStorageError("A browser capture was not a valid PNG; it was not silently substituted.")
                if not any(shot["artifact_id"] == record.artifact_id for shot in self.data["screenshots"]):
                    await asyncio.to_thread(self._write_guarded, f"captures/{self.archive_id}/{record.artifact_id}.png", record.data)
                    saved = _clean(record.metadata())
                    saved["content_url"] = f"/api/saved-research/{self.archive_id}/captures/{record.artifact_id}"
                    self.data["screenshots"].append(saved)
                    changed = True
            elif metadata.get("is_live_browser_capture"):
                raise EvidenceStorageError("The original screenshot bytes were unavailable for archiving.")
        elif kind in {"source_inspection", "browsed_sources"}:
            rows = event.get("sources", []) if kind == "browsed_sources" else [event.get("source") or {}]
            for source in rows:
                retained = browser_observation(source)
                if retained is not None:
                    self.data["observations"] = [
                        row for row in self.data["observations"] if row.get("url") != retained.get("url")
                    ] + [retained]
                    changed = True
        elif kind == "product_baseline":
            baseline = event.get("baseline") or {}
            provenance = str(baseline.get("data_provenance", ""))
            if provenance.startswith(("live_bat", "live_client_browser", "live_vm_playwright")):
                self.data["listing_observation"] = _clean({
                    key: baseline[key] for key in ("canonical_url", "retrieved_response", "data_provenance") if key in baseline
                })
                changed = True
        elif kind == "claim_matrix":
            self.data["analysis"] = _clean({
                key: event[key] for key in (
                    "executive_summary", "recommendation_badge", "what_fits", "watch_outs", "evidence_gaps",
                    "matrix", "authenticity_signals", "evidence_quality_disclaimer", "synthesis_status", "run_mode",
                ) if key in event
            })
            changed = True
        elif kind == "final":
            self.data["report_markdown"] = redact_secrets(str(event.get("text", "")))
            changed = True
        elif kind == "community_exploration" and event.get("phase") == "decision":
            self.data["community_actions"].append(_clean({
                key: event[key] for key in ("step", "action", "goal", "reason", "domain", "url") if key in event
            }))
            changed = True
        if changed:
            await self.save()

    async def finish(self, outcome: str) -> None:
        self.data["outcome"] = outcome
        self.data["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await self.save()
            self.finished = True
        finally:
            ACTIVE_ARCHIVES.discard(self.archive_id)

    def public_status(self) -> dict[str, Any]:
        return {
            "type": "evidence_archive", "status": "saved" if self.finished else "saving",
            "archive_id": self.archive_id, "url": f"/saved-research?run={self.archive_id}",
            "retention": "until_explicit_deletion", "screenshots": len(self.data["screenshots"]),
        }


@router.get("/saved-research", response_class=HTMLResponse)
def history_page():
    return FileResponse(Path(__file__).with_name("saved_research.html"), headers=NO_STORE)


@router.get("/api/saved-research")
def list_research(before: str = "", limit: int = Query(default=20, ge=1, le=50)):
    if before:
        validate_id(before)
    store = get_store()
    names = sorted((name for name in store.names("runs/") if name.endswith(".json")), reverse=True)
    ids = [Path(name).stem for name in names if re.fullmatch(ARCHIVE_ID, Path(name).stem)]
    ids = [identifier for identifier in ids if not before or identifier < before]
    items = []
    for identifier in ids[:limit]:
        try:
            data = store.manifest(identifier)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            continue
        items.append({
            key: data.get(key) for key in (
                "archive_id", "run_id", "created_at", "product_url", "user_intent", "execution_mode", "outcome", "retention"
            )
        } | {"screenshots": len(data.get("screenshots", [])), "observations": len(data.get("observations", []))})
    return JSONResponse({"runs": items, "next": ids[limit - 1] if len(ids) > limit else None}, headers=NO_STORE)


@router.get("/api/saved-research/{archive_id}")
def saved_research(archive_id: str):
    return JSONResponse(get_store().manifest(archive_id), headers=NO_STORE)


@router.get("/api/saved-research/{archive_id}/captures/{artifact_id}")
def saved_capture(archive_id: str, artifact_id: str):
    store = get_store()
    manifest = store.manifest(archive_id)
    shot = next((shot for shot in manifest.get("screenshots", []) if shot["artifact_id"] == artifact_id), None)
    if shot is None or not re.fullmatch(r"cap-[a-f0-9]{12}", artifact_id):
        raise HTTPException(404, "Saved screenshot not found.")
    data = store.get(f"captures/{archive_id}/{artifact_id}.png")
    if data is None:
        raise HTTPException(404, "Saved screenshot bytes are unavailable.")
    if not data.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(data).hexdigest() != shot["sha256"]:
        raise EvidenceStorageError("Saved screenshot integrity check failed.")
    return Response(data, media_type="image/png", headers=NO_STORE)


@router.delete("/api/saved-research/{archive_id}")
def delete_research(archive_id: str):
    store = get_store()
    validate_id(archive_id)
    marker = f"deleted/{archive_id}.json"
    if store.get(marker) is None:
        store.manifest(archive_id)
    if archive_id in ACTIVE_ARCHIVES:
        raise HTTPException(409, "This research run is still active. Stop or finish it before deleting.")
    store.put(marker, b"{}")
    for name in store.names(f"captures/{archive_id}/"):
        store.delete(name)
    store.delete(f"runs/{archive_id}.json")
    return JSONResponse({"deleted": True, "archive_id": archive_id}, headers=NO_STORE)


@router.get("/api/saved-research/{archive_id}/export")
def export_research(archive_id: str):
    store = get_store()
    manifest = store.manifest(archive_id)
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("research.json", json.dumps(manifest, indent=2, ensure_ascii=True))
            bundle.writestr("report.md", manifest.get("report_markdown", ""))
            for shot in manifest.get("screenshots", []):
                identifier = shot["artifact_id"]
                if not re.fullmatch(r"cap-[a-f0-9]{12}", identifier):
                    raise EvidenceStorageError("Invalid saved screenshot identity.")
                data = store.get(f"captures/{archive_id}/{identifier}.png")
                if data is None or hashlib.sha256(data).hexdigest() != shot["sha256"]:
                    raise EvidenceStorageError("A saved screenshot is missing or failed its integrity check; no incomplete export was substituted.")
                bundle.writestr(f"screenshots/{identifier}.png", data)
        output.seek(0)
    except BaseException:
        output.close()
        raise

    def chunks():
        with output:
            while chunk := output.read(64 * 1024):
                yield chunk

    return StreamingResponse(chunks(), media_type="application/zip", headers={
        **NO_STORE, "Content-Disposition": f'attachment; filename="research-{archive_id}.zip"',
    })
