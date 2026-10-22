"""Capture one real, explicitly requested portal workflow without saving raw search grounding.

Requires desktop Edge and an operator-supplied private portal access JSON.
No credentials, cookies, HAR, tracing or raw SSE bodies are written to the output.
An existing output directory is refused before any paid work can start.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import zipfile
import io

from playwright.sync_api import sync_playwright, expect


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_origin(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if (not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", value)
            or not parsed.hostname or parsed.username or parsed.password):
        raise ValueError("Use an HTTPS portal origin without a path, credentials or query.")
    return value


def capture(page, directory: Path, key: str, selector: str | None = None,
            *, max_height: int = 620, locator=None) -> dict:
    if selector or locator is not None:
        target = locator if locator is not None else page.locator(selector).first
        target.scroll_into_view_if_needed()
        content = target.screenshot(animations="disabled")
    else:
        content = page.screenshot(animations="disabled")
    from PIL import Image
    crop = None
    with Image.open(io.BytesIO(content)) as image:
        if selector and image.height > max_height:
            crop = {"left": 0, "top": 0, "width": image.width, "height": max_height}
            buffer = io.BytesIO()
            image.crop((0, 0, image.width, max_height)).save(buffer, format="PNG")
            content = buffer.getvalue()
    filename = key + ".png"
    (directory / filename).write_bytes(content)
    with Image.open(io.BytesIO(content)) as image:
        width, height = image.size
    return {"key": key, "file": filename, "sha256": hashlib.sha256(content).hexdigest(),
            "width": width, "height": height, "captured_at": utc_now(),
            "page_url": page.url, "kind": "genuine_portal_screenshot",
            "region": selector or "browser_viewport", "region_crop": crop, "content_modified": False}


def capture_saved_followup(page, context, args, receipt, archive_id):
    """GET-only follow-up; never reconstruct the transient research timeline."""
    origin = receipt["portal_url"]
    response = context.request.get(origin + "/api/saved-research/" + archive_id)
    if not response.ok:
        raise RuntimeError(f"Saved research could not be reopened (HTTP {response.status}).")
    saved = response.json()
    if saved["product_url"] != receipt["product_url"] or saved["execution_mode"] != receipt["execution_mode"]:
        raise ValueError("The saved run does not match this walkthrough.")
    analysis = saved.get("analysis") or {}
    receipt["saved_research_url"] = origin + "/saved-research?run=" + archive_id
    receipt["saved_run"] = {
        key: saved.get(key) for key in ("archive_id", "run_id", "outcome", "created_at", "completed_at")
    }
    receipt["saved_run"].update({
        "genuine_screenshots": len(saved.get("screenshots", [])),
        "browser_observations": len(saved.get("observations", [])),
        "community_actions": len(saved.get("community_actions", [])),
        "recommendation": analysis.get("recommendation_badge"),
        "executive_summary": analysis.get("executive_summary"),
        "what_fits": analysis.get("what_fits", []),
        "watch_outs": analysis.get("watch_outs", []),
        "evidence_gaps": analysis.get("evidence_gaps", []),
        "claim_checks": len(analysis.get("matrix", [])),
    })
    unexpected_runs = []
    def block_research(route):
        unexpected_runs.append(route.request.url)
        route.abort()

    context.route("**/api/run", block_research)

    def shoot(key, selector=None, max_height=560, locator=None):
        if key not in {shot["key"] for shot in receipt["screens"]}:
            receipt["screens"].append(capture(page, args.output, key, selector, max_height=max_height, locator=locator))
            (args.output / "walkthrough.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    page.goto(receipt["saved_research_url"], wait_until="networkidle")
    expect(page.locator("#research")).to_be_visible()
    shoot("12-saved", "#research", 530)
    first_claim = page.locator("#research > section.card").first.locator("details").first
    expect(first_claim).to_be_visible()
    first_claim.locator("summary").click()
    shoot("07-claims", "#research > section.card details", 430)
    first_claim.locator("summary").click()
    evidence = page.locator("#research > section.card").nth(1)
    shoot("08-timeline", "#research > section.card:nth-of-type(2)", 430)
    listing = evidence.locator("details").first
    listing.locator("summary").click()
    shoot("09-listing", "#research > section.card:nth-of-type(2) details", 450)
    listing.locator("summary").click()
    if saved.get("screenshots"):
        shot = saved["screenshots"][0]
        page.goto(origin + "/api/saved-research/" + archive_id + "/captures/" + shot["artifact_id"],
                  wait_until="networkidle")
        shoot("10-viewport")
        page.goto(receipt["saved_research_url"], wait_until="networkidle")
        expect(page.locator("#research")).to_be_visible()
    evidence = page.locator("#research > section.card").nth(1)
    rows = evidence.locator("details")
    restricted = rows.filter(has_text=re.compile(r"access_denied|HTTP 403|restricted", re.IGNORECASE))
    selected = restricted.first if restricted.count() else rows.nth(1)
    selected.locator("summary").click()
    shoot("11-inspection", "#research > section.card:nth-of-type(2) details", 460, locator=selected)
    shoot("13-retained", "#research section.card:has(.captures)", 570)
    actions = page.locator("#research > p.actions")
    expect(actions.get_by_role("link", name="Download evidence + screenshots")).to_be_visible()
    shoot("14-export", "#research > p.actions", 160)
    with page.expect_download() as downloaded:
        actions.get_by_role("link", name="Download evidence + screenshots").click()
    content = Path(downloaded.value.path()).read_bytes()
    with zipfile.ZipFile(io.BytesIO(content)) as bundle:
        archived = json.loads(bundle.read("research.json"))
        for shot in archived.get("screenshots", []):
            image = bundle.read("screenshots/" + shot["artifact_id"] + ".png")
            if hashlib.sha256(image).hexdigest() != shot["sha256"]:
                raise RuntimeError("An exported screenshot failed its retained SHA-256 check.")
        receipt["export_verified"] = {
            "files": len(bundle.namelist()), "genuine_pngs": len(archived.get("screenshots", [])),
            "sha256": hashlib.sha256(content).hexdigest(), "contains_report": "report.md" in bundle.namelist(),
            "bytes_saved_to_repository": False,
        }
    if unexpected_runs:
        raise RuntimeError("An unexpected research request was blocked during saved follow-up.")


def capture_run(args) -> dict:
    origin = validate_origin(args.portal_url)
    if args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError("Choose a new empty output directory; an existing run must not be repeated.")
    if not args.run_live:
        raise ValueError("Live research requires the explicit --run-live option.")
    private = json.loads(args.passcode_file.read_text(encoding="utf-8"))
    passcode = private["passcode"]
    if not isinstance(passcode, str) or not passcode:
        raise ValueError("The private portal access JSON has no valid passcode.")
    args.output.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema": "portal-walkthrough-v1", "started_at": utc_now(), "portal_url": origin,
        "product_url": args.product_url, "user_intent": args.user_intent,
        "execution_mode": args.inspector, "discovery": "foundry_toolbox_web_search",
        "community_exploration": True, "exploration_domains": args.domains,
        "screens": [], "paid_runs_requested": 0, "raw_grounding_saved": False,
        "checkout_attempted": False,
    }

    def checkpoint():
        (args.output / "walkthrough.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1180, "height": 850}, device_scale_factor=1,
                                      reduced_motion="reduce")
        page = context.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        try:
            login = context.request.post(origin + "/api/auth/login", data={"passcode": passcode},
                                         headers={"Origin": origin})
            if not login.ok:
                raise RuntimeError(f"Portal sign-in failed (HTTP {login.status}).")
            contract_response = context.request.get(origin + "/api/research-contract")
            if not contract_response.ok:
                raise RuntimeError("Cannot verify the deployed research contract.")
            receipt["revision"] = contract_response.json()["revision"]
            if args.inspector == "vm_playwright":
                status = context.request.get(origin + "/api/client-browser/status")
                if not status.ok or not status.json().get("vm_worker_online"):
                    raise RuntimeError("The VM worker is offline; no research was started.")
            page.goto(origin, wait_until="networkidle")
            expect(page.locator("#product-url")).to_have_value("")
            receipt["screens"].append(capture(page, args.output, "01-home"))
            page.locator("#product-url").fill(args.product_url)
            page.locator("#user-intent").fill(args.user_intent)
            page.locator("#user-intent").press("Tab")
            receipt["screens"].append(capture(page, args.output, "02-inputs",
                                               "section[aria-labelledby='input-heading']", max_height=430))
            page.locator("#change-method-btn").click()
            selector = "#exec-mode-vm-playwright" if args.inspector == "vm_playwright" else "#exec-mode-live"
            page.locator(selector).check()
            page.locator("#webiq-key").fill("")
            receipt["screens"].append(capture(page, args.output, "03-method",
                                               "#research-settings .mode-toggle-row", max_height=300))
            receipt["screens"].append(capture(page, args.output, "03b-discovery",
                                               "div:has(> #webiq-key)", max_height=240))
            page.locator("#community-settings > summary").click()
            page.locator("#community-exploration").check()
            page.locator("#exploration-domains").fill(", ".join(args.domains))
            page.locator("#exploration-domains").press("Tab")
            receipt["screens"].append(capture(page, args.output, "04-community", "#community-settings",
                                               max_height=430))
            page.locator("#research-settings > summary").click()
            checkpoint()
            start = time.monotonic()
            with page.expect_request(lambda request: request.url.split("?")[0] == origin + "/api/run") as started:
                page.locator("#run-btn").click()
            payload = started.value.post_data_json
            receipt["paid_runs_requested"] = 1
            receipt["submitted_settings"] = {
                key: payload.get(key) for key in
                ("execution_mode", "inspect_review_pages", "community_exploration", "exploration_domains")
            }
            if payload.get("execution_mode") != args.inspector or not payload.get("inspect_review_pages"):
                raise RuntimeError("The submitted inspector/settings differ from the requested walkthrough.")
            expect(page.locator("#research-overview")).to_be_visible(timeout=30000)
            receipt["screens"].append(capture(page, args.output, "05-running", "#research-overview",
                                               max_height=500))
            checkpoint()
            page.wait_for_function("typeof RUNNING !== 'undefined' && !RUNNING", timeout=1_200_000)
            receipt["elapsed_seconds"] = round(time.monotonic() - start, 2)
            receipt["ui_outcome"] = page.locator("#run-outcome").inner_text()
            receipt["ui_status"] = page.locator("#status-strip").inner_text()
            expect(page.locator("#verdict-card")).to_be_visible(timeout=30000)
            receipt["screens"].append(capture(page, args.output, "06-brief", "#verdict-card", max_height=460))
            link = page.locator("#saved-research-status a").first
            expect(link).to_be_visible()
            followup = link.get_attribute("href")
            receipt["saved_research_url"] = origin + followup if followup.startswith("/") else followup
            archive_id = urlsplit(receipt["saved_research_url"]).query.removeprefix("run=")
            capture_saved_followup(page, context, args, receipt, archive_id)
            receipt["page_errors"] = page_errors
            receipt["finished_at"] = utc_now()
            checkpoint()
            print(json.dumps({key: receipt.get(key) for key in
                              ("revision", "ui_outcome", "elapsed_seconds", "saved_research_url", "saved_run",
                               "export_verified", "paid_runs_requested")}, indent=2))
            return receipt
        except Exception:
            receipt["capture_failed_at"] = utc_now()
            checkpoint()
            raise
        finally:
            browser.close()


def resume_saved(args):
    """Recover screenshots through the saved UI only; no second paid run."""
    path = args.output / "walkthrough.json"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("paid_runs_requested") != 1 or receipt["portal_url"] != validate_origin(args.portal_url):
        raise ValueError("Only a matching, already-submitted walkthrough can be resumed.")
    passcode = json.loads(args.passcode_file.read_text(encoding="utf-8"))["passcode"]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            context = browser.new_context(viewport={"width": 1180, "height": 850}, reduced_motion="reduce")
            login = context.request.post(receipt["portal_url"] + "/api/auth/login", data={"passcode": passcode},
                                         headers={"Origin": receipt["portal_url"]})
            if not login.ok:
                raise RuntimeError(f"Portal sign-in failed (HTTP {login.status}).")
            page = context.new_page()
            capture_saved_followup(page, context, args, receipt, args.resume_archive_id)
            receipt["capture_recovery"] = {"new_paid_runs": 0, "via": "actual saved-research UI",
                                           "recovered_at": utc_now()}
            receipt["finished_at"] = utc_now()
            path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
            print(json.dumps({key: receipt.get(key) for key in
                              ("ui_outcome", "saved_research_url", "saved_run", "export_verified", "capture_recovery")}, indent=2))
        finally:
            browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal-url", required=True)
    parser.add_argument("--passcode-file", type=Path, required=True)
    parser.add_argument("--product-url")
    parser.add_argument("--user-intent")
    parser.add_argument("--inspector", choices=("vm_playwright", "live"), default="vm_playwright")
    parser.add_argument("--domains", nargs="+", default=["reddit.com", "windowscentral.com", "notebookcheck.net", "youtube.com"])
    parser.add_argument("--output", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--run-live", action="store_true", help="Explicitly authorize exactly one potentially billable research run.")
    action.add_argument("--resume-archive-id", help="Complete screenshots using GET-only saved research; never rerun the workflow.")
    args = parser.parse_args()
    if args.resume_archive_id:
        resume_saved(args)
    else:
        if not args.product_url or not args.user_intent:
            parser.error("--product-url and --user-intent are required for a new live run.")
        capture_run(args)


if __name__ == "__main__":
    main()
