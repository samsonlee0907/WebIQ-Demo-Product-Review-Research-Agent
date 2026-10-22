"""Run matched live pipelines with public metadata and optional private visual evidence."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.app.config import SCENARIOS
from api.app.community_exploration import DEFAULT_DOMAINS, normalize_domains
from scripts.windows_playwright_worker import EXTRACTION_JS, community_extraction_script, find_chrome_executable

METHODS = ("webiq_browse", "live", "client_browser", "vm_playwright", "search_only")
REPORT = ROOT / "docs" / "browser-execution-methods-benchmark.html"
RESULTS = ROOT / "docs" / "browser-benchmark-runs.json"
DATA_PATTERN = r'(<script id="benchmark-run-data" type="application/json">).*?(</script>)'
COMMUNITY_METHODS = ("live", "client_browser", "vm_playwright")


def community_metadata(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item for key in ("status", "actions", "pages", "searches", "new_pages", "discussion_blocks")
        if isinstance(item := value.get(key), (str, int))
    }


def public_url(value: Any) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    query = ""
    if parsed.hostname == "youtube.com" or parsed.hostname.endswith(".youtube.com"):
        identity = parse_qs(parsed.query).get("v", [""])[0]
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", identity):
            query = urlencode({"v": identity})
    return urlunsplit(("https", parsed.hostname, parsed.path, query, ""))


def inspection_metadata(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": public_url(source.get("requested_url") or source.get("url")),
        "inspector": source.get("inspection_method"),
        "status": source.get("access_status") or source.get("status"),
        "fetched_characters": source.get("fetched_characters", 0),
        "capture": bool(source.get("screenshot_artifact_id")),
        "restriction_kind": source.get("restriction_kind", ""),
        "verification_attempts": source.get("verification_attempts", 0),
        "verification_outcome": source.get("verification_outcome", "not_needed"),
        "agent_explored": bool(source.get("agent_explored")),
        "discussion_blocks_observed": source.get("discussion_blocks_observed", 0),
    }


def record_event(run: dict[str, Any], event: dict[str, Any], elapsed: float) -> None:
    """Whitelist execution metadata; never retain excerpts, screenshots, model prose or credentials."""
    kind = event.get("type")
    entry: dict[str, Any] = {"elapsed_s": round(elapsed, 2), "type": kind}
    if kind == "status":
        entry["stage"] = event.get("stage")
        if event.get("execution_mode"):
            run["server_method"] = event["execution_mode"]
        if event.get("run_id"):
            run["run_id"] = event["run_id"]
    elif kind == "product_baseline":
        baseline = event.get("baseline") or {}
        entry.update(provenance=baseline.get("data_provenance"), claim_count=len(baseline.get("seller_claims") or []))
        run["listing_provenance"] = entry["provenance"]
    elif kind == "enrichment":
        sources = event.get("sources") or []
        entry.update(provider=event.get("provider"), status=event.get("status"),
                     urls=[public_url(s.get("url")) for s in sources])
        run.update(search_provider=entry["provider"], discovered=len(sources))
    elif kind == "source_inspection":
        entry.update(inspection_metadata(event.get("source") or {}))
    elif kind == "community_exploration":
        entry.update(community_metadata(event))
        entry["phase"] = event.get("phase")
        if event.get("action") in ("search", "visit", "scroll", "finish"):
            entry["action"] = event["action"]
        if event.get("url"):
            entry["url"] = public_url(event["url"])
        if event.get("domain") in run.get("exploration_domains", []):
            entry["domain"] = event["domain"]
        if event.get("phase") == "completed":
            run["community_outcome"] = community_metadata(event)
    elif kind == "browsed_sources":
        rows = [inspection_metadata(s) for s in event.get("sources") or []]
        entry["sources"] = rows
        run.update(inspections=rows, inspected=sum(s["status"] == "inspected" for s in rows),
                   not_inspected=sum(s["status"] != "inspected" for s in rows))
    elif kind == "inspection_skipped":
        run.update(inspect_review_pages=False, inspected=0, not_inspected=0,
                   intentional_skips=event.get("discovered", 0), inspections=[])
        entry.update(stage=event.get("stage"), intentional=True, discovered=event.get("discovered", 0))
    elif kind == "screenshot":
        screenshot = event.get("screenshot") or {}
        entry.update(stage=screenshot.get("stage"), real_capture=bool(screenshot.get("is_live_browser_capture")),
                     capture_kind=screenshot.get("capture_kind"), sha256=screenshot.get("sha256"))
        run["real_captures"] = run.get("real_captures", 0) + int(entry["real_capture"])
    elif kind == "claim_matrix":
        entry.update(model=event.get("model_deployment"), status=event.get("synthesis_status"),
                     claims=len(event.get("matrix") or []), gaps=len(event.get("evidence_gaps") or []))
        run.update(synthesis_model=entry["model"], synthesis_status=entry["status"], claims=entry["claims"], gaps=entry["gaps"])
        metrics = event.get("evidence_metrics") or {}
        run["evidence_metrics"] = {
            key: metrics[key] for key in ("search_content_characters", "inspected_context_characters",
                                         "discovered_sources", "inspected_sources", "inspect_review_pages")
            if isinstance(metrics.get(key), (int, bool))
        }
        if isinstance(event.get("community_exploration"), dict):
            run["community_outcome"] = community_metadata(event["community_exploration"])
    elif kind == "error":
        code = str(event.get("code") or "research_error")
        entry["code"] = code if re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", code) else "research_error"
        run["error_code"] = entry["code"]
    elif kind == "done":
        entry["outcome"] = event.get("outcome")
        run["outcome"] = entry["outcome"]
    else:
        return
    run["events"].append(entry)


def finalize_inspection_counts(run: dict[str, Any]) -> None:
    """Retain observed progress when a deadline interrupts the final source summary."""
    if run.get("inspect_review_pages") is False:
        run.update(inspected=0, not_inspected=0, inspections=[], coverage_incomplete=False)
        return
    if any(event["type"] == "browsed_sources" for event in run["events"]):
        run["coverage_incomplete"] = False
        return
    progress = {}
    for event in run["events"]:
        if event["type"] == "source_inspection":
            progress[event["url"]] = {
                key: event[key] for key in ("url", "inspector", "status", "fetched_characters", "capture",
                                           "restriction_kind", "verification_attempts", "verification_outcome",
                                           "agent_explored", "discussion_blocks_observed") if key in event
            }
    if progress:
        run["inspections"] = list(progress.values())
        run["inspected"] = sum(source["status"] == "inspected" for source in progress.values())
        run["not_inspected"] = sum(source["status"] != "inspected" for source in progress.values())
        run["coverage_incomplete"] = True


def site_only_recording(data: dict[str, Any]) -> dict[str, Any]:
    """Project legacy recordings without changing their actual outcomes or timings."""
    current_contract = "one-inspector-four-research-stages"
    historical = data.get("pipeline_contract") != current_contract

    def project(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: project(item) for key, item in value.items()
                    if not any(token in key.lower() for token in ("video", "processed_media", "metadata_only", "content_understanding"))}
        if isinstance(value, list):
            return [project(item) for item in value
                    if not (isinstance(item, dict) and
                            any("video" in str(item.get(key, "")).lower() for key in ("type", "stage")))]
        return value

    projected = project(data)
    for run in projected["runs"]:
        run.setdefault("recording_scope", "historical-full-run" if historical else "four-stage-site-research")
    projected["schema_version"] = 2
    projected["pipeline_contract"] = current_contract
    return projected


def publish(data: dict[str, Any]) -> None:
    data = site_only_recording(data)
    latest = {(run["case_id"], run["method"]): run for run in data["runs"]}
    earlier = [run for run in data["runs"] if latest[(run["case_id"], run["method"])] is not run]
    data["attempt_history"] = data.get("attempt_history", []) + earlier
    data["runs"] = list(latest.values())
    for run in data["runs"]:
        finalize_inspection_counts(run)
    snapshots = {
        (run.get("server_revision"), run.get("source_commit"), run.get("grounding_policy"))
        for run in data["runs"]
    }
    uniform_snapshot = (len(snapshots) == 1 and all(next(iter(snapshots))) and
                        all(run.get("revision_consistent") is not False for run in data["runs"]))
    matched_inputs = all(
        len({(run.get("product_url"), run.get("intent"), run.get("web_query"))
             for run in data["runs"] if run["case_id"] == case["id"]}) <= 1
        for case in data["cases"]
    )
    data["comparison_note"] = (
        "Retained runs use one runtime revision, source commit and passage-grounding policy, "
        "with matched product URL, buyer priorities and primary search query within each case. "
        if uniform_snapshot and matched_inputs else
        "Retained runs do not share a verified runtime/grounding snapshot and matched inputs; "
        "consult each run's original revision, policy, inputs and execution date. "
    ) + (
        "Live searches ran sequentially: source sets, generated secondary queries, site access and network conditions can differ. "
        "This is an operational comparison, not a controlled answer-quality experiment or universal timing ranking. "
        "Gross context volume is not net-new facts, review authenticity or recommendation accuracy."
    )
    if any(run.get("community_exploration") for run in data["runs"]):
        data["comparison_note"] += (
            " BAT, Local and VM have additional agent-guided community exploration enabled; "
            "WebIQ Browse and search-only do not support that phase. Browser-route timings include extra "
            "model decisions, scoped searches and visits, so this is intentionally not an equal-workload speed test."
        )
    data["recorded_at"] = max(run.get("completed_at") or run["started_at"] for run in data["runs"])
    payload = json.dumps(data, ensure_ascii=True, indent=2).replace("<", "\\u003c")
    source = (ROOT / "api" / "app" / "benchmark_report.html").read_text(encoding="utf-8")
    updated, count = re.subn(DATA_PATTERN, lambda match: match[1] + payload + match[2], source, flags=re.S)
    if count != 1:
        raise RuntimeError("Report must contain exactly one benchmark-run-data block.")
    RESULTS.write_text(payload + "\n", encoding="utf-8")
    REPORT.write_text(updated, encoding="utf-8")
    (ROOT / "api" / "app" / "benchmark_report.html").write_text(updated, encoding="utf-8")


class LocalChrome:
    """Local native Chrome through the same observation protocol, without borrowing a VM."""

    def __init__(self) -> None:
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        try:
            self.browser = self.playwright.chromium.launch(executable_path=find_chrome_executable(), channel="chrome", headless=False)
        except Exception:
            self.playwright.stop()
            raise
        self.context = self.browser.new_context(viewport={"width": 1365, "height": 900})
        self.context.route("**/*", self.guard)
        self.page = self.context.new_page()
        self.status: int | None = None
        self.page.on("response", self._record_navigation_response)

    def _record_navigation_response(self, response: Any) -> None:
        if response.request.is_navigation_request() and response.frame == self.page.main_frame:
            self.status = response.status

    @staticmethod
    def guard(route: Any) -> None:
        from api.app.browser_driver import FORBIDDEN_URL_SEGMENTS
        if route.request.method not in ("GET", "HEAD", "OPTIONS") or any(segment in urlsplit(route.request.url).path.lower() for segment in FORBIDDEN_URL_SEGMENTS):
            route.abort()
        else:
            route.continue_()

    def execute(self, client: httpx.Client, command: dict[str, Any]) -> None:
        if command.get("action") == "close":
            return
        error = None
        try:
            if command.get("action") == "goto":
                self.status = None
                response = self.page.goto(command["url"], wait_until="domcontentloaded", timeout=26000)
                if self.status is None:
                    self.status = response.status if response else None
                self.page.wait_for_timeout(1200)
            elif command.get("action") == "scroll_down":
                self.page.evaluate("pixels => window.scrollBy(0, pixels)", command.get("scroll_pixels") or 700)
                self.page.wait_for_timeout(400)
            snapshot = self.page.evaluate(EXTRACTION_JS)
            if command.get("capture_community"):
                snapshot.update(self.page.evaluate(community_extraction_script()))
            image = base64.b64encode(self.page.screenshot(type="png")).decode("ascii") if command.get("capture_screenshot", True) else None
        except Exception as exc:
            error = str(exc)[:240]
            snapshot, image = {}, None
        response = client.post("/api/client-browser/observation", json={
            "run_id": command["run_id"], "command_id": command["command_id"],
            "url": snapshot.get("url") or command.get("url") or self.page.url,
            "title": snapshot.get("title", ""), "status_code": self.status,
            "headings": snapshot.get("headings", []), "body_excerpt": snapshot.get("body_excerpt", ""),
            "candidate_lines": snapshot.get("candidate_lines", []),
            "screenshot_data_url": f"data:image/png;base64,{image}" if image else None,
            "error": error, "worker_label": "Local Playwright: native Chrome, isolated benchmark profile",
            **{key: snapshot[key] for key in (
                "community_capture_status", "viewport_text", "discussion_blocks", "observed_links", "scroll_y", "at_page_end"
            ) if key in snapshot},
        })
        response.raise_for_status()

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self.playwright.stop()


def execute_run(client: httpx.Client, case: dict[str, Any], method: str, key: str, deployment: str, *,
                visual: bool = False, transient_payloads: list | None = None,
                community_exploration: bool = False, exploration_domains: list[str] | None = None) -> dict[str, Any]:
    from scripts.visual_replay import VisualRecorder
    explore = community_exploration and method in COMMUNITY_METHODS
    domains = normalize_domains(exploration_domains or DEFAULT_DOMAINS) if explore else []
    run: dict[str, Any] = {
        "case_id": case["id"], "product_url": public_url(case["url"]), "intent": case["intent"],
        "method": method, "started_at": datetime.now(timezone.utc).isoformat(),
        "deployment": deployment, "outcome": "interrupted", "events": [],
        "profile": "isolated local native Chrome" if method == "client_browser" else method,
        "web_query": case["web_query"],
        "recording_scope": "four-stage-site-research",
        "inspect_review_pages": method != "search_only",
        "baseline_inspector": "webiq_browse" if method == "search_only" else method,
        "grounding_policy": "passage-3000-internal-200-display",
        "community_exploration": explore,
        "exploration_domains": domains,
    }
    started = time.monotonic()
    chrome: LocalChrome | None = None
    recorder = VisualRecorder(method, key) if visual else None
    try:
        chrome = LocalChrome() if method == "client_browser" else None
        payload = {
            "product_url": case["url"], "user_intent": case["intent"],
            "execution_mode": run["baseline_inspector"], "inspect_review_pages": run["inspect_review_pages"],
            "webiq_query": case["web_query"],
            "community_exploration": explore,
            **({"exploration_domains": domains} if explore else {}),
        }
        admission_deadline = time.monotonic() + 300
        queue_wait = 0.0
        while True:
            admission_started = time.monotonic()
            with client.stream("POST", "/api/run", headers={"X-WebIQ-Key": key}, json=payload) as response:
                if response.status_code == 429:
                    response.read()
                    try:
                        rejection = response.json()
                    except json.JSONDecodeError:
                        response.raise_for_status()
                    detail = rejection.get("detail") if isinstance(rejection, dict) else None
                    concurrency_rejection = isinstance(detail, str) and detail.startswith((
                        "Another Live Web Research session is currently active",
                        "Server is currently running the maximum number of concurrent evaluations",
                    ))
                    if concurrency_rejection and time.monotonic() < admission_deadline:
                        run["admission_retries"] = run.get("admission_retries", 0) + 1
                        print(json.dumps({"method": method, "waiting_for_research_slot": True,
                                          "retry": run["admission_retries"]}), flush=True)
                        time.sleep(min(10.0, max(0.0, admission_deadline - time.monotonic())))
                        queue_wait += time.monotonic() - admission_started
                        run["queue_wait_s"] = round(queue_wait, 2)
                        continue
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    record_event(run, event, time.monotonic() - started - queue_wait)
                    if recorder:
                        recorder.capture(event, client)
                    if chrome and event.get("type") == "client_browser_command" and event.get("target") == "extension":
                        chrome.execute(client, event)
                break
        if recorder:
            payload = recorder.finish(run, client)
            if transient_payloads is not None:
                transient_payloads.append(payload)
            run["visual_capture_status"] = "captured"
    except Exception as exc:
        run["outcome"] = "error"
        run["error_code"] = type(exc).__name__
        if isinstance(exc, httpx.HTTPStatusError):
            run["error_http_status"] = exc.response.status_code
    finally:
        if chrome:
            chrome.close()
        run["elapsed_s"] = round(time.monotonic() - started - run.get("queue_wait_s", 0.0), 2)
        run["completed_at"] = datetime.now(timezone.utc).isoformat()
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal-url", required=True)
    parser.add_argument("--passcode-file", type=Path, required=True)
    parser.add_argument("--webiq-key-file", type=Path)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--runtime-source-commit", required=True,
                        help="Full source SHA used to build the executing image, not the recorder's HEAD.")
    parser.add_argument("--community-exploration", action="store_true",
                        help="Enable model-guided community exploration only for BAT, Local and VM.")
    parser.add_argument("--community-domain", action="append",
                        help="Repeat for each additional-exploration domain; does not limit general discovery.")
    parser.add_argument("--scenario", action="append", choices=list(SCENARIOS))
    parser.add_argument("--method", action="append", choices=METHODS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-errors", action="store_true", help="Retry only previously recorded failed cells; retain the original attempts.")
    parser.add_argument("--store", choices=("official", "amazon"))
    parser.add_argument("--visual-evidence", action="store_true")
    parser.add_argument("--reseed-revision", help="Restore only the generated brief after publication, within its original RAM expiry. Never restore source grounding.")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", args.runtime_source_commit):
        parser.error("--runtime-source-commit must be the executing image's full lowercase source SHA.")
    if args.community_domain and not args.community_exploration:
        parser.error("--community-domain requires --community-exploration.")
    try:
        domains = normalize_domains(args.community_domain or DEFAULT_DOMAINS)
    except ValueError as exc:
        parser.error(str(exc))
    if args.retry_errors and not args.resume:
        parser.error("--retry-errors requires --resume so the original attempts are retained.")
    key = args.webiq_key_file.read_text(encoding="utf-8-sig").strip() if args.webiq_key_file else os.environ.get("WEBIQ_API_KEY", "").strip()
    if not key:
        parser.error("A request-scoped WebIQ key file or WEBIQ_API_KEY is required for matched discovery.")
    passcode = json.loads(args.passcode_file.read_text(encoding="utf-8"))["passcode"]
    cases = []
    for name in args.scenario or SCENARIOS:
        preset = SCENARIOS[name]
        for store in preset["store_targets"]:
            if store["store_id"] not in ("official", "amazon"):
                continue
            if args.store and store["store_id"] != args.store:
                continue
            cases.append({
                "id": f"{name}-{store['store_id']}", "url": store["url"], "intent": preset["user_intent"],
                "web_query": f"{preset['title']} independent owner review long term problems",
            })
    data = json.loads(RESULTS.read_text(encoding="utf-8")) if args.resume and RESULTS.exists() else {
        "schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "round_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "pipeline_contract": "one-inspector-four-research-stages",
        "source_commit": args.runtime_source_commit,
        "retention": "Public recordings contain execution metadata only. Private browser observations and images are separate. Raw WebIQ grounding is workflow-only; generated briefs alone may remain briefly in RAM. Credentials are never retained.",
        "cases": cases, "runs": [],
    }
    existing_cases = {case["id"]: case for case in data["cases"]}
    for case in cases:
        if case["id"] in existing_cases and existing_cases[case["id"]] != case:
            raise RuntimeError("Cannot resume a benchmark after changing its matched inputs.")
        if case["id"] not in existing_cases:
            data["cases"].append(case)
    with httpx.Client(base_url=args.portal_url.rstrip("/"), timeout=httpx.Timeout(600.0, connect=30.0), headers={"Origin": args.portal_url.rstrip("/")}) as client:
        transient_payloads: list[dict[str, Any]] = []
        response = client.post("/api/auth/login", json={"passcode": passcode})
        response.raise_for_status()
        csrf = response.json().get("csrf_token")
        if csrf:
            client.headers["X-CSRF-Token"] = csrf
        contract = client.get("/api/research-contract")
        contract.raise_for_status()
        if contract.json().get("contract") != data["pipeline_contract"]:
            raise RuntimeError("The deployed portal does not implement the required inspection contract.")
        data["server_revision"] = contract.json().get("revision")
        data["source_commit"] = args.runtime_source_commit
        data["recorder_source_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        data["retention"] = (
            "Public recordings contain execution metadata only. Raw WebIQ source grounding is not replayed after the workflow. "
            "Generated briefs may remain briefly in RAM; genuine browser observations and PNGs are separate private artifacts."
        )
        run_budget = contract.json().get("run_timeout_seconds")
        if args.community_exploration:
            advertised = contract.json().get("community_exploration") or {}
            if not set(COMMUNITY_METHODS).issubset(advertised.get("supported_inspectors", [])):
                raise RuntimeError("The deployed runtime does not advertise the requested community exploration.")
        if "vm_playwright" in (args.method or METHODS):
            worker = client.get("/api/client-browser/status")
            worker.raise_for_status()
            if not worker.json().get("vm_worker_online"):
                raise RuntimeError("The VM worker must be online before starting this comparison round.")
        for case in cases:
            for method in args.method or METHODS:
                previous = [run for run in data["runs"] if run["case_id"] == case["id"] and run["method"] == method]
                enabled = args.community_exploration and method in COMMUNITY_METHODS
                if args.resume and any(
                    bool(run.get("community_exploration")) != enabled
                    or run.get("exploration_domains", []) != (domains if enabled else [])
                    for run in previous
                ):
                    raise RuntimeError("Cannot resume a comparison after changing its community exploration settings.")
                if args.retry_errors and (not previous or previous[-1]["outcome"] != "error"):
                    continue
                if args.resume and not args.retry_errors and any(r["case_id"] == case["id"] and r["method"] == method and r["deployment"] == args.deployment for r in data["runs"]):
                    continue
                before = client.get("/api/research-contract")
                before.raise_for_status()
                if before.json().get("revision") != data["server_revision"]:
                    raise RuntimeError("Runtime revision changed during the comparison; refusing to mislabel a new execution.")
                run = execute_run(client, case, method, key, args.deployment, visual=args.visual_evidence,
                                  transient_payloads=transient_payloads, community_exploration=args.community_exploration,
                                  exploration_domains=domains)
                run["source_commit"] = args.runtime_source_commit
                run["recorder_source_commit"] = data["recorder_source_commit"]
                run["server_revision"] = data["server_revision"]
                after = client.get("/api/research-contract")
                after.raise_for_status()
                run["revision_consistent"] = after.json().get("revision") == run["server_revision"]
                run["run_timeout_seconds"] = run_budget
                data["runs"].append(run)
                publish(data)
                if not run["revision_consistent"]:
                    raise RuntimeError("Runtime revision changed during an execution; its revision could not be verified.")
                print(json.dumps({"case": case["id"], "method": method, "outcome": run["outcome"],
                                  "inspected": run.get("inspected", 0), "seconds": run["elapsed_s"]}), flush=True)
        if args.reseed_revision and transient_payloads:
            print("Visual captures saved privately. Temporary context stays only in RAM while awaiting the final revision.", flush=True)
            deadline = time.time() + 900
            while time.time() < deadline:
                response = client.get("/api/research-contract")
                response.raise_for_status()
                if response.json().get("revision") == args.reseed_revision:
                    restored = expired = 0
                    for payload in transient_payloads:
                        if payload["expires_at"] <= time.time():
                            expired += 1
                            continue
                        client.post("/api/replay/transient", json=payload).raise_for_status()
                        restored += 1
                    print(json.dumps({"generated_briefs_restored_with_original_expiry": restored,
                                      "expired_briefs_not_restored": expired,
                                      "raw_search_grounding_restored": False}), flush=True)
                    break
                time.sleep(5)
            else:
                raise RuntimeError("The target revision was not published within the temporary evidence window.")


if __name__ == "__main__":
    main()
