"""Synthetic controller/protocol tests; no publisher content or live inference."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import bat_research as bat
import community_exploration as community
import main
import pipeline
import webiq
from fastapi.testclient import TestClient


def enrichment(url: str = "https://example.com/review") -> webiq.Enrichment:
    return webiq.normalize_response({"webResults": [
        {"url": url, "title": "Synthetic owner discussion", "content": "Synthetic initial search evidence."},
    ]}, "Synthetic product reliability", provenance="live_webiq")


def baseline():
    value = bat.build_unverified_seed_baseline("https://example.com/product", "Long-term reliability")
    value.brand, value.model = "Synthetic", "Headphone"
    value.data_provenance = "live_bat_observed"
    return value


def decision(action: str, **kwargs):
    return community.CommunityDecision(
        action=action, reason="Seek evidence for long-term use.", goal="Long-term reliability", **kwargs,
    )


def observation(url="https://forum.example.com/", **kwargs):
    return bat.PageObservation(
        url=url, title="Synthetic public discussion", status_code=200, headings=["Public discussion"],
        visible_text_excerpt="Synthetic page body", price_candidates=[], sku_candidates=[], claim_candidates=[],
        community_capture_status="ready", viewport_text="Synthetic rendered viewport", **kwargs,
    )


class TestCommunityScope(unittest.TestCase):
    def test_domain_boundaries_and_readonly_routes(self):
        self.assertEqual(community.normalize_domains([" Reddit.COM ", "reddit.com", "forum.example.com"]),
                         ["reddit.com", "forum.example.com"])
        for domain in ("https://reddit.com", "*.reddit.com", "reddit.com/x", "reddit.com:443",
                       "127.0.0.1", "localhost", "reddit.com@evil.example"):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                community.normalize_domains([domain])
        for url in (
            "https://evilreddit.com/thread", "https://reddit.com.evil.example/thread",
            "https://user@reddit.com/thread", "https://reddit.com:8443/thread", "http://reddit.com/thread",
            "https://reddit.com/checkout", "https://reddit.com/%63heckout", "https://reddit.com/%252fcheckout",
            "https://reddit.com/cart/add", "https://reddit.com/reply", "https://reddit.com/thread?action=delete",
            "https://reddit.com/billing", "https://reddit.com/one-click",
            "https://reddit.com/hide?id=123", "https://reddit.com/fave?id=123",
            "https://reddit.com/flag?id=123", "https://reddit.com/settings",
            "https://reddit.com/movie.mp4", "https://reddit.com/file.zip",
        ):
            with self.subTest(url=url):
                self.assertIsNone(community.domain_for_url(url, ["reddit.com"]))
        self.assertEqual(community.domain_for_url("https://www.reddit.com/r/audio/comments/public/", ["reddit.com"]),
                         "reddit.com")
        self.assertEqual(community.domain_for_url("https://www.youtube.com/watch?v=review", ["youtube.com"]),
                         "youtube.com")
        self.assertEqual(community.domain_for_url("https://www.amazon.com/product-reviews/ASIN", ["amazon.com"]),
                         "amazon.com")

    def test_opt_in_plan_preserves_open_discovery_and_four_stages(self):
        plan = pipeline.build_agent_plan("https://example.com/product", "Reliability", execution_mode="live",
            community_exploration=True, exploration_domains=["forum.example.com"])
        self.assertEqual(len(plan["steps"]), 4)
        self.assertIn("independent labs", plan["steps"][1]["objective"])
        self.assertEqual(plan["exploration_domains"], ["forum.example.com"])
        self.assertEqual(plan["exploration_limits"]["actions"], 12)
        self.assertIn("explore", plan["steps"][2]["title"])
        for mode, inspect in (("sample", True), ("webiq_browse", True), ("live", False)):
            with self.subTest(mode=mode, inspect=inspect), self.assertRaises(ValueError):
                pipeline.build_agent_plan("https://example.com/product", "Reliability", execution_mode=mode,
                    inspect_review_pages=inspect, community_exploration=True)
        normal = pipeline.build_agent_plan("https://example.com/product", "Reliability", execution_mode="live")
        self.assertFalse(normal["community_exploration"])

    def test_api_rejects_bad_options_before_browser_start(self):
        with TestClient(main.app, base_url="http://127.0.0.1:8011") as client, \
             patch.object(pipeline, "inspect_product_listing_live", new=AsyncMock()) as inspector:
            body = {"product_url": "https://example.com/product", "user_intent": "Reliability",
                    "execution_mode": "webiq_browse", "community_exploration": True}
            self.assertEqual(client.post("/api/plan", json=body).status_code, 400)
            self.assertEqual(client.post("/api/run", json=body).status_code, 400)
            body.update(execution_mode="live", exploration_domains=["https://reddit.com"])
            self.assertEqual(client.post("/api/run", json=body).status_code, 422)
            inspector.assert_not_awaited()
            contract = client.get("/api/research-contract").json()["community_exploration"]
            self.assertFalse(contract["default_enabled"])
            self.assertEqual(contract["supported_inspectors"], ["live", "client_browser", "vm_playwright"])


class TestCommunityController(unittest.IsolatedAsyncioTestCase):
    async def run_controller(self, decisions, observations=(), *, sources=None, driver=None, max_actions=12,
                             timeout_seconds=240):
        driver = driver or AsyncMock()
        driver.observe.side_effect = list(observations)
        invoke = AsyncMock(side_effect=list(decisions))
        model = MagicMock()
        model.with_structured_output.return_value.ainvoke = invoke
        rows, events = [], []
        outcome = community.ExplorationOutcome()

        async def emit(event):
            events.append(json.loads(json.dumps(event)))

        with patch.object(community, "get_foundry_credential", return_value=object()), \
             patch.object(community, "_init_foundry_chat_model", return_value=model), \
             patch.object(community, "validate_public_https_url", side_effect=lambda url, **_: url):
            await community.explore_communities(
                driver=driver, rows=rows, baseline=baseline(), user_intent="Long-term reliability",
                enrichment=sources if sources is not None else enrichment(), domains=["forum.example.com"],
                execution_mode="live", webiq_key="request-scoped-secret", emit=emit, outcome=outcome,
                max_actions=max_actions,
                timeout_seconds=timeout_seconds,
            )
        return driver, invoke, rows, events, outcome

    async def test_model_follows_observed_link_beyond_search_results_and_scrolls(self):
        thread = "https://forum.example.com/threads/headphone-owner-feedback"
        _, invoke, rows, events, outcome = await self.run_controller([
            decision("visit", target_id="p1"), decision("visit", target_id="p2"),
            decision("scroll"), decision("finish"),
        ], [
            observation(observed_links=[{"url": thread, "text": "Long-term headphone ownership"}]),
            observation(thread, discussion_blocks=["Synthetic owner assertion: hinges failed after travel."]),
            observation(thread, discussion_blocks=["Synthetic second assertion: replacement service was slow."],
                        scroll_y=700, at_page_end=True),
        ])
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.pages, 2)
        self.assertEqual(outcome.new_pages, 2)
        self.assertEqual(outcome.discussion_blocks, 2)
        self.assertEqual(rows[1].parent_url, "https://forum.example.com/")
        self.assertEqual(rows[1].discovery_origin, "observed_page_link")
        self.assertIn("replacement service", rows[1].page_content)
        self.assertEqual([step["action"] for step in rows[1].exploration_steps], ["visit", "scroll"])
        self.assertIn("Long-term headphone ownership", invoke.await_args_list[1].args[0][1][1])
        self.assertNotIn("request-scoped-secret", json.dumps(events))
        self.assertTrue(all(event.get("discussion_blocks", 0) == int(event.get("discussion_blocks", 0))
                            for event in events if event["type"] == "community_exploration"))

    async def test_domain_scoped_search_adds_candidates_but_not_out_of_domain_results(self):
        found = webiq.normalize_response({"webResults": [
            {"url": "https://forum.example.com/owners", "title": "Owner discussion", "content": "Do not retain this grounding."},
            {"url": "https://evil.example/review", "title": "Out of scope", "content": "Do not visit."},
        ]}, "owners", provenance="live_webiq")
        with patch.object(community, "search_webiq", new=AsyncMock(return_value=found)) as search:
            driver, invoke, rows, events, outcome = await self.run_controller([
                decision("search", domain="forum.example.com", query="owner problems"),
                decision("visit", target_id="p2"), decision("finish"),
            ], [observation("https://forum.example.com/owners")])
        self.assertEqual(search.await_args.args[0], "request-scoped-secret")
        self.assertIn("site:forum.example.com", search.await_args.args[1])
        self.assertNotIn("evil.example", invoke.await_args_list[1].args[0][1][1])
        self.assertNotIn("Do not retain this grounding.", json.dumps(events))
        self.assertEqual(driver.goto.await_args.args[0], "https://forum.example.com/owners")
        self.assertEqual(outcome.searches, 1)

    async def test_empty_initial_search_still_explores_configured_domain(self):
        empty = webiq.normalize_response({}, "nothing", provenance="live_webiq")
        driver, _, rows, _, outcome = await self.run_controller(
            [decision("visit", target_id="p1"), decision("finish")], [observation()], sources=empty,
        )
        self.assertEqual(driver.goto.await_count, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(outcome.status, "completed")

    async def test_premature_finish_gets_explicit_correction_not_scripted_navigation(self):
        driver, invoke, _, events, outcome = await self.run_controller([
            decision("finish"), decision("visit", target_id="p1"), decision("finish"),
        ], [observation()])
        self.assertEqual(driver.goto.await_count, 1)
        self.assertEqual(outcome.status, "completed")
        self.assertIn("No independent community action was attempted", invoke.await_args_list[1].args[0][1][1])
        self.assertTrue(any(event.get("status") == "rejected" for event in events))

    async def test_repeated_premature_finish_fails_honestly_without_browser_fallback(self):
        driver, _, _, events, outcome = await self.run_controller([decision("finish"), decision("finish")])
        driver.goto.assert_not_awaited()
        self.assertEqual(outcome.status, "failed")
        self.assertIn("declined independent community exploration twice", outcome.stop_reason)
        self.assertTrue(any(event.get("phase") == "error" for event in events))

    async def test_invented_target_and_out_of_scope_domain_fail_without_navigation(self):
        for action in (decision("visit", target_id="p999"),
                       decision("search", domain="evil.example", query="owners")):
            driver, _, _, events, outcome = await self.run_controller([action])
            driver.goto.assert_not_awaited()
            self.assertEqual(outcome.status, "failed")
            self.assertTrue(outcome.gaps)
            self.assertTrue(any(event.get("phase") == "error" for event in events))

    async def test_denial_stops_domain_and_cannot_be_retried(self):
        denied = observation(restricted=True, restriction_reason="HTTP 403 denial", restriction_kind="access_denied")
        driver, _, rows, _, outcome = await self.run_controller(
            [decision("visit", target_id="p1"), decision("visit", target_id="p2")], [denied],
            sources=enrichment("https://forum.example.com/first"),
        )
        self.assertEqual(driver.goto.await_count, 1)
        self.assertEqual(rows[0].status, "restricted_by_site")
        self.assertEqual(rows[0].page_content, "")
        self.assertEqual(outcome.status, "failed")

    async def test_time_budget_cancels_slow_model_without_fallback(self):
        async def slow_decision(*_args, **_kwargs):
            await asyncio.sleep(10)
            return decision("finish")

        model = MagicMock()
        model.with_structured_output.return_value.ainvoke = slow_decision
        events, rows = [], []
        outcome = community.ExplorationOutcome()
        emit = AsyncMock(side_effect=lambda event: events.append(event))
        with patch.object(community, "get_foundry_credential", return_value=object()), \
             patch.object(community, "_init_foundry_chat_model", return_value=model):
            await community.explore_communities(
                driver=AsyncMock(), rows=rows, baseline=baseline(), user_intent="Reliability",
                enrichment=enrichment(), domains=["forum.example.com"], execution_mode="live",
                webiq_key="", emit=emit, outcome=outcome, timeout_seconds=0.02,
            )
        self.assertEqual(outcome.status, "budget_exhausted")
        self.assertIn("time budget", outcome.stop_reason)
        self.assertEqual(rows, [])

    async def test_unsupported_old_bridge_reports_update_requirement(self):
        old = observation()
        old.community_capture_status = ""
        driver, _, rows, _, outcome = await self.run_controller([decision("visit", target_id="p1")], [old])
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(rows[0].status, "error")
        self.assertIn("Update/reload", outcome.stop_reason)
        self.assertEqual(rows[0].exploration_steps[0]["status"], "error")
        driver.scroll_down.assert_not_awaited()

    async def test_scope_redirect_discards_text_and_stops_domain(self):
        _, _, rows, _, outcome = await self.run_controller(
            [decision("visit", target_id="p1"), decision("finish")],
            [observation("https://evil.example/redirected", discussion_blocks=["Must not become evidence."])],
        )
        self.assertEqual(rows[0].status, "skipped_out_of_scope")
        self.assertNotIn("Must not become evidence.", json.dumps(rows[0].to_dict()))
        self.assertEqual(outcome.discussion_blocks, 0)

    async def test_budget_and_model_failure_are_explicit_not_scripted_fallbacks(self):
        _, _, _, _, outcome = await self.run_controller(
            [decision("visit", target_id="p1")], [observation()], max_actions=1,
        )
        self.assertEqual(outcome.status, "budget_exhausted")
        driver, _, rows, events, outcome = await self.run_controller(
            [RuntimeError("model unavailable request-scoped-secret")],
        )
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(rows, [])
        self.assertNotIn("request-scoped-secret", json.dumps(events))
        driver.goto.assert_not_awaited()

    async def test_default_twelve_action_and_six_page_budgets(self):
        actions, observations = [], []
        for index in range(1, 7):
            url = "https://forum.example.com/" if index == 1 else f"https://forum.example.com/thread-{index}"
            links = [{"url": f"https://forum.example.com/thread-{index + 1}", "text": "More owner experiences"}]
            actions.extend([decision("visit", target_id=f"p{index}"), decision("scroll")])
            observations.extend([
                observation(url, observed_links=links),
                observation(url, observed_links=links, scroll_y=700),
            ])
        driver, _, _, _, outcome = await self.run_controller(actions, observations)
        self.assertEqual(outcome.actions, 12)
        self.assertEqual(outcome.pages, 6)
        self.assertEqual(driver.goto.await_count, 6)
        self.assertEqual(driver.scroll_down.await_count, 6)
        self.assertEqual(outcome.status, "budget_exhausted")

    async def test_no_initial_search_and_failed_synthesis_preserve_observed_evidence(self):
        empty = webiq.normalize_response({}, "nothing", provenance="live_webiq")
        row = bat.BrowsedSourceEvidence(
            url="https://forum.example.com/thread", platform="Synthetic forum", platform_category="Community",
            title="Synthetic owner thread", agent_explored=True, page_content="Synthetic browser context.",
            observed_excerpt="Synthetic browser context.",
        )
        with patch.object(bat, "_init_foundry_chat_model", side_effect=RuntimeError("synthetic model failure")):
            result = await bat.synthesize_due_diligence_with_gpt(
                baseline=baseline(), user_intent="Reliability", enrichment=empty, browsed_sources=[row], credential=object(),
            )
        self.assertEqual(result["synthesis_status"], "failed")
        self.assertEqual(result["run_mode"], "live_community_research")
        self.assertIn("Unavailable", result["claim_matrix"][0]["verdict"])
        self.assertEqual(result["claim_matrix"][0]["source_attribution"]["url"], row.url)
        self.assertNotIn("Listing Inspected Only", result["recommendation_badge"])

    async def test_selected_browser_lifecycle_is_reused_even_without_search_results(self):
        driver = AsyncMock()
        lifecycle = bat.SetupLifecycle(run_id="empty-community", driver=driver)
        callback = AsyncMock()
        with patch.object(bat, "create_bat_browser_session", new=AsyncMock(return_value=lifecycle)):
            await bat.browse_permitted_sources_live(
                run_id="empty-community", sources=[], credential=object(), explore_communities=callback,
            )
        callback.assert_awaited_once_with(driver, [])
        driver.close.assert_awaited_once()
        self.assertTrue(lifecycle.cleaned_up)

    async def test_client_and_vm_bridge_preserve_community_observations(self):
        for target in ("extension", "vm_worker"):
            async def emit(command):
                if command["type"] == "client_browser_command":
                    self.assertTrue(command["capture_community"])
                    bat.CLIENT_BROWSER_BROKER.resolve_observation(bat.ClientBrowserObservationPayload(
                        run_id=command["run_id"], command_id=command["command_id"],
                        url="https://forum.example.com/thread", community_capture_status="ready",
                        viewport_text="Synthetic current viewport",
                        discussion_blocks=["Synthetic owner assertion, not verified."],
                        observed_links=[{"url": "https://forum.example.com/replies", "text": "Replies"}],
                        scroll_y=700, at_page_end=True,
                    ))
            driver = bat.ClientExtensionBrowserDriver(run_id=f"bridge-{target}", emit_sse=emit, bridge_target=target)
            with patch.object(bat, "validate_public_https_url", side_effect=lambda url, **_: url):
                await driver.goto("https://forum.example.com/thread")
                observed = await driver.observe(stage="test", capture_title="Fixture", capture_community=True,
                                                capture_screenshot=False)
            self.assertEqual(observed.community_capture_status, "ready")
            self.assertEqual(observed.observed_links[0]["text"], "Replies")
            self.assertEqual(observed.scroll_y, 700)
            self.assertTrue(observed.at_page_end)
            await driver.close()

    async def test_new_browser_page_reaches_synthesis_and_unretrieved_citations_are_excluded(self):
        page_url = "https://forum.example.com/thread"
        row = bat.BrowsedSourceEvidence(
            url=page_url, requested_url=page_url, platform="Synthetic forum", platform_category="Community",
            title="Synthetic owner thread", agent_explored=True, discovery_origin="observed_page_link",
            page_content="NEW_COMMUNITY_FINDING: synthetic hinge failure after travel.",
            discussion_blocks_observed=1,
        )
        make_row = lambda url: bat.SynthesizedClaimRow(
            seller_claim="Durability", verdict="Unverified owner assertion",
            cross_platform_evidence="An owner reports hinge failure; not authenticated.",
            user_intent_impact="Seek more long-term reports.", source_label="Synthetic forum", source_url=url,
        )
        generated = bat.DueDiligenceSynthesisSchema(
            recommendation_badge="Further verification", fit_label="Partial",
            executive_summary="One synthetic public assertion.",
            claim_rows=[make_row(page_url), make_row("https://unretrieved.example/fake")],
        )
        model = MagicMock()
        invoke = AsyncMock(return_value=generated)
        model.with_structured_output.return_value.ainvoke = invoke
        with patch.object(bat, "_init_foundry_chat_model", return_value=model):
            result = await bat.synthesize_due_diligence_with_gpt(
                baseline=baseline(), user_intent="Reliability", enrichment=enrichment(),
                browsed_sources=[row], credential=object(),
                community_exploration={"status": "budget_exhausted", "gaps": ["Unverified travel durability."]},
            )
        self.assertIn("NEW_COMMUNITY_FINDING", invoke.await_args.args[0])
        self.assertIn("Additional community page", invoke.await_args.args[0])
        self.assertIn("Unverified travel durability.", invoke.await_args.args[0])
        self.assertEqual(result["claim_matrix"][0]["source_attribution"]["url"], page_url)
        self.assertEqual(len(result["claim_matrix"]), 1)
        self.assertTrue(any("Excluded 1" in gap for gap in result["evidence_gaps"]))

    async def test_pipeline_activates_exploration_after_empty_initial_discovery(self):
        empty = webiq.normalize_response({}, "nothing", provenance="live_webiq")
        captured = {}

        async def browse(**kwargs):
            self.assertEqual(kwargs["sources"], ())
            self.assertIn("explore_communities", kwargs)
            await kwargs["explore_communities"](object(), [])
            return []

        async def explore(**kwargs):
            captured.update(kwargs)
            kwargs["outcome"].status = "completed"
            kwargs["outcome"].gaps.append("Synthetic community evidence gap.")

        async def collect():
            return [json.loads(line[6:]) async for frame in pipeline.run_due_diligence_pipeline(
                product_url="https://example.com/product", user_intent="Reliability",
                execution_mode="live", webiq_key="test-key", community_exploration=True,
                exploration_domains=["forum.example.com"],
            ) for line in frame.splitlines() if line.startswith("data: ")]

        with patch.object(pipeline, "inspect_product_listing_live", new=AsyncMock(return_value=(baseline(), {}))), \
             patch.object(pipeline, "plan_review_queries_with_gpt", new=AsyncMock(return_value={"primary_web_query": "nothing"})), \
             patch.object(pipeline, "search_webiq_reviews", new=AsyncMock(return_value=empty)), \
             patch.object(pipeline, "browse_permitted_sources_live", side_effect=browse), \
             patch.object(pipeline, "explore_communities", side_effect=explore), \
             patch.object(bat, "get_foundry_credential", side_effect=bat.FoundryAuthenticationRequiredError("synthetic offline")):
            events = await collect()
        self.assertEqual(captured["domains"], ["forum.example.com"])
        summary = next(event for event in events if event["type"] == "claim_matrix")
        self.assertIn("Synthetic community evidence gap.", summary["evidence_gaps"])
        self.assertEqual(summary["community_exploration"]["status"], "completed")
        self.assertEqual(events[-1]["outcome"], "partial")


if __name__ == "__main__":
    unittest.main()
