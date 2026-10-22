"""Local-only usability smoke checks. Requires an already running portal and Edge."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
    if urlparse(base_url).hostname != "127.0.0.1":
        raise ValueError("This check only targets the loopback preview.")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.route(
            "**/*",
            lambda route: route.continue_()
            if urlparse(route.request.url).hostname == "127.0.0.1"
            else route.abort(),
        )
        page = context.new_page()
        errors: list[str] = []
        requests = []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base_url)
        expect(page.locator("#product-url")).to_have_value("")
        expect(page.locator("#user-intent")).to_have_value("")
        expect(page.locator("#exec-mode-sample")).to_be_checked()
        expect(page.locator("#input-mode-badge")).to_contain_text("No live pages")
        expect(page.locator("#timeline-approval")).not_to_be_visible()
        expect(page.locator("#comparison-card")).not_to_be_visible()
        expect(page.locator("#connection-status")).not_to_be_visible()
        assert page.locator("h1").count() == 1
        page.set_viewport_size({"width": 390, "height": 844})
        action = page.locator("#run-btn").bounding_box()
        assert action["y"] + action["height"] <= 844
        page.set_viewport_size({"width": 1280, "height": 900})
        page.locator("#load-example-btn").click()
        expect(page.locator("#product-url")).to_have_value(re.compile(r"https://electronics\.sony\.com/"))
        expect(page.locator("#exec-mode-sample")).to_be_checked()
        page.locator("#change-method-btn").click()
        expect(page.locator("#exec-mode-sample")).to_be_focused()
        page.locator(".sample-picker > summary").first.click()
        expect(page.locator(".pill-btn")).to_have_count(3)
        expect(page.locator("#exec-mode-sample")).to_be_visible()
        expect(page.locator("#exec-mode-live")).to_be_visible()
        expect(page.locator("#exec-mode-search-only")).to_be_visible()
        expect(page.locator("#initial-inspector-wrap")).not_to_be_visible()
        expect(page.locator("#webiq-key")).to_be_visible()
        assert page.locator("#enable-video-search, #authorized-video-drawer, #video-card, #test-cu-btn").count() == 0

        # Verify Phase-1 mode selector toggle UI state (without running live mode)
        page.locator("#exec-mode-live").check()
        expect(page.locator("#exec-mode-live")).to_be_checked()
        expect(page.locator("#community-exploration")).to_be_enabled()
        page.locator("#community-settings > summary").click()
        with page.expect_request(lambda request: request.url.endswith("/api/plan") and
                                 request.post_data_json.get("community_exploration") is True) as community_plan:
            page.locator("#community-exploration").check()
        assert community_plan.value.post_data_json["exploration_domains"] == ["reddit.com", "youtube.com", "amazon.com"]
        expect(page.locator("#community-domains-wrap")).to_be_visible()
        page.locator("#exploration-domains").fill("head-fi.org,\nnews.ycombinator.com")
        with page.expect_request(lambda request: request.url.endswith("/api/plan") and
                                 request.post_data_json.get("exploration_domains") == ["head-fi.org", "news.ycombinator.com"]):
            page.locator("#exploration-domains").press("Tab")
        expect(page.locator("#input-mode-badge")).to_contain_text("Toolbox web_search (Path 2)")
        page.locator("#webiq-key").fill("test-key-ui-check")
        expect(page.locator("#input-mode-badge")).to_contain_text("Web IQ Search (Path 1)")
        page.locator("#webiq-key").fill("")
        page.locator("#exec-mode-sample").check()
        expect(page.locator("#exec-mode-sample")).to_be_checked()
        expect(page.locator("#community-exploration")).not_to_be_checked()
        expect(page.locator("#community-exploration")).to_be_disabled()

        # Missing WebIQ credentials are explained before any research session is opened.
        page.locator("#exec-mode-webiq-browse").check()
        before = sum("/api/run" in url for url in requests)
        page.locator("#run-btn").click()
        expect(page.locator("#webiq-key")).to_be_focused()
        expect(page.locator("#status-strip")).to_contain_text("WebIQ Browse needs a key")
        assert page.locator("#status-strip").get_attribute("role") == "alert"
        assert sum("/api/run" in url for url in requests) == before
        page.locator("#exec-mode-sample").check()
        page.locator("#research-settings > summary").click()

        with page.expect_request("**/api/run") as sample_started:
            page.locator("#run-btn").click()
        page.wait_for_function("!RUNNING")
        assert sample_started.value.post_data_json["inspect_review_pages"] is True
        assert sample_started.value.post_data_json["community_exploration"] is False
        expect(page.locator("#verdict-card")).to_be_visible()
        expect(page.locator("#what-fits-list")).to_be_visible()
        expect(page.locator("#watch-outs-list")).to_be_visible()
        assert page.locator("#what-fits-list").evaluate("(element) => !element.closest('.timeline-step')")
        assert page.locator("#research-timeline .timeline-step[open]").count() == 0
        expect(page.locator("#comparison-card")).not_to_be_visible()
        expect(page.locator("#run-btn")).to_have_text("Preview sample")
        expect(page.locator("#load-example-btn")).to_be_enabled()
        expect(page.locator("#status-strip")).to_contain_text("Your brief is ready")
        expect(page.locator(".timeline-step")).to_have_count(5)
        expect(page.locator("#research-timeline .timeline-step")).to_have_count(4)
        assert page.locator("#timeline-approval").evaluate("(element) => !element.closest('#research-timeline')")
        expect(page.locator("#timeline-status-browsing")).to_contain_text("Not performed")
        expect(page.locator("#timeline-status-listing")).to_contain_text("Illustrative fixture")
        expect(page.locator("#run-comparisons")).to_contain_text("Sample runs are excluded")
        report_url = page.locator("#download-report-link").get_attribute("href")
        assert report_url
        assert context.request.get(base_url + report_url).status == 200

        for width in (320, 375, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            overflow = page.evaluate("""() => [...document.querySelectorAll('body *')]
                .filter(el => el.getClientRects().length && getComputedStyle(el).position !== 'fixed')
                .filter(el => !el.matches('.skip-link:not(:focus)'))
                .filter(el => !el.closest('.comparison-scroll'))
                .filter(el => {const r = el.getBoundingClientRect(); return r.left < -1 || r.right > innerWidth + 1;})
                .map(el => ({tag:el.tagName, id:el.id, cls:el.className})).slice(0,10)""")
            assert not overflow, f"Overflow at {width}px: {overflow}"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Horizontal scroll at {width}px"

        page.set_viewport_size({"width": 1280, "height": 900})
        page.locator("#timeline-listing > summary").click()
        first_card = page.locator("#timeline-captures-listing .shot-card").first
        first_card.click()
        expect(page.locator("dialog")).to_be_visible()
        for _ in range(4):
            page.keyboard.press("Tab")
            assert page.evaluate("""document.activeElement === document.body ||
                document.querySelector('dialog').contains(document.activeElement)""")
        page.locator("#run-btn").focus()
        expect(page.locator("#run-btn")).not_to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("dialog")).not_to_be_visible()
        expect(first_card).to_be_focused()

        page.locator("#timeline-approval > summary").click()
        page.locator("#approve-cart-btn").click()
        expect(page.locator("#cart-receipt-box")).to_contain_text("No Real Retailer Cart Modified")
        expect(page.locator("#approve-cart-btn")).to_be_disabled()
        page.get_by_role("button", name="Confirm purchases are blocked").click()
        expect(page.locator("#cart-receipt-box")).to_contain_text("HTTP 403")

        page.locator("#product-url").fill("https://example.com/wh-1000xm6")
        page.locator("#user-intent").fill("Quiet daily use under $250")
        expect(page.locator("#input-mode-badge")).to_contain_text("Custom URL")
        page.locator("#run-btn").click()
        page.wait_for_function("!RUNNING")
        expect(page.locator("#baseline-meta")).to_contain_text("UNVERIFIED-CUSTOM-URL")
        expect(page.locator("#web-sources-badge")).to_contain_text("No external sources")
        expect(page.locator("#approve-cart-btn")).to_be_disabled()

        async_error = 'data: {"type":"error","message":"Simulated service failure"}\n\ndata: {"type":"done","outcome":"error"}\n\n'
        page.route("**/api/run", lambda route: route.fulfill(
            status=200, content_type="text/event-stream", body=async_error,
        ))
        page.locator("#run-btn").click()
        page.wait_for_function("!RUNNING")
        expect(page.locator("#status-strip")).to_contain_text("Simulated service failure")
        expect(page.locator("#download-report-link")).not_to_be_visible()
        assert not page.locator(".step-item.done").count()
        page.unroute("**/api/run")
        page.route("**/api/run", lambda route: route.fulfill(
            status=200, content_type="text/event-stream", body='data: {"type":"status","stage":"start"}\n\n',
        ))
        page.locator("#run-btn").click()
        page.wait_for_function("!RUNNING")
        expect(page.locator("#status-strip")).to_contain_text("ended before completion")
        expect(page.locator("#run-btn")).to_be_enabled()
        expect(page.locator("#run-outcome")).to_contain_text("Failed")
        expect(page.locator("#timeline-status-discovery")).to_contain_text("Not reached")
        assert page.locator("#progress-listing").get_attribute("data-state") == "error"
        page.unroute("**/api/run")

        def replay(events):
            return "\n\n".join("data: " + json.dumps(event) for event in events) + "\n\n"

        def live_events(mode="live", restricted=False):
            return [
                {"type": "status", "stage": "digest_product_url"},
                {"type": "product_baseline", "baseline": {
                    "brand": "Observed Brand", "model": "Observed Model", "sku": "SKU-123", "price": None,
                    "data_provenance": "live_webiq_browse_restricted" if restricted else "live_bat_observed",
                    "seller_claims": ["A stated claim"], "limitation_notice": "HTTP 403 restriction respected" if restricted else "",
                }},
                {"type": "status", "stage": "webiq_search"},
                {"type": "query_plan", "query_plan": {"primary_webiq_query": "observed model owner review", "supplementary_queries": ["observed model long term"]}},
                {"type": "enrichment", "status": "ready", "provenance": "live_toolbox_web_search",
                 "web_query": "observed model owner review", "sources": [
                     {"url": "https://example.com/review", "title": "<script>window.injected=true</script>Independent review",
                      "platform": "Independent", "snippet": "Do not duplicate this legacy excerpt.", "provenance": "live_toolbox_web_search",
                      "provider": "toolbox_web_search", "retrieved_characters": 125, "content_display_limit": 200,
                      "retrieved_response": {"title": "Review", "url": "https://example.com/review",
                                             "content": "<script>window.injected=true</script><img src=x onerror=window.injected=true>Retrieved text",
                                             "crawledAt": "2026-10-20", "lastUpdatedAt": None}}
                 ], "video_sources": []},
                {"type": "status", "stage": "permitted_source_browsing"},
                {"type": "source_inspection", "inspection_method": mode, "total": 1, "source":
                    {"url": "https://example.com/review", "title": "Review", "platform": "Independent",
                     "inspection_method": mode, "status": "inspecting", "access_status": "inspecting", "fetched_characters": 0}},
                {"type": "browsed_sources", "inspection_method": mode, "sources": [
                    {"url": "https://example.com/review", "title": "Review", "platform": "Independent",
                     "inspection_method": mode, "fetched_characters": 0 if restricted else 1234,
                     "access_status": "restricted" if restricted else "inspected", "restriction_reason": "HTTP 403" if restricted else "",
                     "extracted_summary": "Not inspected" if restricted else "Observed page text",
                     "content_display_limit": 200 if mode == "webiq_browse" else 3200,
                     "retrieved_response": {"url": "https://example.com/review", "title": "Review",
                                            "content": "" if restricted else "Actual extracted browser text <script>window.injected=true</script>",
                                            "headings": ["Observed heading"], "status": "restricted" if restricted else "inspected",
                                            "inspection_method": mode}}
                ]},
                {"type": "status", "stage": "synthesize_verdict"},
                {"type": "claim_matrix", "run_mode": "live_test", "executive_summary": "Grounded test summary",
                 "recommendation_badge": "More evidence needed", "matrix": [], "evidence_gaps": ["Unverified ownership duration"]},
                {"type": "done", "outcome": "partial", "run_mode": "live_test"}
            ]

        # Replayed live contracts exercise the UI without contacting Azure or third-party sites.
        page.evaluate("setExecutionMode('live')")
        page.route("**/api/run", lambda route: route.fulfill(status=200, content_type="text/event-stream", body=replay(live_events())))
        with page.expect_request("**/api/run") as live_started:
            page.locator("#run-btn").click()
        page.wait_for_function("!RUNNING")
        assert live_started.value.post_data_json["inspect_review_pages"] is True
        assert live_started.value.post_data_json["execution_mode"] == "live"
        expect(page.locator("#run-options")).to_contain_text("Azure BAT Tool")
        expect(page.locator("#run-comparisons")).to_contain_text("Run 1: Azure BAT Tool")
        expect(page.locator("#run-btn")).to_have_text("Start research")
        expect(page.locator("#evidence-gaps-list")).to_be_visible()
        expect(page.locator("#evidence-gaps-list")).to_contain_text("Unverified ownership duration")
        assert page.locator("#progress-synthesis").get_attribute("data-state") == "limited"
        assert page.locator(".progress-track [aria-current='step']").count() == 0
        expect(page.locator("#timeline-events-discovery")).to_have_count(1)
        page.locator("#timeline-discovery > summary").click()
        expect(page.locator("#timeline-evidence-discovery")).to_contain_text("Executed web query")
        page.locator("#web-sources-list .retrieved-response > summary").click()
        expect(page.locator("#web-sources-list pre")).to_contain_text("<script>window.injected=true</script>")
        expect(page.locator("#web-sources-list pre")).to_contain_text('"crawledAt"')
        expect(page.locator("#web-sources-list")).not_to_contain_text("Do not duplicate this legacy excerpt")
        expect(page.locator("#web-sources-list .item-captures")).to_contain_text("Search retrieves text, not screenshots")
        assert page.locator("#web-sources-list .retrieved-response img, #web-sources-list .retrieved-response script").count() == 0
        assert page.evaluate("window.injected === undefined")
        page.unroute("**/api/run")

        # Genuine captures associate by artifact ID or exact observed URL, in either arrival order.
        pixel = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII="
        page.evaluate("""pixel => {
            appendScreenshot({id:'before', content_url:pixel, is_live_browser_capture:true,
                title:'Before-source viewport', sequence:1, stage:'permitted_source_browsing'});
            renderBrowsedSources([{url:'https://example.com/by-id', title:'ID matched review',
                inspection_method:'live', access_status:'inspected', screenshot_artifact_id:'before',
                retrieved_response:{content:'Extracted text, not a digest.'}}]);
        }""", pixel)
        page.locator("#timeline-browsing").evaluate("(element) => element.open = true")
        page.locator("#browsed-sources-list .retrieved-response > summary").click()
        page.locator("#browsed-sources-list .item-capture-link").click()
        expect(page.locator("dialog")).to_be_visible()
        expect(page.locator("#modal-title")).to_contain_text("Before-source viewport")
        page.keyboard.press("Escape")
        page.evaluate("""pixel => {
            renderBrowsedSources([{url:'https://example.com/by-url', title:'URL matched review',
                inspection_method:'live', access_status:'inspected'}]);
            appendScreenshot({id:'after', observed_url:'https://example.com/by-url', content_url:pixel,
                is_live_browser_capture:true, title:'After-source viewport', sequence:2, stage:'permitted_source_browsing'});
        }""", pixel)
        expect(page.locator("#browsed-sources-list .item-capture-link")).to_have_count(1)

        page.evaluate("setExecutionMode('webiq_browse')")
        page.evaluate("document.getElementById('webiq-key').value = 'request-only-test-key'; onWebIqKeyChanged()")
        page.route("**/api/run", lambda route: route.fulfill(status=200, content_type="text/event-stream", body=replay(live_events("webiq_browse", True))))
        page.locator("#run-btn").click()
        page.wait_for_function("!RUNNING")
        expect(page.locator("#timeline-status-listing")).to_contain_text("Restricted / unverified")
        expect(page.locator("#run-comparisons")).to_contain_text("Run 2: WebIQ Browse")
        expect(page.locator("#comparison-warning")).to_contain_text("not a controlled")
        assert "request-only-test-key" not in page.locator("#run-comparisons").inner_text()
        assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
        page.locator("#research-settings").evaluate("(element) => element.open = false")
        page.get_by_role("button", name="Expand all", exact=True).click()
        expect(page.locator("#browsed-sources-list")).to_contain_text("HTTP 403")
        expect(page.locator("#review-inspection-heading")).to_contain_text("WebIQ Browse")
        expect(page.locator("#browsed-sources-list")).to_contain_text("WebIQ Browse")
        expect(page.locator("#browsed-sources-list")).to_contain_text("Grounding text is no longer available after the workflow")
        for width in (320, 375, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Timeline/comparison overflow at {width}px"
        page.get_by_role("button", name="Collapse all", exact=True).click()
        assert page.locator(".timeline-step[open]").count() == 0
        page.locator("#timeline-listing > summary").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#baseline-meta")).to_be_visible()
        page.unroute("**/api/run")

        # WebIQ projections are capped across duplicate URLs and discarded on errors, not saved in caches.
        page.evaluate("""() => {
            beginResearchTimeline(selectedResearchOptions());
            const source = {url:'https://example.com/grounding', title:'Attribution survives', provider:'webiq',
                snippet:'Legacy WebIQ snippet must disappear', content_display_limit:200,
                retrieved_response:{title:'Attribution survives', content:'WebIQ raw grounding '.repeat(40)}};
            handleSseEvent({type:'enrichment', provenance:'live_webiq', sources:[source, source]});
            handleSseEvent({type:'source_inspection', total:1, inspection_method:'webiq_browse',
                source:{...source, inspection_method:'webiq_browse', status:'inspected'}});
        }""")
        assert page.evaluate("""[...document.querySelectorAll('[data-webiq-source]')]
            .reduce((sum,node) => sum + Number(node.dataset.groundingCharacters || 0),0) <= 200""")
        expect(page.locator("#web-sources-list")).not_to_contain_text("Legacy WebIQ snippet must disappear")
        page.evaluate("handleSseEvent({type:'error', message:'Fixture stop'})")
        expect(page.locator("#web-sources-list")).not_to_contain_text("WebIQ raw grounding")
        expect(page.locator("#web-sources-list")).to_contain_text("Grounding text is no longer available after the workflow")
        expect(page.locator("#web-sources-list")).to_contain_text("Attribution survives")
        assert page.evaluate("ACTIVE_RESEARCH.inspectionRows.size === 0")
        # This manual presentation fixture is not added to the real tab comparison.
        page.evaluate("ACTIVE_RESEARCH = null")

        # The fifth UI strategy maps to a real seed inspector, never execution_mode='search_only'.
        page.route("**/api/plan", lambda route: route.fulfill(status=200, json={"plan": {"steps": []}}))
        with page.expect_request(lambda request: request.url.endswith("/api/plan") and
                                 request.post_data_json.get("inspect_review_pages") is False) as planned:
            page.evaluate("setExecutionMode('search_only')")
        assert planned.value.post_data_json["execution_mode"] == "webiq_browse"
        expect(page.locator("#initial-inspector")).to_have_value("webiq_browse")
        page.locator("#research-settings").evaluate("(element) => element.open = true")
        expect(page.locator("#initial-inspector-wrap")).to_be_visible()
        page.locator("#webiq-key").fill("")
        before = sum("/api/run" in url for url in requests)
        page.locator("#run-btn").click()
        expect(page.locator("#webiq-key")).to_be_focused()
        assert sum("/api/run" in url for url in requests) == before
        with page.expect_request(lambda request: request.url.endswith("/api/plan") and
                                 request.post_data_json.get("execution_mode") == "live" and
                                 request.post_data_json.get("inspect_review_pages") is False) as planned:
            page.locator("#initial-inspector").select_option("live")
        assert planned.value.post_data_json["use_client_browser"] is False
        expect(page.locator("#input-mode-badge")).to_contain_text("Listing: Azure BAT Tool")
        search_events = [event for event in live_events() if event["type"] not in ("source_inspection", "browsed_sources")]
        search_events.insert(-3, {"type": "inspection_skipped", "stage": "permitted_source_browsing",
                                 "reason": "Search-only reviews selected; discovered pages were not opened.", "discovered": 1})
        search_events[-2]["evidence_metrics"] = {"search_content_characters": 12345,
                                                 "inspected_context_characters": 0, "discovered_sources": 1,
                                                 "inspected_sources": 0, "inspect_review_pages": False}
        page.route("**/api/run", lambda route: route.fulfill(status=200, content_type="text/event-stream", body=replay(search_events)))
        with page.expect_request("**/api/run") as started:
            page.locator("#run-btn").click()
        payload = started.value.post_data_json
        assert payload["execution_mode"] == "live" and payload["inspect_review_pages"] is False
        page.wait_for_function("!RUNNING")
        expect(page.locator("#run-options")).to_contain_text("Search-only reviews")
        expect(page.locator("#run-options")).to_contain_text("Listing only: Azure BAT Tool")
        expect(page.locator("#timeline-status-browsing")).to_have_text("Skipped by choice")
        expect(page.locator("#timeline-hint-browsing")).to_contain_text("0 review pages opened")
        expect(page.locator("#run-comparisons")).to_contain_text("Search-only reviews")
        expect(page.locator("#run-comparisons")).to_contain_text("12345 / 0 characters")
        assert page.locator("#progress-browsing").get_attribute("data-state") == "skipped"
        assert page.evaluate("ACTIVE_RESEARCH.inspected === 0 && ACTIVE_RESEARCH.restricted === 0")
        assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
        page.unroute("**/api/run")
        page.unroute("**/api/plan")

        page.route("**/api/run", lambda route: route.fulfill(status=200, content_type="text/event-stream", body=async_error))
        for _ in range(3):
            page.locator("#run-btn").click()
            page.wait_for_function("!RUNNING")
        assert page.locator(".comparison-table thead th").count() == 6  # Row heading + last five live runs
        page.unroute("**/api/run")
        page.evaluate("COMPARISON_RUNS.forEach(run => run.expires = 0); renderRunComparisons()")
        expect(page.locator("#run-comparisons")).not_to_contain_text("Run 2:")
        page.locator("#clear-comparisons").is_disabled()

        # The shared extractor changes with the viewport, not the first body-text prefix.
        fixture = context.new_page()
        fixture.set_content("""<style>body{margin:8px}.spacer{height:1400px}</style>
            <p>Visible synthetic product-discussion introduction.</p>
            <a href="https://forum.example.com/thread">Public owner thread</a>
            <a hidden href="https://forum.example.com/hidden">Hidden link</a>
            <p class="commtext" hidden>Hidden synthetic comment must not be collected.</p>
            <div class="spacer"></div>
            <p class="commtext">Synthetic lower-page owner assertion: hinge durability needs investigation.</p>""")
        extractor = (Path(__file__).resolve().parents[2] / "browser_extension" / "community_snapshot.js").read_text(encoding="utf-8")
        before = fixture.evaluate(f"({extractor})()")
        assert before["community_capture_status"] == "ready"
        assert before["discussion_blocks"] == []
        assert before["observed_links"] == [{"url": "https://forum.example.com/thread", "text": "Public owner thread"}]
        fixture.evaluate("window.scrollBy(0, 1200)")
        after = fixture.evaluate(f"({extractor})()")
        assert after["scroll_y"] > before["scroll_y"]
        assert len(after["discussion_blocks"]) == 1
        assert "hinge durability" in after["viewport_text"]
        assert "Hidden synthetic" not in json.dumps(after)
        assert fixture.screenshot(type="png").startswith(b"\x89PNG\r\n\x1a\n")
        fixture.close()

        # Each model action retains its own escaped text and exact capture ID.
        page.evaluate("""pixel => {
            setExecutionMode('live');
            document.getElementById('community-exploration').checked = true;
            beginResearchTimeline(selectedResearchOptions());
            handleSseEvent({type:'status',stage:'permitted_source_browsing'});
            handleSseEvent({type:'community_exploration',phase:'decision',step:1,action:'visit',
                reason:'<img src=x onerror=window.communityInjected=true>',goal:'Check durability',
                url:'https://forum.example.com/thread'});
            handleSseEvent({type:'source_inspection',inspection_method:'live',total:1,source:{
                url:'https://forum.example.com/thread',title:'Synthetic community thread',platform:'Fixture forum',
                status:'inspected',access_status:'inspected',inspection_method:'live',agent_explored:true,
                discovery_origin:'observed_page_link',parent_url:'https://forum.example.com/',
                discussion_blocks_observed:2,screenshot_artifact_id:'community-two',
                exploration_steps:[
                    {step:1,action:'visit',goal:'Find owner context',reason:'Seek long-term use',status:'inspected',
                     screenshot_artifact_id:'community-one',viewport_text:'Synthetic first viewport'},
                    {step:2,action:'scroll',goal:'Inspect more discussion',reason:'Look below the introduction',status:'inspected',
                     screenshot_artifact_id:'community-two',
                     visible_discussion_blocks:['Synthetic comment <script>window.communityInjected=true</script>'],
                     viewport_text:'Synthetic second viewport'}],
            }});
            appendScreenshot({id:'community-one',url:'https://forum.example.com/thread',content_url:pixel,
                is_live_browser_capture:true,title:'Synthetic step one viewport',stage:'permitted_source_browsing'});
            appendScreenshot({id:'community-two',url:'https://forum.example.com/thread',content_url:pixel,
                is_live_browser_capture:true,title:'Synthetic step two viewport',stage:'permitted_source_browsing'});
        }""", pixel)
        page.locator("#timeline-browsing").evaluate("(element) => element.open = true")
        expect(page.locator(".community-decision")).to_contain_text("<img")
        expect(page.locator(".community-observation")).to_have_count(2)
        for index, label in ((0, "Synthetic step one viewport"), (1, "Synthetic step two viewport")):
            step = page.locator(".community-observation").nth(index)
            step.evaluate("(element) => element.open = true")
            step.locator(".retrieved-response").evaluate("(element) => element.open = true")
            expect(step.locator(".item-capture-link")).to_have_count(1)
            expect(step.locator(".item-capture-link")).to_contain_text(label)
        expect(page.locator(".community-observation").nth(1).locator("pre")).to_contain_text("<script>")
        assert page.evaluate("window.communityInjected === undefined")
        for width in (320, 375, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Community trace overflow at {width}px"
        page.evaluate("ACTIVE_RESEARCH = null")
        page.reload()
        # Report fixtures stay in this browser response, never in published recordings.
        report_path = Path(__file__).resolve().parents[1] / "app" / "benchmark_report.html"
        recording = {
            "source_commit": "a" * 40,
            "round_id": "synthetic-round",
            "comparison_note": "Synthetic operational comparison; live sources can differ.",
            "cases": [{"id": "synthetic-report-contract"}],
            "runs": [{
                "case_id": "synthetic-report-contract", "method": "webiq_browse",
                "outcome": "partial", "elapsed_s": 2, "started_at": "synthetic browser fixture",
                "deployment": "test-only", "profile": "test-only",
                "events": [
                    {"type": "status", "stage": "digest_product_url", "elapsed_s": 0},
                    {"type": "source_inspection", "inspector": "webiq_browse", "status": "restricted",
                     "fetched_characters": 0, "url": "<img src=x onerror=window.reportInjected=true>", "elapsed_s": 1},
                    {"type": "done", "outcome": "partial", "elapsed_s": 2},
                ],
            }],
        }
        recording["runs"].insert(0, {
            **recording["runs"][0], "started_at": "earlier synthetic attempt", "outcome": "error",
            "events": [{"type": "done", "outcome": "error", "elapsed_s": 1}],
        })
        recording["runs"][-1]["visual_evidence_id"] = "a" * 12
        for method in ("live", "client_browser", "vm_playwright", "search_only"):
            recording["runs"].append({
                **recording["runs"][1], "method": method,
                "server_method": "live" if method == "search_only" else method,
                "inspect_review_pages": method != "search_only",
                "server_revision": "test-only-newer" if method == "search_only" else "ca-pdd-portal--0000022",
                **({"evidence_metrics": {"search_content_characters": 9876, "inspected_context_characters": 0,
                                         "discovered_sources": 2, "inspected_sources": 0, "inspect_review_pages": False},
                    "events": [
                        {"type": "enrichment", "provider": "webiq", "urls": ["https://example.com/fixture"], "elapsed_s": 0},
                        {"type": "inspection_skipped", "reason": "Search-only reviews selected; discovered pages were not opened.", "elapsed_s": 1},
                        {"type": "done", "outcome": "partial", "elapsed_s": 2},
                    ]} if method == "search_only" else {}),
            })
        browser_run = next(run for run in recording["runs"] if run["method"] == "live")
        browser_run["inspections"] = [{
            "url": "https://example.com/fixture", "status": "restricted_by_site",
            "verification_attempts": 2, "verification_outcome": "unresolved",
        }]
        browser_run.update(
            community_exploration=True, exploration_domains=["forum.example.com"],
            community_outcome={"status": "completed", "actions": 4, "searches": 1,
                               "pages": 1, "new_pages": 1, "discussion_blocks": 3},
        )
        browser_run["events"] = [
            browser_run["events"][0],
            {"type": "status", "stage": "permitted_source_browsing", "elapsed_s": 0.4},
            browser_run["events"][1],
            {"type": "status", "stage": "synthesize_verdict", "elapsed_s": 1.6},
            browser_run["events"][-1],
        ]
        asset = "/api/replay/assets/" + "b" * 64 + ".png"
        visual_fixture = {
            "saved": {"baseline": {"brand": "Observed fixture brand", "model": "<img src=x onerror=window.reportInjected=true>", "claims": ["Observed seller claim"],
                                   "screenshot_artifact_id": "fixture-shot",
                                   "retrieved_response": {"title": "Listing", "content": "<script>window.reportInjected=true</script>Actual retained browser text"}},
                      "screenshots": [{"asset": asset, "artifact_id": "fixture-shot", "title": "Test-only capture", "stage": "listing_digestion"}],
                      "search_sources": [
                          {"url": "https://example.com/fixture", "provider": "webiq", "title": "WebIQ attribution",
                           "retrieved_response": {"content": "Forbidden retained WebIQ grounding"}},
                          {"url": "https://example.com/toolbox", "provider": "toolbox_web_search", "title": "Toolbox attribution",
                           "retrieved_response": {"content": "<img src=x onerror=window.reportInjected=true>Safe JSON text", "crawledAt": None}},
                      ],
                      "sources": [{"url": "https://example.com/fixture", "status": "restricted", "fetched_characters": 0, "reason": "Fixture restriction"}],
                      "community_actions": [{"step": 1, "action": "search", "domain": "forum.example.com",
                                             "goal": "Synthetic durability goal",
                                             "reason": "<img src=x onerror=window.reportInjected=true>"}]},
            "temporary": {"synthesis": {"summary": "Temporary understood result", "gaps": ["Unverified long-term ownership"]}},
            "temporary_status": "available", "temporary_expires_at": 9999999999,
        }
        page.route("**/api/replay/evidence/*", lambda route: route.fulfill(status=200, json=visual_fixture))
        import base64
        page.route("**/api/replay/assets/*", lambda route: route.fulfill(
            status=200, content_type="image/png",
            body=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII="),
        ))
        report = re.sub(
            r'(<script id="benchmark-run-data" type="application/json">).*?(</script>)',
            lambda match: match[1] + json.dumps(recording).replace("<", "\\u003c") + match[2],
            report_path.read_text(encoding="utf-8"), flags=re.S,
        )
        page.route("**/benchmark-report-test", lambda route: route.fulfill(
            status=200, content_type="text/html", body=report,
        ))
        replay_requests = []
        replay_methods = []
        page.on("request", lambda request: replay_requests.append(request.url))
        page.on("request", lambda request: replay_methods.append(request.method))
        page.goto(base_url + "/benchmark-report-test")
        expect(page.locator("#current-results tr")).to_have_count(1)
        expect(page.locator("#method-summary tr")).to_have_count(5)
        expect(page.locator("#latest-outcomes")).to_contain_text("5 partial, 0 execution errors")
        expect(page.locator("#current-results tr td")).to_have_count(6)
        expect(page.locator("#context-comparison tr")).to_have_count(5)
        expect(page.locator("#community-comparison tr")).to_have_count(5)
        expect(page.locator("#community-comparison")).to_contain_text("4 / 1")
        expect(page.locator("#community-comparison")).to_contain_text("Not supported by this path")
        expect(page.locator("#community-recording-note")).to_contain_text("forum.example.com")
        expect(page.locator("#context-comparison")).to_contain_text("9876 (measured)")
        expect(page.locator("#context-comparison")).to_contain_text("estimated historical upper bound")
        expect(page.locator("#context-comparison")).to_contain_text("1.20s (stage interval)")
        expect(page.locator("#context-comparison")).to_contain_text("2 rechecks; 0 cleared, 1 unresolved")
        expect(page.locator("#comparison-note")).to_have_text(recording["comparison_note"])
        expect(page.locator("#recording-date")).to_contain_text("synthetic-round")
        expect(page.locator("#replay-context")).to_contain_text("Search-only reviews")
        expect(page.locator("#replay-context")).to_contain_text("test-only-newer")
        expect(page.locator(".visual-step")).to_have_count(4)
        expect(page.locator("#visual-replay-panel")).to_contain_text("Observed fixture brand")
        expect(page.locator("#visual-replay-panel .item-captures img")).to_have_count(1)
        expect(page.locator("#visual-replay-panel")).to_contain_text("skipped by choice")
        expect(page.locator("#visual-replay-panel")).not_to_contain_text("Forbidden retained WebIQ grounding")
        expect(page.locator("#visual-replay-panel")).to_contain_text("Grounding text is no longer available after the workflow")
        expect(page.locator("#visual-replay-panel pre")).to_contain_text([
            "<script>window.reportInjected=true</script>", "<img src=x onerror=window.reportInjected=true>"
        ])
        assert page.locator("#visual-replay-panel pre script, #visual-replay-panel pre img").count() == 0
        expect(page.get_by_role("button", name=re.compile("Replay earlier attempt"))).to_have_count(0)
        expect(page.locator("#current-results")).not_to_contain_text("error |")
        expect(page.get_by_role("button", name="Replay recorded run", exact=True)).to_have_count(5)
        page.evaluate("selectRun(recording.runs.find(run => run.method === 'live'))")
        expect(page.locator(".community-actions")).to_have_count(1)
        expect(page.locator(".community-actions")).to_contain_text("Synthetic durability goal")
        expect(page.locator(".community-actions")).to_contain_text("<img src=x onerror=window.reportInjected=true>")
        assert page.locator(".community-actions img").count() == 0
        expect(page.locator("#replay-context")).to_contain_text("Community exploration enabled for: forum.example.com")
        assert page.evaluate("""() => {
            const host = document.createElement('div');
            responseViewer(host, {url:'https://example.com/thread', require_capture_id:true},
                'inspection', [{url:'https://example.com/thread', asset:'/api/replay/assets/' + 'b'.repeat(64) + '.png'}]);
            return host.querySelectorAll('img').length === 0 && host.textContent.includes('No matching genuine screenshot');
        }""")
        page.get_by_role("button", name="Replay recorded run", exact=True).last.click()
        expect(page.locator("#replay-position")).to_have_text("0/3 recorded events")
        expect(page.locator(".visual-step")).to_have_count(4)
        page.locator("#replay-next").evaluate("(element) => element.closest('details').open = true")
        page.locator("#replay-next").click()
        expect(page.locator("#replay-position")).to_have_text("1/3 recorded events")
        page.locator("#replay-all").click()
        expect(page.locator("#replay-position")).to_have_text("3/3 recorded events")
        assert page.evaluate("window.reportInjected === undefined")
        page.locator("#replay-play").click()
        page.locator("#replay-pause").click()
        assert page.evaluate("replayTimer === null")
        assert not any("/api/run" in url for url in replay_requests), replay_requests
        assert all(method == "GET" for method in replay_methods), replay_methods
        assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
        visual_fixture["temporary"] = None
        visual_fixture["temporary_status"] = "expired_or_not_retained"
        page.evaluate("loadVisualReplay(replayRun)")
        expect(page.locator("#visual-replay-panel")).to_contain_text("expired or was not retained")
        expect(page.locator("#visual-replay-panel")).not_to_contain_text("Temporary understood result")
        page.evaluate("loadVisualReplay({...replayRun, visual_evidence_id:null})")
        expect(page.locator("#visual-replay-panel")).to_contain_text("Metadata-only recording")
        expect(page.locator("#visual-replay-panel")).to_contain_text("skipped by choice")
        # Missing retained evidence remains explicit and does not invoke a research session.
        page.route("**/api/replay/evidence/*", lambda route: route.fulfill(status=410, json={"detail": "Expired"}))
        page.evaluate("loadVisualReplay(replayRun)")
        expect(page.locator("#visual-replay-panel")).to_contain_text("HTTP 410")
        expect(page.locator("#visual-replay-panel")).to_contain_text("No content or screenshots are substituted")
        for width in (320, 375, 768, 1280):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Report overflow at {width}px"
        assert not errors, errors
        assert not any("/api/video/" in url for url in requests), requests
        print(json.dumps({"status": "passed", "responsive_widths": [320, 375, 768, 1280], "page_errors": errors}))
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
