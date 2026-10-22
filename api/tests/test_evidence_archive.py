from __future__ import annotations

import asyncio
import base64
import io
import json
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from fastapi.testclient import TestClient
import pytest

import auth
import evidence_archive as archive
import main as portal_main
from screenshot_store import SCREENSHOT_STORE

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII=")


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    store = archive.EvidenceStore(root=tmp_path)
    monkeypatch.setattr(archive, "get_store", lambda: store)
    monkeypatch.setattr(portal_main, "get_store", lambda: store)
    monkeypatch.setenv("PDD_HOSTED_MODE", "false")
    monkeypatch.setenv("PDD_REQUIRE_PASSCODE", "false")
    auth.reset_security_state_for_testing()
    yield store
    archive.ACTIVE_ARCHIVES.clear()
    auth.reset_security_state_for_testing()


def recorder(store):
    return archive.EvidenceRecorder(
        store, product_url="https://example.com/product", user_intent="Long-term comfort",
        execution_mode="client_browser",
    )


def test_browser_capture_survives_memory_and_store_restart_and_deletes(private_store):
    run = recorder(private_store)
    capture = SCREENSHOT_STORE.record_capture(
        run_id="abcdef123456", sequence=1, stage="listing_digestion", title="Actual capture",
        url="https://example.com/product", caption="Browser", data=PNG, media_type="image/png",
        capture_kind="live_client_browser_png", is_live_browser_capture=True,
    )

    async def record():
        await run.start()
        await run.observe({"type": "status", "run_id": "abcdef123456"})
        await run.observe({"type": "screenshot", "screenshot": capture.metadata()})
        await run.observe({"type": "screenshot", "screenshot": capture.metadata()})
        await run.observe({"type": "final", "text": "# Generated internal report\n[Source](https://example.com/product)"})
        await run.finish("partial")

    asyncio.run(record())
    SCREENSHOT_STORE._records.pop(capture.artifact_id)
    reopened = archive.EvidenceStore(root=private_store.root)
    data = reopened.manifest(run.archive_id)
    assert data["retention"] == "until_explicit_deletion"
    assert len(data["screenshots"]) == 1
    assert reopened.get(f"captures/{run.archive_id}/{capture.artifact_id}.png") == PNG
    with TestClient(portal_main.app) as client:
        image = client.get(data["screenshots"][0]["content_url"])
        assert image.status_code == 200 and image.content == PNG
        bundle = client.get(f"/api/saved-research/{run.archive_id}/export")
        assert bundle.status_code == 200
        with zipfile.ZipFile(io.BytesIO(bundle.content)) as saved:
            assert saved.read(f"screenshots/{capture.artifact_id}.png") == PNG
            assert saved.read("report.md").startswith(b"# Generated")
            assert json.loads(saved.read("research.json"))["archive_id"] == run.archive_id
        assert client.delete(f"/api/saved-research/{run.archive_id}", headers={"Origin": "http://testserver"}).status_code == 200
        assert client.get(f"/api/saved-research/{run.archive_id}").status_code == 404
        assert client.get(data["screenshots"][0]["content_url"]).status_code == 404
    assert reopened.names(f"captures/{run.archive_id}/") == []
    assert reopened.names("runs/") == []


def test_webiq_raw_content_excluded_but_generated_attributed_work_product_retained(private_store):
    run = recorder(private_store)
    browser = {
        "url": "https://forum.example.com/thread", "inspection_method": "client_browser", "provider": "browser",
        "retrieved_response": {"content": "Independently observed browser text", "title": "Search-derived title"},
        "exploration_steps": [{"step": 1, "viewport_text": "Actual scrolled viewport"}],
    }

    async def record():
        await run.start()
        await run.observe({"type": "enrichment", "sources": [{"content": "FORBIDDEN_RAW_SEARCH", "snippet": "FORBIDDEN_SNIPPET"}]})
        await run.observe({"type": "product_baseline", "baseline": {
            "data_provenance": "live_webiq_browse", "retrieved_response": {"content": "FORBIDDEN_RAW_LISTING"},
        }})
        await run.observe({"type": "browsed_sources", "sources": [
            {"inspection_method": "webiq_browse", "provider": "webiq", "retrieved_response": {"content": "FORBIDDEN_RAW_BROWSE"}},
            browser,
        ]})
        await run.observe({"type": "claim_matrix", "executive_summary": "Generated conclusion",
                           "browsed_sources": [{"content": "FORBIDDEN_NESTED_GROUNDING"}],
                           "matrix": [{"source_attribution": {"url": "https://review.example.com", "label": "Attributed source"}}]})
        await run.observe({"type": "final", "text": "Generated brief with [source attribution](https://review.example.com)"})
        await run.finish("partial")

    asyncio.run(record())
    data = private_store.manifest(run.archive_id)
    encoded = json.dumps(data)
    assert "FORBIDDEN" not in encoded
    assert "Independently observed browser text" in encoded
    assert "Actual scrolled viewport" in encoded
    assert "Search-derived title" not in encoded
    assert "Generated conclusion" in encoded and "https://review.example.com" in encoded
    assert len(data["observations"]) == 1 and "listing_observation" not in data


def test_generated_illustrations_do_not_become_real_screenshots(private_store):
    run = recorder(private_store)
    card = SCREENSHOT_STORE.record_capture(
        run_id="abcdef123456", sequence=1, stage="plan", title="Illustration",
        url="https://example.com", caption="", data=b"<svg/>",
    )

    async def record():
        await run.start()
        await run.observe({"type": "screenshot", "screenshot": card.metadata()})
        await run.finish("complete")

    asyncio.run(record())
    assert not private_store.manifest(run.archive_id)["screenshots"]


def test_api_enforces_auth_and_csrf_for_deletion_and_preserves_saved_link(private_store, monkeypatch):
    run = recorder(private_store)
    asyncio.run(run.start())
    asyncio.run(run.finish("complete"))
    monkeypatch.setenv("PDD_REQUIRE_PASSCODE", "true")
    monkeypatch.setenv("PDD_DEMO_PASSCODE", "testing-passcode-only-123")
    monkeypatch.setenv("PDD_PASSCODE_SHA256", "")
    monkeypatch.setenv("PDD_SESSION_SIGNING_SECRET", "s" * 64)
    monkeypatch.setenv("PDD_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("PDD_ALLOWED_ORIGINS", "http://testserver")
    with TestClient(portal_main.app) as client:
        path = f"/api/saved-research/{run.archive_id}"
        assert client.get(path).status_code == 401
        assert client.get(path + "/export").status_code == 401
        assert client.delete(path).status_code == 401
        login_page = client.get("/saved-research?run=" + run.archive_id)
        assert "window.location.pathname === '/saved-research'" in login_page.text
        login = client.post("/api/auth/login", json={"passcode": "testing-passcode-only-123"}, headers={"Origin": "http://testserver"})
        assert login.status_code == 200
        assert client.get(path).status_code == 200
        assert client.delete(path, headers={"Origin": "http://testserver"}).status_code == 403
        assert client.delete(path, headers={"Origin": "https://evil.example", "X-CSRF-Token": login.json()["csrf_token"]}).status_code == 403
        assert client.delete(path, headers={"Origin": "http://testserver", "X-CSRF-Token": login.json()["csrf_token"]}).status_code == 200


def test_running_archive_cannot_be_deleted_and_partial_checkpoints_remain(private_store):
    run = recorder(private_store)
    asyncio.run(run.start())
    with TestClient(portal_main.app) as client:
        path = f"/api/saved-research/{run.archive_id}"
        assert client.get(path).json()["outcome"] == "running"
        assert client.delete(path, headers={"Origin": "http://testserver"}).status_code == 409
        asyncio.run(run.finish("interrupted"))
        assert client.get(path).json()["outcome"] == "interrupted"
        assert client.delete(path, headers={"Origin": "http://testserver"}).status_code == 200


def test_missing_or_corrupt_capture_prevents_incomplete_export(private_store):
    run = recorder(private_store)
    asyncio.run(run.start())
    run.data["screenshots"] = [{"artifact_id": "cap-abcdef123456", "sha256": "f" * 64}]
    asyncio.run(run.finish("partial"))
    with TestClient(portal_main.app) as client:
        path = f"/api/saved-research/{run.archive_id}"
        assert client.get(path + "/export").status_code == 503
        private_store.put(f"captures/{run.archive_id}/cap-abcdef123456.png", PNG)
        assert client.get(path + "/captures/cap-abcdef123456").status_code == 503
        assert client.get(path + "/export").status_code == 503


def test_start_storage_failure_blocks_paid_pipeline_and_releases_slot(private_store, monkeypatch):
    started = []

    async def forbidden_pipeline(**kwargs):
        started.append(True)
        yield 'data: {"type":"done","outcome":"complete"}\n\n'

    monkeypatch.setattr(portal_main, "run_due_diligence_pipeline", forbidden_pipeline)
    monkeypatch.setattr(private_store, "put", lambda *args: (_ for _ in ()).throw(archive.EvidenceStorageError("Storage offline")))
    with TestClient(portal_main.app) as client:
        result = client.post("/api/run", json={"product_url": "https://example.com/product", "user_intent": "Durability"},
                             headers={"Origin": "http://testserver"})
    assert result.status_code == 503 and not started
    assert auth._ACTIVE_TOTAL_RUNS == 0 and not archive.ACTIVE_ARCHIVES


def test_run_stream_saves_actual_evidence_and_excludes_operational_commands(private_store, monkeypatch):
    async def pipeline(**kwargs):
        yield ": keep-alive\n\n"
        for event in (
            {"type": "status", "run_id": "abcdef123456"},
            {"type": "enrichment", "sources": [{"content": "FORBIDDEN_SEARCH_RESPONSE"}]},
            {"type": "client_browser_command", "command_id": "do-not-replay-commands"},
            {"type": "claim_matrix", "executive_summary": "Generated fixture assessment", "matrix": []},
            {"type": "final", "text": "# Generated report"},
            {"type": "done", "outcome": "partial"},
        ):
            yield "data: " + json.dumps(event) + "\n\n"

    monkeypatch.setattr(portal_main, "run_due_diligence_pipeline", pipeline)
    with TestClient(portal_main.app) as client:
        result = client.post("/api/run", json={"product_url": "https://example.com/product", "user_intent": "Comfort"},
                             headers={"Origin": "http://testserver"})
        assert result.status_code == 200
        statuses = [json.loads(row[6:]) for row in result.text.splitlines() if row.startswith("data: ")]
        saved = [row for row in statuses if row["type"] == "evidence_archive" and row["status"] == "saved"]
        assert len(saved) == 1
        data = client.get("/api/saved-research/" + saved[0]["archive_id"]).json()
        assert data["run_id"] == "abcdef123456" and data["outcome"] == "partial"
        assert data["analysis"]["executive_summary"] == "Generated fixture assessment"
        assert "FORBIDDEN" not in json.dumps(data) and "do-not-replay-commands" not in json.dumps(data)
        assert statuses.index(saved[0]) < next(i for i, row in enumerate(statuses) if row["type"] == "done")
        assert ": keep-alive" in result.text


def test_deletion_marker_prevents_old_revision_resurrection_and_allows_retry(private_store):
    run = recorder(private_store)
    asyncio.run(run.start())
    archive.ACTIVE_ARCHIVES.discard(run.archive_id)
    with TestClient(portal_main.app) as client:
        path = f"/api/saved-research/{run.archive_id}"
        assert client.delete(path, headers={"Origin": "http://testserver"}).status_code == 200
        with pytest.raises(archive.EvidenceStorageError, match="will not be recreated"):
            asyncio.run(run.save())
        assert private_store.get(f"runs/{run.archive_id}.json") is None
        assert client.get(path).status_code == 404
        assert client.delete(path, headers={"Origin": "http://testserver"}).status_code == 200


def test_midstream_storage_failure_is_explicit_and_checkpoint_is_incomplete(private_store, monkeypatch):
    original_put = private_store.put
    writes = []

    def fail_once(name, data):
        writes.append(name)
        if len(writes) == 2:
            raise archive.EvidenceStorageError("Storage temporarily unavailable")
        original_put(name, data)

    async def pipeline(**kwargs):
        for event in (
            {"type": "status", "run_id": "abcdef123456"},
            {"type": "final", "text": "# Research continued, but retention failed"},
            {"type": "done", "outcome": "partial"},
        ):
            yield "data: " + json.dumps(event) + "\n\n"

    monkeypatch.setattr(private_store, "put", fail_once)
    monkeypatch.setattr(portal_main, "run_due_diligence_pipeline", pipeline)
    with TestClient(portal_main.app) as client:
        result = client.post("/api/run", json={"product_url": "https://example.com/product", "user_intent": "Comfort"},
                             headers={"Origin": "http://testserver"})
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
        assert any(event.get("type") == "done" for event in events)
        assert any(event.get("type") == "evidence_archive" and event.get("status") == "failed" for event in events)
        assert not any(event.get("type") == "evidence_archive" and event.get("status") == "saved" for event in events)
        identifier = events[0]["archive_id"]
        assert client.get("/api/saved-research/" + identifier).json()["outcome"] == "retention_incomplete"
    assert auth._ACTIVE_TOTAL_RUNS == 0 and not archive.ACTIVE_ARCHIVES


def test_asgi_disconnect_closes_upstream_and_saves_interrupted_checkpoint(private_store, monkeypatch):
    async def check():
        disconnected, closed = asyncio.Event(), asyncio.Event()
        sent = []

        async def pipeline(**kwargs):
            try:
                yield 'data: {"type":"status","run_id":"abcdef123456"}\n\n'
                await asyncio.sleep(60)
            finally:
                closed.set()

        monkeypatch.setattr(portal_main, "run_due_diligence_pipeline", pipeline)
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": json.dumps({
                    "product_url": "https://example.com/product", "user_intent": "Comfort",
                }).encode(), "more_body": False}
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and b'"run_id":"abcdef123456"' in message.get("body", b""):
                disconnected.set()

        await asyncio.wait_for(portal_main.app({
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/run",
            "raw_path": b"/api/run", "query_string": b"", "root_path": "",
            "headers": [(b"host", b"testserver"), (b"origin", b"http://testserver"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        }, receive, send), timeout=10)
        assert closed.is_set()
        initial = next(message["body"] for message in sent if message["type"] == "http.response.body")
        identifier = json.loads(initial.decode().removeprefix("data:").strip())["archive_id"]
        assert private_store.manifest(identifier)["outcome"] == "interrupted"
        assert auth._ACTIVE_TOTAL_RUNS == 0 and not archive.ACTIVE_ARCHIVES

    asyncio.run(check())


def test_upload_racing_with_deletion_removes_just_uploaded_bytes(private_store, monkeypatch):
    run = recorder(private_store)
    original_put = private_store.put
    object_name = f"captures/{run.archive_id}/cap-abcdef123456.png"

    def delete_during_write(name, data):
        original_put(name, data)
        if name == object_name:
            original_put(f"deleted/{run.archive_id}.json", b"{}")

    monkeypatch.setattr(private_store, "put", delete_during_write)
    with pytest.raises(archive.EvidenceStorageError, match="upload was finishing"):
        run._write_guarded(object_name, PNG)
    assert private_store.get(object_name) is None


def test_hosted_storage_fails_closed_without_blob_configuration():
    with pytest.raises(archive.EvidenceStorageError):
        archive._configured_store("", "research-evidence", "unused", True)
    with pytest.raises(archive.EvidenceStorageError):
        archive.EvidenceStore(account_url="https://attacker.example.com")


def test_blank_local_directory_uses_documented_private_default(monkeypatch):
    monkeypatch.setenv("PDD_HOSTED_MODE", "false")
    monkeypatch.setenv("PDD_EVIDENCE_BLOB_ACCOUNT_URL", "")
    monkeypatch.setenv("PDD_EVIDENCE_DIRECTORY", "")
    assert archive.get_store().root == Path(archive.__file__).resolve().parents[1] / "private_history"


def test_saved_library_paginates_and_rejects_traversal(private_store):
    runs = [recorder(private_store) for _ in range(3)]
    for run in runs:
        asyncio.run(run.start())
        asyncio.run(run.finish("complete"))
    with TestClient(portal_main.app) as client:
        first = client.get("/api/saved-research?limit=2").json()
        second = client.get("/api/saved-research", params={"limit": 2, "before": first["next"]}).json()
        assert len(first["runs"]) == 2 and len(second["runs"]) == 1
        assert len({row["archive_id"] for row in first["runs"] + second["runs"]}) == 3
        assert client.get("/api/saved-research/not-an-identifier").status_code == 404
    with pytest.raises(archive.EvidenceStorageError):
        private_store.get("../outside")
