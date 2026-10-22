"""Automated test suite for the Redesigned Product Due-Diligence Assistant (v2).

Verifies:
  1. Fail-closed URL safety & multi-platform taxonomy
  2. Web IQ `/v3/search/web` & `/v3/search/videos` normalization (<=200-char excerpt cap,
     `length='HH:MM:SS'`, `description`/`summary`/`moments`, and §2.5.1 metadata policy)
  3. Generated SVG Stage Illustrations (`capture_kind='generated_svg_illustration'`, `is_live_browser_capture=False`)
  4. Truthful `/api/health` capability reporting (`not_connected`, `simulation_only`, `scaffold_not_deployment_ready`)
  5. Curated sample walkthrough vs. unverified custom-URL honesty (no fabricated prices, SKUs, scores, or fake citations)
  6. Simulation-only cart guardrail (`status='simulated_add_to_cart'`, `simulation_only=True`, HTTP 403 checkout block)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

_HERE = Path(__file__).resolve().parent
for candidate in (
    _HERE,
    _HERE.parent,
    _HERE.parent / "app",
    _HERE.parents[1] / "api" / "app",
    _HERE.parents[1] / "agents" / "hosted",
):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from fastapi.testclient import TestClient  # noqa: E402

import auth  # noqa: E402
import bat_research  # noqa: E402
import browser_driver  # noqa: E402
import config  # noqa: E402
import main as portal_main  # noqa: E402
import pipeline  # noqa: E402
import screenshot_store  # noqa: E402
import url_safety  # noqa: E402
import webiq  # noqa: E402


def setUpModule():
    directory = unittest.enterModuleContext(TemporaryDirectory(prefix="pdd-unit-evidence-"))
    unittest.enterModuleContext(patch.dict(os.environ, {
        "PDD_EVIDENCE_DIRECTORY": directory, "PDD_EVIDENCE_BLOB_ACCOUNT_URL": "",
    }))


def tearDownModule():
    unittest.doModuleCleanups()


class TestUrlSafetyAndPlatformTaxonomy(unittest.TestCase):
    def test_valid_public_https_urls(self):
        url = "https://www.bestbuy.com/site/sony-wh-1000xm6/6580001.p#frag"
        cleaned = url_safety.validate_public_https_url(url, resolve_dns=False)
        self.assertEqual(cleaned, "https://www.bestbuy.com/site/sony-wh-1000xm6/6580001.p")

    def test_rejects_http_private_ips_localhost_and_ports(self):
        bad_urls = [
            "http://www.bestbuy.com/site/item",
            "https://localhost/admin",
            "https://127.0.0.1/secret",
            "https://169.254.169.254/latest/meta-data/",
            "https://10.0.0.1/internal",
            "https://192.168.1.50/router",
            "https://user:pass@www.bestbuy.com/item",
            "https://www.bestbuy.com:8443/item",
        ]
        for bad in bad_urls:
            with self.subTest(url=bad):
                with self.assertRaises(url_safety.UrlValidationError):
                    url_safety.validate_public_https_url(bad, resolve_dns=False)

    def test_multi_platform_classification_allows_public_visit_attempts(self):
        rtings = url_safety.classify_review_platform("https://www.rtings.com/headphones/reviews/sony/wh-1000xm6")
        self.assertEqual(rtings["platform"], "RTINGS")
        self.assertTrue(rtings["bat_allowed"])

        reddit = url_safety.classify_review_platform("https://www.reddit.com/r/headphones/comments/abc/xm6/")
        self.assertEqual(reddit["platform"], "Reddit")
        self.assertTrue(reddit["bat_allowed"])
        for url in ("https://www.amazon.com/product-reviews/example", "https://www.youtube.com/watch?v=example"):
            self.assertTrue(url_safety.classify_review_platform(url)["bat_allowed"])


class TestWebIQContractAndLegalLimits(unittest.TestCase):
    def test_web_response_caps_excerpts_at_200_chars_and_redacts_secrets(self):
        fake_key = "secret-webiq-sub-key-999"
        long_text = "A" * 350 + f" Key={fake_key}"
        payload = {
            "webResults": [
                {
                    "title": "<b>RTINGS</b> XM6 Review",
                    "url": "https://www.rtings.com/headphones/reviews/sony/wh-1000xm6",
                    "content": f"<p>{long_text}</p>",
                    "publishedAt": "2026-07-18T12:00:00Z",
                }
            ]
        }
        enrichment = webiq.normalize_response(payload, "Sony WH-1000XM6 review", fake_key)
        self.assertEqual(len(enrichment.sources), 1)
        src = enrichment.sources[0]
        citation = src.citation()
        self.assertLessEqual(len(citation["snippet"]), 200)
        self.assertNotIn(fake_key, citation["snippet"])
        self.assertGreater(len(src.content), 200)
        self.assertLessEqual(len(citation["retrieved_response"]["content"]), 200)
        self.assertTrue(citation["content_truncated"])
        self.assertNotIn(fake_key, json.dumps(citation))

    def test_search_requests_passages_and_preserves_bounded_grounding_separately(self):
        import httpx
        requests = []
        key = "private-test-request-key"
        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"webResults": [{
                "url": "https://example.com/review", "title": "Review",
                "content": "<p>" + key + " " + "measured grounding " * 250 + "</p>",
            }]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch.object(webiq.httpx, "AsyncClient", return_value=client):
            result = asyncio.run(webiq.search_webiq(key, "independent review"))
        self.assertEqual(requests[0]["contentFormat"], "passage")
        self.assertEqual(requests[0]["maxLength"], 3000)
        source = result.sources[0]
        self.assertGreater(len(source.content), 200)
        self.assertLessEqual(len(source.content), 3000)
        self.assertNotIn(key, source.content)
        self.assertNotIn(key, source.retrieved_content)
        self.assertEqual(source.citation()["retrieved_response"]["content"], source.retrieved_content[:200])



class TestTruthfulCapabilitiesAndCustomUrlHonesty(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(portal_main.app)

    def test_research_contract_reports_deployed_revision(self):
        with patch.dict(os.environ, {"CONTAINER_APP_REVISION": "test-revision"}):
            response = self.client.get("/api/research-contract")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["revision"], "test-revision")
        self.assertEqual(response.json()["research_stages"], 4)
        self.assertIsNone(response.json()["review_inspection_limit"])
        self.assertEqual(response.json()["run_timeout_seconds"], config.PDD_RUN_TIMEOUT_SECONDS)

    def test_health_reports_sample_mode_and_not_connected_capabilities(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "sample_demo")
        self.assertEqual(body["provisioning_script_status"], "scaffold_not_deployment_ready")
        caps = body["capabilities"]
        self.assertEqual(caps["browser_listing_digestion"]["state"], "not_connected")
        self.assertNotIn("video_understanding", caps)
        self.assertEqual(caps["model_synthesis"]["state"], "not_connected")
        self.assertEqual(caps["cart_action"]["state"], "simulation_only")

    def test_curated_sample_run_has_decision_findings_and_simulated_cart_gate(self):
        res = self.client.post(
            "/api/run",
            json={
                "product_url": "https://www.bestbuy.com/site/sony-wh-1000xm6-wireless-noise-canceling-over-the-ear-headphones-black/6580001.p",
                "user_intent": "I fly 10-hour international routes, wear thin prescription glasses, need Windows + iPhone multipoint, and want a durable hinge under $450.",
            },
        )
        self.assertEqual(res.status_code, 200)
        events = [
            json.loads(line[6:])
            for frame in res.text.split("\n\n")
            for line in frame.splitlines()
            if line.startswith("data: ")
        ]

        # Check SVG illustration provenance
        shot = next(e for e in events if e["type"] == "screenshot")["screenshot"]
        self.assertEqual(shot["capture_kind"], "generated_svg_illustration")
        self.assertFalse(shot["is_live_browser_capture"])

        # Check decision-first claim_matrix has no fake 91/100 score and includes what_fits & watch_outs
        cm = next(e for e in events if e["type"] == "claim_matrix")
        self.assertIsNone(cm["fit_score"])
        self.assertNotIn("Strong Buy", cm["recommendation_badge"])
        self.assertGreaterEqual(len(cm["what_fits"]), 3)
        self.assertGreaterEqual(len(cm["watch_outs"]), 2)
        self.assertTrue(all("source_attribution" in row for row in cm["matrix"]))

        # Check cart simulation & checkout block
        gate = next(e for e in events if e["type"] == "cart_gate")["cart_gate"]
        self.assertTrue(gate["simulation_only"])

        blocked = self.client.post(
            "/api/cart/approve",
            json={
                "approval_token": gate["approval_token"],
                "approved": True,
                "sku": gate["sku"],
                "variant": gate["variant"],
                "seller": gate["seller"],
                "max_price": gate["max_price_ceiling"],
                "quantity": 1,
                "requested_action": "proceed_to_checkout",
            },
        )
        self.assertEqual(blocked.status_code, 403)

        ok = self.client.post(
            "/api/cart/approve",
            json={
                "approval_token": gate["approval_token"],
                "approved": True,
                "sku": gate["sku"],
                "variant": gate["variant"],
                "seller": gate["seller"],
                "max_price": gate["max_price_ceiling"],
                "quantity": 1,
                "requested_action": "add_to_cart",
            },
        )
        self.assertEqual(ok.status_code, 200)
        receipt = ok.json()
        self.assertEqual(receipt["status"], "simulated_add_to_cart")
        self.assertTrue(receipt["simulation_only"])
        replay = self.client.post("/api/cart/approve", json=json.loads(ok.request.content))
        self.assertEqual(replay.status_code, 403)
        self.assertIn("already been used", replay.json()["detail"])

    def test_custom_url_does_not_fabricate_prices_skus_or_fake_reviews(self):
        res = self.client.post(
            "/api/run",
            json={
                "product_url": "https://www.amazon.com/dp/B09V3KXJPB",
                "user_intent": "Looking for quiet daily use under $250",
            },
        )
        self.assertEqual(res.status_code, 200)
        events = [
            json.loads(line[6:])
            for frame in res.text.split("\n\n")
            for line in frame.splitlines()
            if line.startswith("data: ")
        ]
        baseline = next(e for e in events if e["type"] == "product_baseline")["baseline"]
        self.assertTrue(baseline["is_custom_url"])
        self.assertIsNone(baseline["price"])
        self.assertEqual(baseline["sku"], "UNVERIFIED-CUSTOM-URL")

        enrichment = next(e for e in events if e["type"] == "enrichment")
        self.assertEqual(len(enrichment["sources"]), 0)
        self.assertEqual(enrichment["provenance"], "illustrative_custom_url")

        cm = next(e for e in events if e["type"] == "claim_matrix")
        self.assertIn("Unverified Custom URL", cm["recommendation_badge"])

    def test_product_keywords_on_other_hosts_remain_custom(self):
        for url in (
            "https://example.com/wh-1000xm6/6580001.p",
            "https://example.com/pg32ucdm",
            "https://example.com/?product=ode-brew-grinder-gen-2",
            "https://fellowproducts.com/products/not-the-ode",
        ):
            with self.subTest(url=url):
                self.assertIsNone(browser_driver._match_preset_key(url))
        for scenario in config.SCENARIOS.values():
            self.assertIsNotNone(browser_driver._match_preset_key(scenario["product_url"]))

    def test_all_samples_have_site_only_claims_and_explicit_fixture_provenance(self):
        async def run(scenario):
            return [
                json.loads(frame[6:].strip())
                async for frame in pipeline.run_due_diligence_pipeline(
                    product_url=scenario["product_url"],
                    user_intent="Compare Sony headphones too, but prioritize this exact product.",
                )
            ]
        for key, scenario in config.SCENARIOS.items():
            with self.subTest(key=key):
                events = asyncio.run(run(scenario))
                self.assertEqual(events[-1]["outcome"], "complete")
                self.assertFalse(any(e["type"] == "video_analysis" for e in events))
                cm = next(e for e in events if e["type"] == "claim_matrix")
                self.assertTrue(cm["matrix"])
                self.assertTrue(all("video_evidence" not in row for row in cm["matrix"]))
                self.assertIn("not verified facts", cm["executive_summary"])

    def test_live_search_never_attributes_sample_measurements_to_found_pages(self):
        live = webiq.normalize_response({
            "webResults": [{
                "url": "https://example.com/real-review",
                "title": "Independent report",
                "content": "An actual search excerpt that does not substantiate any battery measurement.",
            }]
        }, "review")
        async def run():
            return [
                json.loads(frame[6:].strip())
                async for frame in pipeline.run_due_diligence_pipeline(
                    product_url=config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    user_intent="Battery life",
                    webiq_key="mock-key-not-a-credential",
                )
            ]
        with patch.object(pipeline, "search_webiq_reviews", new=AsyncMock(return_value=live)) as search:
            events = asyncio.run(run())
        search.assert_awaited_once()
        self.assertEqual(events[-1]["outcome"], "complete")
        cm = next(e for e in events if e["type"] == "claim_matrix")
        self.assertEqual(len(cm["matrix"]), 1)
        self.assertEqual(cm["matrix"][0]["source_attribution"]["url"], "https://example.com/real-review")
        self.assertEqual(cm["matrix"][0]["Cross_platform_evidence"], live.sources[0].content)
        self.assertIn("no model synthesized a buying recommendation", cm["executive_summary"])
        self.assertFalse(next(e for e in events if e["type"] == "artifact")["persisted"])

    def test_live_export_is_memory_only_no_store_and_expires(self):
        with patch.object(Path, "write_bytes", side_effect=AssertionError("Live reports must not be written")):
            meta = pipeline.register_artifact("live.md", b"short live report", contains_live_webiq=True)
        self.assertIsNone(meta["path"])
        response = self.client.get(meta["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"short live report")
        self.assertEqual(response.headers["cache-control"], "no-store")
        with patch.object(pipeline.time, "monotonic", return_value=meta["expires_at"] + 1):
            self.assertEqual(self.client.get(meta["url"]).status_code, 404)

    def test_live_search_failure_is_terminal_and_does_not_fall_back_to_sample(self):
        async def run():
            return [
                json.loads(frame[6:].strip())
                async for frame in pipeline.run_due_diligence_pipeline(
                    product_url=config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    user_intent="Battery life",
                    webiq_key="mock-key-not-a-credential",
                )
            ]
        with patch.object(pipeline, "search_webiq_reviews", new=AsyncMock(side_effect=webiq.WebIQError("Service unavailable"))):
            events = asyncio.run(run())
        self.assertEqual(events[-1]["outcome"], "error")
        self.assertFalse(any(e["type"] in {"claim_matrix", "artifact", "cart_gate"} for e in events))



class TestLoopbackAndHttpsWebIQTransport(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(portal_main.app)

    def test_loopback_http_allows_request_scoped_webiq_key(self):
        live = webiq.normalize_response(
            {
                "webResults": [
                    {
                        "url": "https://www.rtings.com/headphones/reviews/sony/wh-1000xm6",
                        "title": "RTINGS Review",
                        "content": "Measured ANC and battery rundown.",
                    }
                ]
            },
            "Sony WH-1000XM6 review",
        )
        with patch.object(pipeline, "search_webiq_reviews", new=AsyncMock(return_value=live)):
            res = self.client.post(
                "http://127.0.0.1:8011/api/run",
                headers={
                    "Host": "127.0.0.1:8011",
                    "Origin": "http://127.0.0.1:8011",
                    "X-WebIQ-Key": "loopback-test-key-not-persisted",
                },
                json={
                    "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    "user_intent": "Battery life and glasses comfort",
                    "execution_mode": "sample",
                },
            )
        self.assertEqual(res.status_code, 200)

    def test_non_loopback_http_or_forwarded_http_rejects_webiq_key(self):
        # 1. Non-loopback Host header over plain HTTP
        res_remote_host = self.client.post(
            "http://example.com/api/run",
            headers={"Host": "example.com", "X-WebIQ-Key": "secret-key"},
            json={
                "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                "user_intent": "Battery life",
            },
        )
        self.assertEqual(res_remote_host.status_code, 400)
        self.assertIn("WebIQ requires an HTTPS connection", res_remote_host.json()["detail"])

        # 2. Non-loopback Origin header over plain HTTP
        res_bad_origin = self.client.post(
            "http://127.0.0.1:8011/api/run",
            headers={
                "Host": "127.0.0.1:8011",
                "Origin": "http://evil.example.com",
                "X-WebIQ-Key": "secret-key",
            },
            json={
                "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                "user_intent": "Battery life",
            },
        )
        self.assertEqual(res_bad_origin.status_code, 400)

        # 3. X-Forwarded-Proto: http from reverse proxy
        res_fwd_http = self.client.post(
            "http://127.0.0.1:8011/api/run",
            headers={
                "Host": "127.0.0.1:8011",
                "X-Forwarded-Proto": "http",
                "X-WebIQ-Key": "secret-key",
            },
            json={
                "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                "user_intent": "Battery life",
            },
        )
        self.assertEqual(res_fwd_http.status_code, 400)

        # 4. Non-loopback HTTP request spoofing X-Forwarded-Proto: https MUST be rejected
        res_spoofed_https = self.client.post(
            "http://example.com/api/run",
            headers={
                "Host": "example.com",
                "X-Forwarded-Proto": "https",
                "X-WebIQ-Key": "secret-key",
            },
            json={
                "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                "user_intent": "Battery life",
            },
        )
        self.assertEqual(res_spoofed_https.status_code, 400)
        self.assertIn("WebIQ requires an HTTPS connection", res_spoofed_https.json()["detail"])

        # 5. Plain HTTP request with spoofed X-Forwarded-Proto: https and X-Forwarded-For external IP MUST be rejected
        res_spoofed_proxy = self.client.post(
            "http://127.0.0.1:8011/api/run",
            headers={
                "Host": "127.0.0.1:8011",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "203.0.113.42",
                "X-WebIQ-Key": "secret-key",
            },
            json={
                "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                "user_intent": "Battery life",
            },
        )
        self.assertEqual(res_spoofed_proxy.status_code, 400)


class TestRuntimeDependenciesAndImports(unittest.TestCase):
    def test_declared_requirements_and_real_runtime_imports(self):
        import importlib
        import importlib.metadata as metadata

        req_text = (Path(__file__).resolve().parents[2] / "requirements.txt").read_text(encoding="utf-8")
        expected_packages = [
            "fastapi",
            "uvicorn",
            "httpx",
            "pydantic",
            "mcp",
            "starlette",
            "pytest",
            "azure-core",
            "azure-identity",
            "azure-ai-inference",
            "msal",
            "msal-extensions",
            "langchain",
            "langchain-core",
            "langchain-azure-ai",
            "langchain-mcp-adapters",
            "langgraph",
            "playwright",
        ]
        for pkg in expected_packages:
            self.assertIn(pkg, req_text)
            ver = metadata.version(pkg)
            self.assertTrue(len(ver) > 0)

        for mod_name in (
            "azure.core.credentials",
            "azure.identity",
            "msal",
            "msal_extensions",
            "langchain.chat_models",
            "langchain_azure_ai.tools",
            "langchain_mcp_adapters.client",
            "langgraph.graph",
            "playwright.async_api",
        ):
            mod = importlib.import_module(mod_name)
            self.assertIsNotNone(mod)


class TestCloudPasscodeAuthAndManagedIdentity(unittest.TestCase):
    def setUp(self):
        auth.reset_login_rate_limits()

    def test_hosted_managed_identity_credential_selection_and_token_acquisition(self):
        from azure.core.credentials import AccessToken

        with patch.dict(
            os.environ,
            {
                "PDD_HOSTED_MODE": "true",
                "FOUNDRY_MANAGED_IDENTITY_CLIENT_ID": "11111111-2222-3333-4444-555555555555",
            },
            clear=False,
        ):
            cred = bat_research.get_foundry_credential()
            self.assertIsInstance(cred, bat_research.HostedManagedIdentityCredential)
            self.assertEqual(cred.client_id, "11111111-2222-3333-4444-555555555555")

            fake_token = AccessToken("mi-access-token", int(time.time()) + 3600)
            with patch("azure.identity.ManagedIdentityCredential") as mock_mi_cls:
                mock_mi_instance = mock_mi_cls.return_value
                mock_mi_instance.get_token.return_value = fake_token
                tok = cred.get_token("https://ai.azure.com/.default")
                self.assertEqual(tok.token, "mi-access-token")
                mock_mi_cls.assert_called_once_with(client_id="11111111-2222-3333-4444-555555555555")

    def test_fail_closed_startup_when_hosted_or_passcode_required_without_secrets(self):
        with patch.dict(
            os.environ,
            {
                "PDD_HOSTED_MODE": "true",
                "PDD_DEMO_PASSCODE": "",
                "PDD_PASSCODE_SHA256": "",
                "PDD_SESSION_SIGNING_SECRET": "",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                auth.validate_startup_auth_configuration()
            self.assertIn("Passcode protection is enabled", str(ctx.exception))

    def test_passcode_protection_blocks_anonymous_routes_and_docs_and_allows_authenticated_flow_with_csrf(self):
        env_overrides = {
            "PDD_REQUIRE_PASSCODE": "true",
            "PDD_DEMO_PASSCODE": "TestPasscode-Strong-2026!XyZ",
            "PDD_SESSION_SIGNING_SECRET": "a" * 64,
            "PDD_TRUSTED_INGRESS_PROXY": "aca",
            "PDD_ALLOWED_HOSTS": "ca-pdd-portal.eastus.azurecontainerapps.io",
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            client = TestClient(portal_main.app)
            aca_headers = {
                "Host": "ca-pdd-portal.eastus.azurecontainerapps.io",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "198.51.100.25",
            }

            # 1. Unauthenticated /api/health returns minimal non-sensitive status
            health_anon = client.get("/api/health", headers=aca_headers)
            self.assertEqual(health_anon.status_code, 200)
            self.assertEqual(health_anon.json()["status"], "ok")
            self.assertTrue(health_anon.json()["auth_required"])
            self.assertFalse(health_anon.json()["authenticated"])
            self.assertNotIn("capabilities", health_anon.json())

            # 2. Unauthenticated root serves passcode login gate HTML, not portal HTML
            root_anon = client.get("/", headers=aca_headers)
            self.assertEqual(root_anon.status_code, 200)
            self.assertIn('id="passcode-input"', root_anon.text)
            self.assertIn('autocomplete="current-password"', root_anon.text)
            self.assertIn("Portal passcode", root_anon.text)
            self.assertNotIn("Look beyond the listing", root_anon.text)

            # 3. Unauthenticated research APIs, reports, screenshots, and OpenAPI docs are all blocked (401)
            for path in (
                "/api/scenarios",
                "/api/skills",
                "/api/reports/demo.md",
                "/api/screenshots/shot_001",
                "/api/replay/cu-proof",
                "/api/replay/cu-binary-proof",
                "/api/replay/evidence/aaaaaaaaaaaa",
                "/api/replay/assets/" + "a" * 64 + ".png",
                "/docs",
                "/redoc",
                "/openapi.json",
            ):
                res = client.get(path, headers=aca_headers)
                self.assertEqual(res.status_code, 401, f"Expected 401 on {path}, got {res.status_code}")

            for post_path in ("/api/plan", "/api/run", "/api/cart/approve", "/api/replay/transient"):
                res = client.post(post_path, headers=aca_headers, json={})
                self.assertEqual(res.status_code, 401, f"Expected 401 on POST {post_path}")

            # 4. Wrong passcode returns 401
            bad_login = client.post(
                "/api/auth/login",
                headers={**aca_headers, "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io"},
                json={"passcode": "wrong-passcode"},
            )
            self.assertEqual(bad_login.status_code, 401)

            # 5. Valid passcode issues HttpOnly + Secure session cookie + csrf_token
            good_login = client.post(
                "/api/auth/login",
                headers={**aca_headers, "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io"},
                json={"passcode": "TestPasscode-Strong-2026!XyZ"},
            )
            self.assertEqual(good_login.status_code, 200)
            csrf_token = good_login.json()["csrf_token"]
            self.assertTrue(len(csrf_token) > 10)
            set_cookie_hdr = good_login.headers.get("set-cookie", "")
            self.assertIn("pdd_session=", set_cookie_hdr)
            self.assertIn("HttpOnly", set_cookie_hdr)
            self.assertIn("Secure", set_cookie_hdr)
            session_cookie_val = good_login.cookies.get("pdd_session")
            self.assertTrue(session_cookie_val)
            auth_aca_headers = {**aca_headers, "Cookie": f"pdd_session={session_cookie_val}"}

            # 6. Authenticated root now serves full portal HTML
            root_auth = client.get("/", headers=auth_aca_headers)
            self.assertEqual(root_auth.status_code, 200)
            self.assertIn("Look beyond the listing", root_auth.text)

            # 7. State-changing POST without X-CSRF-Token is rejected (403)
            plan_no_csrf = client.post(
                "/api/plan",
                headers={**auth_aca_headers, "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io"},
                json={
                    "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    "user_intent": "Battery life",
                    "execution_mode": "sample",
                },
            )
            self.assertEqual(plan_no_csrf.status_code, 403)
            replay_payload = {"run_id": "a" * 12, "expires_at": time.time() - 1, "evidence": {}}
            self.assertEqual(client.post("/api/replay/transient", headers=auth_aca_headers, json=replay_payload).status_code, 403)
            self.assertEqual(client.post("/api/replay/transient", headers={
                **auth_aca_headers, "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io",
                "X-CSRF-Token": csrf_token,
            }, json=replay_payload).status_code, 410)

            # 8. State-changing POST with valid X-CSRF-Token succeeds
            plan_with_csrf = client.post(
                "/api/plan",
                headers={
                    **auth_aca_headers,
                    "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io",
                    "X-CSRF-Token": csrf_token,
                },
                json={
                    "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    "user_intent": "Battery life",
                    "execution_mode": "sample",
                },
            )
            self.assertEqual(plan_with_csrf.status_code, 200)

            # 9. Logout clears session cookie
            logout_res = client.post(
                "/api/auth/logout",
                headers={
                    **auth_aca_headers,
                    "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io",
                    "X-CSRF-Token": csrf_token,
                },
            )
            self.assertEqual(logout_res.status_code, 200)
            after_logout = client.get("/api/scenarios", headers=aca_headers)
            self.assertEqual(after_logout.status_code, 401)

    def test_passcode_login_rate_limiting_uses_rightmost_aca_forwarded_for(self):
        auth.reset_login_rate_limits()
        env_overrides = {
            "PDD_REQUIRE_PASSCODE": "true",
            "PDD_DEMO_PASSCODE": "TestPasscode-Strong-2026!XyZ",
            "PDD_SESSION_SIGNING_SECRET": "b" * 64,
            "PDD_TRUSTED_INGRESS_PROXY": "aca",
            "PDD_ALLOWED_HOSTS": "ca-pdd-portal.eastus.azurecontainerapps.io",
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            client = TestClient(portal_main.app)
            # Attacker tries to spoof X-Forwarded-For prefix on each attempt, but ACA appends real IP rightmost
            for i in range(auth.LOGIN_MAX_ATTEMPTS):
                res = client.post(
                    "/api/auth/login",
                    headers={
                        "Host": "ca-pdd-portal.eastus.azurecontainerapps.io",
                        "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io",
                        "X-Forwarded-Proto": "https",
                        "X-Forwarded-For": f"10.0.0.{i}, 203.0.113.99",
                    },
                    json={"passcode": "wrong"},
                )
                self.assertEqual(res.status_code, 401)

            # Next attempt from same rightmost IP (203.0.113.99) is rate-limited with 429
            blocked = client.post(
                "/api/auth/login",
                headers={
                    "Host": "ca-pdd-portal.eastus.azurecontainerapps.io",
                    "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": "10.99.99.99, 203.0.113.99",
                },
                json={"passcode": "TestPasscode-Strong-2026!XyZ"},
            )
            self.assertEqual(blocked.status_code, 429)

    def test_trusted_aca_https_ingress_allows_webiq_key_only_on_configured_allowed_host(self):
        env_overrides = {
            "PDD_REQUIRE_PASSCODE": "true",
            "PDD_DEMO_PASSCODE": "TestPasscode-Strong-2026!XyZ",
            "PDD_SESSION_SIGNING_SECRET": "c" * 64,
            "PDD_TRUSTED_INGRESS_PROXY": "aca",
            "PDD_ALLOWED_HOSTS": "ca-pdd-portal.eastus.azurecontainerapps.io",
        }
        with patch.dict(os.environ, env_overrides, clear=False):
            client = TestClient(portal_main.app)
            aca_headers = {
                "Host": "ca-pdd-portal.eastus.azurecontainerapps.io",
                "Origin": "https://ca-pdd-portal.eastus.azurecontainerapps.io",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "198.51.100.77",
            }
            login = client.post("/api/auth/login", headers=aca_headers, json={"passcode": "TestPasscode-Strong-2026!XyZ"})
            csrf_token = login.json()["csrf_token"]
            session_cookie_val = login.cookies.get("pdd_session")
            auth_aca_headers = {**aca_headers, "Cookie": f"pdd_session={session_cookie_val}"}

            # Trusted ACA HTTPS request with X-WebIQ-Key succeeds on /api/plan
            plan_ok = client.post(
                "/api/plan",
                headers={**auth_aca_headers, "X-CSRF-Token": csrf_token, "X-WebIQ-Key": "user-webiq-key"},
                json={
                    "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    "user_intent": "Battery life",
                    "execution_mode": "sample",
                },
            )
            self.assertEqual(plan_ok.status_code, 200)

            # Untrusted Host header is rejected by TrustedHost / ACA check (400)
            untrusted_host = client.post(
                "/api/plan",
                headers={
                    "Host": "evil.example.com",
                    "Origin": "https://evil.example.com",
                    "X-Forwarded-Proto": "https",
                    "Cookie": f"pdd_session={session_cookie_val}",
                    "X-CSRF-Token": csrf_token,
                    "X-WebIQ-Key": "user-webiq-key",
                },
                json={
                    "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    "user_intent": "Battery life",
                    "execution_mode": "sample",
                },
            )
            self.assertEqual(untrusted_host.status_code, 400)

    def test_run_concurrency_limiter_and_secret_redaction(self):
        with patch.dict(os.environ, {"PDD_DEMO_PASSCODE": "SecretPasscode999"}, clear=False):
            redacted = bat_research.redact_secrets(
                "Header X-WebIQ-Key: mykey123 Cookie pdd_session=abc.def and SecretPasscode999",
                "mykey123",
            )
            self.assertNotIn("SecretPasscode999", redacted)
            self.assertNotIn("mykey123", redacted)
            self.assertNotIn("abc.def", redacted)

        # Verify RunConcurrencySlot rejects when live concurrency limit is saturated
        slot1 = auth.RunConcurrencySlot(is_live_mode=True)
        slot2 = auth.RunConcurrencySlot(is_live_mode=True)
        with slot1:
            with self.assertRaises(Exception) as ctx:
                with slot2:
                    pass
            self.assertEqual(getattr(ctx.exception, "status_code", None), 429)


class TestNonInteractiveTenantCredential(unittest.TestCase):
    def setUp(self):
        bat_research.reset_auth_circuit_breaker()

    def tearDown(self):
        bat_research.reset_auth_circuit_breaker()

    def test_silent_cache_miss_raises_auth_required_and_trips_circuit_breaker_without_popup(self):
        cred = bat_research.NonInteractiveTenantCredential(tenant_id=config.FOUNDRY_TENANT_ID)
        with patch.object(
            bat_research.NonInteractiveTenantCredential,
            "_acquire_from_msal_cache_silent",
            side_effect=bat_research.FoundryAuthenticationRequiredError(
                f"No cached token for tenant {config.FOUNDRY_TENANT_ID}",
                tenant_id=config.FOUNDRY_TENANT_ID,
            ),
        ) as mock_silent:
            with self.assertRaises(bat_research.FoundryAuthenticationRequiredError) as ctx1:
                cred.get_token("https://ai.azure.com/.default")
            self.assertEqual(ctx1.exception.status_code, 401)
            self.assertEqual(ctx1.exception.code, "foundry_auth_required")
            self.assertIn(config.FOUNDRY_TENANT_ID, str(ctx1.exception))
            self.assertEqual(mock_silent.call_count, 1)

            # Second call hits circuit breaker immediately without re-probing MSAL cache
            with self.assertRaises(bat_research.FoundryAuthenticationRequiredError):
                cred.get_token("https://ai.azure.com/.default")
            self.assertEqual(mock_silent.call_count, 1)

    def test_ignores_raw_bearer_token_env_and_rejects_mismatched_tenant(self):
        import os

        cred = bat_research.NonInteractiveTenantCredential(tenant_id=config.FOUNDRY_TENANT_ID)
        with patch.dict(
            os.environ,
            {
                "FOUNDRY_BEARER_TOKEN": "unverified-env-token",
                "AZURE_AI_BEARER_TOKEN": "unverified-env-token-2",
            },
            clear=False,
        ), patch.object(
            bat_research.NonInteractiveTenantCredential,
            "_acquire_from_msal_cache_silent",
            side_effect=bat_research.FoundryAuthenticationRequiredError(
                "No valid MSAL silent token",
                tenant_id=config.FOUNDRY_TENANT_ID,
            ),
        ) as mock_silent:
            with self.assertRaises(bat_research.FoundryAuthenticationRequiredError):
                cred.get_token("https://ai.azure.com/.default")
            self.assertEqual(mock_silent.call_count, 1)

        with self.assertRaises(bat_research.FoundryAuthenticationRequiredError):
            cred.get_token("https://ai.azure.com/.default", tenant_id="00000000-0000-0000-0000-000000000000")

    def test_select_single_account_enforces_configured_oid_and_never_falls_back_to_other_cached_user(self):
        tenant = config.FOUNDRY_TENANT_ID
        authorized_acct = {
            "home_account_id": f"11111111-1111-4111-8111-111111111111.{tenant}",
            "local_account_id": "11111111-1111-4111-8111-111111111111",
            "realm": tenant,
            "username": "authorized@example.com",
        }
        other_same_tenant_acct = {
            "home_account_id": f"22222222-2222-4222-8222-222222222222.{tenant}",
            "local_account_id": "22222222-2222-4222-8222-222222222222",
            "realm": tenant,
            "username": "other@example.com",
        }

        cred = bat_research.NonInteractiveTenantCredential(
            tenant_id=tenant,
            account_oid="11111111-1111-4111-8111-111111111111",
        )
        selected = cred._select_single_account([other_same_tenant_acct, authorized_acct])
        self.assertEqual(selected["local_account_id"], "11111111-1111-4111-8111-111111111111")

        # If only the other user is cached, fail closed instead of silently switching identities
        with self.assertRaises(bat_research.FoundryAuthenticationRequiredError) as ctx_missing:
            cred._select_single_account([other_same_tenant_acct])
        self.assertIn("Refusing to silently switch to a different cached user", str(ctx_missing.exception))

        # If no account filter is configured and multiple accounts exist in the tenant, fail closed
        unfiltered_cred = bat_research.NonInteractiveTenantCredential(
            tenant_id=tenant,
            account_oid="",
            account_upn="",
        )
        with self.assertRaises(bat_research.FoundryAuthenticationRequiredError) as ctx_ambiguous:
            unfiltered_cred._select_single_account([other_same_tenant_acct, authorized_acct])
        self.assertIn("Multiple cached accounts", str(ctx_ambiguous.exception))


class TestPhase1LiveWebResearchFlow(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(portal_main.app)
        bat_research.reset_auth_circuit_breaker()

    def _build_mock_live_baseline(self, run_id: str) -> browser_driver.ProductBaseline:
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        rec = screenshot_store.SCREENSHOT_STORE.record_capture(
            run_id=run_id,
            sequence=1,
            stage="live_bat_listing_inspection",
            title="Live BAT Listing Capture: Sony WH-1000XM6",
            url="https://www.bestbuy.com/site/sony-wh-1000xm6/6580001.p",
            caption="Real Playwright CDP screenshot captured via Foundry BAT workspace ws-browser-automation.",
            data=png_bytes,
            media_type="image/png",
            capture_kind="live_bat_screenshot",
            is_live_browser_capture=True,
            provenance_label="Live BAT Browser Capture (ws-browser-automation)",
        )
        return browser_driver.ProductBaseline(
            product_url="https://www.bestbuy.com/site/sony-wh-1000xm6/6580001.p",
            canonical_url="https://www.bestbuy.com/site/sony-wh-1000xm6/6580001.p",
            brand="Sony",
            model="WH-1000XM6",
            sku="6580001",
            variant="Black",
            price=399.99,
            currency="USD",
            seller="Best Buy",
            condition="New",
            return_policy="15-day return window",
            seller_claims=[
                "Up to 30-hour battery life with ANC enabled",
                "QN3 HD Noise Canceling Processor",
            ],
            puffery_filtered=["Industry-leading silence"],
            screenshots=[rec.metadata()],
            is_custom_url=False,
            data_provenance="live_bat_observed",
            limitation_notice="Observed directly via Foundry BAT (ws-browser-automation) + gpt-5.6-terra.",
        )

    def test_phase1_live_full_flow_with_bat_webiq_permitted_browse_and_gpt_synthesis(self):
        async def fake_inspect(*, run_id: str, product_url: str, user_intent: str, **kwargs):
            return self._build_mock_live_baseline(run_id), {"steps": 2}

        fake_query_plan = {
            "primary_web_query": "Sony WH-1000XM6 independent review glasses multipoint battery",
            "secondary_web_query": "Sony WH-1000XM6 owner issues hinge",
            "video_metadata_query": "Sony WH-1000XM6 long term review test",
            "target_source_categories": ["Lab Measurement", "Community Forum"],
            "rationale": "Focus on measured battery rundown with multipoint and eyewear seal.",
            "planner": config.FOUNDRY_MODEL_DEPLOYMENT,
        }
        live_enrichment = webiq.normalize_response(
            {
                "webResults": [
                    {
                        "url": "https://www.rtings.com/headphones/reviews/sony/wh-1000xm6",
                        "title": "Sony WH-1000XM6 Headphones Review - RTINGS.com",
                        "content": "Measured 24.1h continuous battery with LDAC + Multipoint enabled; 31.2 dB cabin bass attenuation.",
                    },
                    {
                        "url": "https://www.reddit.com/r/headphones/comments/xm6/owner_impressions/",
                        "title": "WH-1000XM6 3-month glasses & hinge impressions",
                        "content": "Thin metal frames seal cleanly; internal metal hinge sleeve feels solid.",
                    },
                ]
            },
            fake_query_plan["primary_web_query"],
        )
        fake_browsed = [
            bat_research.BrowsedSourceEvidence(
                url="https://www.rtings.com/headphones/reviews/sony/wh-1000xm6",
                platform="RTINGS",
                platform_category="Lab Measurement & Benchmark",
                title="Sony WH-1000XM6 Headphones Review - RTINGS.com",
                access_status="inspected",
                extracted_summary="Confirmed 24.1h multipoint battery test and 1.1 dB thin-frame leakage.",
                screenshot_artifact_id=None,
            ),
            bat_research.BrowsedSourceEvidence(
                url="https://www.reddit.com/r/headphones/comments/xm6/owner_impressions/",
                platform="Reddit",
                platform_category="Community Discussion Thread",
                title="WH-1000XM6 3-month glasses & hinge impressions",
                access_status="skipped_restricted_domain",
                extracted_summary="Skipped live BAT navigation for Reddit (bat_allowed=False); using <=200-char Web IQ snippet only.",
                screenshot_artifact_id=None,
            ),
        ]

        with patch.object(pipeline, "inspect_product_listing_live", side_effect=fake_inspect) as mock_inspect, \
             patch.object(pipeline, "plan_review_queries_with_gpt", new=AsyncMock(return_value=fake_query_plan)) as mock_plan, \
             patch.object(pipeline, "search_webiq_reviews", new=AsyncMock(return_value=live_enrichment)) as mock_search, \
             patch.object(pipeline, "browse_permitted_sources_live", new=AsyncMock(return_value=fake_browsed)) as mock_browse, \
             patch.object(bat_research, "get_foundry_credential", side_effect=bat_research.FoundryAuthenticationRequiredError("offline test")):
            res = self.client.post(
                "http://127.0.0.1:8011/api/run",
                headers={
                    "Host": "127.0.0.1:8011",
                    "Origin": "http://127.0.0.1:8011",
                    "X-WebIQ-Key": "live-portal-key",
                },
                json={
                    "product_url": "https://www.bestbuy.com/site/sony-wh-1000xm6/6580001.p",
                    "user_intent": "10-hour flights, thin glasses, Windows + iPhone multipoint under $450",
                    "execution_mode": "live",
                },
            )

        self.assertEqual(res.status_code, 200)
        mock_inspect.assert_awaited_once()
        mock_plan.assert_awaited_once()
        mock_search.assert_awaited_once()
        mock_browse.assert_awaited_once()

        events = [
            json.loads(line[6:])
            for frame in res.text.split("\n\n")
            for line in frame.splitlines()
            if line.startswith("data: ")
        ]
        shot = next(e for e in events if e["type"] == "screenshot")["screenshot"]
        self.assertEqual(shot["capture_kind"], "live_bat_screenshot")
        self.assertTrue(shot["is_live_browser_capture"])

        browsed_evt = next(e for e in events if e["type"] == "browsed_sources")
        self.assertEqual(len(browsed_evt["sources"]), 2)
        self.assertEqual(browsed_evt["sources"][0]["access_status"], "inspected")
        self.assertEqual(browsed_evt["sources"][1]["access_status"], "skipped_restricted_domain")

        cm = next(e for e in events if e["type"] == "claim_matrix")
        self.assertFalse(any("Content Understanding" in gap for gap in cm["evidence_gaps"]))
        self.assertEqual(cm["synthesis_status"], "failed")
        self.assertFalse(next(e for e in events if e["type"] == "artifact")["persisted"])
        self.assertEqual(events[-1]["outcome"], "partial")

    def test_search_only_reviews_inspects_listing_and_never_opens_discovered_pages(self):
        for inspector, provider in (("webiq_browse", "live_webiq"), ("live", "live_toolbox_web_search")):
            with self.subTest(inspector=inspector):
                baseline = bat_research.build_unverified_seed_baseline("https://example.com/product", "Long-term reliability")
                baseline.data_provenance = "live_webiq_browse_observed" if inspector == "webiq_browse" else "live_bat_observed"
                baseline.provider = "webiq" if inspector == "webiq_browse" else "browser"
                enrichment = webiq.normalize_response({"webResults": [{
                    "url": "https://example.com/review", "title": "Owner review",
                    "content": "Grounding material beyond the display cap. " * 30,
                }]}, "owner review", provenance=provider)
                with patch.object(pipeline, "inspect_product_listing_webiq_browse", new=AsyncMock(return_value=(baseline, []))) as web_listing, \
                     patch.object(pipeline, "inspect_product_listing_live", new=AsyncMock(return_value=(baseline, []))) as bat_listing, \
                     patch.object(pipeline, "plan_review_queries_with_gpt", new=AsyncMock(return_value={"primary_web_query": "owner review"})), \
                     patch.object(pipeline, "search_webiq_reviews", new=AsyncMock(return_value=enrichment)) as search, \
                     patch.object(pipeline, "search_toolbox_web_search", new=AsyncMock(return_value=enrichment)) as toolbox, \
                     patch.object(pipeline, "browse_permitted_sources_live", new=AsyncMock()) as review_inspector, \
                     patch.object(bat_research, "get_foundry_credential", side_effect=bat_research.FoundryAuthenticationRequiredError("offline test")):
                    response = self.client.post("http://127.0.0.1:8011/api/run",
                        headers={"Host": "127.0.0.1:8011", "Origin": "http://127.0.0.1:8011",
                                 **({"X-WebIQ-Key": "request-scope-test-key"} if provider == "live_webiq" else {})},
                        json={"product_url": "https://example.com/product", "user_intent": "Long-term reliability",
                              "execution_mode": inspector, "inspect_review_pages": False})
                events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                self.assertEqual(response.status_code, 200)
                review_inspector.assert_not_awaited()
                (web_listing if inspector == "webiq_browse" else bat_listing).assert_awaited_once()
                (bat_listing if inspector == "webiq_browse" else web_listing).assert_not_awaited()
                (search if provider == "live_webiq" else toolbox).assert_awaited_once()
                plan = next(event["plan"] for event in events if event["type"] == "plan")
                self.assertFalse(plan["inspect_review_pages"])
                self.assertEqual(plan["steps"][2]["tools"], [])
                self.assertTrue(any(event["type"] == "inspection_skipped" for event in events))
                self.assertFalse(any(event["type"] in ("source_inspection", "browsed_sources") for event in events))
                metrics = next(event["evidence_metrics"] for event in events if event["type"] == "claim_matrix")
                self.assertEqual(metrics["inspected_sources"], 0)
                self.assertEqual(metrics["inspected_context_characters"], 0)
                self.assertEqual(metrics["search_content_characters"], len(enrichment.sources[0].content))
                self.assertEqual(events[-1]["comparison_method"], "search_only")

    def test_phase1_live_mode_without_webiq_key_returns_listing_only_partial_never_samples(self):
        async def fake_inspect(*, run_id: str, product_url: str, user_intent: str, **kwargs):
            return self._build_mock_live_baseline(run_id), {"steps": 1}

        with patch.object(pipeline, "inspect_product_listing_live", side_effect=fake_inspect), \
             patch.object(bat_research, "get_foundry_credential", side_effect=bat_research.FoundryAuthenticationRequiredError("offline test")):
            res = self.client.post(
                "/api/run",
                json={
                    "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    "user_intent": "Battery life and glasses comfort",
                    "execution_mode": "live",
                },
            )
        self.assertEqual(res.status_code, 200)
        events = [
            json.loads(line[6:])
            for frame in res.text.split("\n\n")
            for line in frame.splitlines()
            if line.startswith("data: ")
        ]
        enrichment = next(e for e in events if e["type"] == "enrichment")
        self.assertEqual(enrichment["status"], "unavailable_missing_key")
        self.assertEqual(enrichment["provenance"], "unavailable_missing_key")
        self.assertEqual(len(enrichment["sources"]), 0)

        cm = next(e for e in events if e["type"] == "claim_matrix")
        self.assertEqual(cm["run_mode"], "live_bat_listing_only")
        self.assertTrue(any("Web IQ API key" in gap for gap in cm["evidence_gaps"]))
        self.assertEqual(events[-1]["outcome"], "partial")

    def test_phase1_live_mode_foundry_403_error_does_not_fall_back_to_samples(self):
        with patch.object(
            pipeline,
            "inspect_product_listing_live",
            side_effect=bat_research.FoundryAccessDeniedError(
                "Foundry access denied (HTTP 403): missing Microsoft.CognitiveServices/accounts/AIServices/agents/read"
            ),
        ):
            res = self.client.post(
                "/api/run",
                json={
                    "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    "user_intent": "Battery life",
                    "execution_mode": "live",
                },
            )
        self.assertEqual(res.status_code, 200)
        events = [
            json.loads(line[6:])
            for frame in res.text.split("\n\n")
            for line in frame.splitlines()
            if line.startswith("data: ")
        ]
        err_evt = next(e for e in events if e["type"] == "error")
        self.assertEqual(err_evt["code"], "foundry_caller_rbac_403")
        self.assertEqual(err_evt["status_code"], 403)
        self.assertFalse(any(e["type"] in {"claim_matrix", "cart_gate"} for e in events))
        self.assertEqual(events[-1]["outcome"], "error")

    def test_browse_permitted_sources_guarantees_setup_cleanup(self):
        sources = webiq.normalize_response(
            {
                "webResults": [
                    {
                        "url": "https://www.rtings.com/headphones/reviews/sony/wh-1000xm6",
                        "title": "RTINGS XM6",
                        "content": "Excerpt",
                    }
                ]
            },
            "query",
        ).sources

        mock_driver = AsyncMock()
        mock_driver.goto = AsyncMock(return_value=200)
        mock_driver.observe = AsyncMock(
            return_value=bat_research.PageObservation(
                url="https://www.rtings.com/headphones/reviews/sony/wh-1000xm6",
                title="RTINGS XM6",
                status_code=200,
                headings=["Sound Quality", "Battery Life"],
                visible_text_excerpt="Detailed lab measurements for Sony WH-1000XM6.",
                price_candidates=["399.99"],
                sku_candidates=["6580001"],
                claim_candidates=["Measured 24.1h continuous battery with multipoint active."],
                restricted=False,
                screenshot_artifact_id="shot_rtings_1",
            )
        )
        cleanup_called = False

        async def fake_cleanup():
            nonlocal cleanup_called
            cleanup_called = True

        fake_lifecycle = bat_research.SetupLifecycle(run_id="testrun123")
        fake_lifecycle.driver = mock_driver
        fake_lifecycle.cleanup = fake_cleanup

        with patch.object(
            bat_research,
            "create_bat_browser_session",
            new=AsyncMock(return_value=fake_lifecycle),
        ):
            screenshot_store.SCREENSHOT_STORE.start_run("testrun123")
            try:
                results = asyncio.run(
                    bat_research.browse_permitted_sources_live(
                        run_id="testrun123",
                        sources=sources,
                        max_sources=1,
                    )
                )
            finally:
                screenshot_store.SCREENSHOT_STORE.finish_run("testrun123")

        self.assertTrue(cleanup_called)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].access_status, "inspected")
        self.assertEqual(results[0].screenshot_artifact_id, "shot_rtings_1")

    def test_extract_ws_endpoint_decodes_unicode_escaped_mcp_cdp_url(self):
        mcp_payload = [
            {
                "type": "text",
                "text": (
                    '{"cdp_url":"wss://browser.playwright.microsoft.com/ws?os=Linux\\u0026browser=chromium'
                    '\\u0026shouldRedirect=true\\u0026token=eyJhbGciOiJSUzI1NiJ9.test.sig\\u0026startTime=123",'
                    '"live_view_url":"https://example.com/live"}'
                ),
                "id": "lc_tool_1",
            }
        ]
        extracted = bat_research._extract_ws_endpoint(mcp_payload)
        self.assertIn("token=eyJhbGciOiJSUzI1NiJ9.test.sig", extracted)
    def test_site_http2_protocol_error_treated_as_site_restriction_not_foundry_unavailable(self):
        bestbuy_url = "https://www.bestbuy.com/site/sony-wh-1000xm6-wireless-noise-canceling-over-the-ear-headphones-black/6580001.p"
        seed = bat_research.build_unverified_seed_baseline(bestbuy_url, "10-hour flights")
        self.assertIn("Sony Wh 1000Xm6", seed.model)
        self.assertNotEqual(seed.model, "6580001.P")

        driver = bat_research.LiveCDPBrowserDriver(run_id="run_http2_test", ws_endpoint="wss://example.com/ws")
        fake_page = AsyncMock()
        fake_page.url = "about:blank"
        fake_page.goto = AsyncMock(
            side_effect=RuntimeError(
                f"Error: Page.goto: net::ERR_HTTP2_PROTOCOL_ERROR at {bestbuy_url}"
            )
        )
        fake_page.set_content = AsyncMock(return_value=None)
        fake_page.evaluate = AsyncMock(
            return_value={
                "url": "about:blank",
                "title": "",
                "headings": ["Retailer Blocked Automated Datacenter Browser Navigation"],
                "bodyExcerpt": "Observed Transport Status: net::ERR_HTTP2_PROTOCOL_ERROR",
                "candidateLines": [],
            }
        )
        fake_page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        driver._page = fake_page

        screenshot_store.SCREENSHOT_STORE.start_run("run_http2_test")
        try:
            status = asyncio.run(driver.goto(bestbuy_url))
            self.assertIsNone(status)
            obs = asyncio.run(
                driver.observe(
                    stage="live_bat_listing_inspection",
                    capture_title="Initial Listing Viewport",
                )
            )
        finally:
            screenshot_store.SCREENSHOT_STORE.finish_run("run_http2_test")

        self.assertTrue(obs.restricted)
        self.assertIn("net::ERR_HTTP2_PROTOCOL_ERROR", obs.restriction_reason)
        self.assertEqual(obs.url, bestbuy_url)
        self.assertEqual(obs.title, "")
        self.assertIsNotNone(obs.screenshot_artifact_id)

    def test_parse_toolbox_web_search_response_and_live_fallback_when_webiq_key_omitted(self):
        mcp_response = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [
                    {
                        "type": "resource",
                        "_meta": {
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://www.rtings.com/headphones/reviews/sony/wh-1000xm6-wireless",
                                    "title": "Sony WH-1000XM6 Review - RTINGS.com",
                                    "start_index": 98,
                                    "end_index": 175,
                                },
                                {
                                    "type": "url_citation",
                                    "url": "https://www.soundguys.com/sony-wh-1000xm6-review-138252/",
                                    "title": "Sony WH-1000XM6 Review - SoundGuys",
                                    "start_index": 265,
                                    "end_index": 330,
                                },
                            ]
                        },
                        "resource": {
                            "uri": "web-search://results",
                            "mimeType": "text/plain",
                            "text": (
                                "- RTINGS measured 31.7 hours of continuous battery life and strong low-frequency ANC attenuation "
                                "([rtings.com](https://www.rtings.com/headphones/reviews/sony/wh-1000xm6-wireless)).\n"
                                "- SoundGuys praised the wider headband comfort with thin eyeglass frames "
                                "([soundguys.com](https://www.soundguys.com/sony-wh-1000xm6-review-138252/))."
                            ),
                        },
                    }
                ],
                "isError": False,
            },
        }
        parsed = bat_research.parse_toolbox_web_search_response(
            mcp_response,
            "Sony WH-1000XM6 review battery comfort",
            exclude_url="https://www.bestbuy.com/site/sony-wh-1000xm6/6580001.p",
        )
        self.assertEqual(parsed.provenance, "live_toolbox_web_search")
        self.assertEqual(len(parsed.sources), 2)
        self.assertEqual(parsed.sources[0].platform, "RTINGS")
        self.assertTrue(len(parsed.sources[0].content) <= config.MAX_DISPLAY_EXCERPT_CHARS)
        self.assertIn("31.7 hours", parsed.sources[0].content)

        async def fake_inspect(*, run_id: str, product_url: str, user_intent: str, **kwargs):
            return self._build_mock_live_baseline(run_id), {"steps": 1}

        fake_browsed = [
            bat_research.BrowsedSourceEvidence(
                url="https://www.rtings.com/headphones/reviews/sony/wh-1000xm6-wireless",
                platform="RTINGS",
                platform_category="Specialist Lab Review",
                title="Sony WH-1000XM6 Review - RTINGS.com",
                access_status="inspected",
                extracted_summary="Verified 31.7h battery test and cabin ANC attenuation.",
                screenshot_artifact_id=None,
            )
        ]

        with patch.object(pipeline, "inspect_product_listing_live", side_effect=fake_inspect), \
             patch.object(pipeline, "search_toolbox_web_search", new=AsyncMock(return_value=parsed)) as mock_tb_search, \
             patch.object(pipeline, "browse_permitted_sources_live", new=AsyncMock(return_value=fake_browsed)) as mock_browse, \
             patch.object(bat_research, "get_foundry_credential", side_effect=bat_research.FoundryAuthenticationRequiredError("offline test")):
            res = self.client.post(
                "/api/run",
                json={
                    "product_url": config.SCENARIOS["sony-wh1000xm6"]["product_url"],
                    "user_intent": "Battery life and glasses comfort",
                    "execution_mode": "live",
                },
            )

        self.assertEqual(res.status_code, 200)
        mock_tb_search.assert_awaited_once()
        mock_browse.assert_awaited_once()
        events = [
            json.loads(line[6:])
            for frame in res.text.split("\n\n")
            for line in frame.splitlines()
            if line.startswith("data: ")
        ]
        enrichment_evt = next(e for e in events if e["type"] == "enrichment")
        self.assertEqual(enrichment_evt["provider"], "toolbox_web_search")
        self.assertEqual(enrichment_evt["provenance"], "live_toolbox_web_search")
        self.assertEqual(len(enrichment_evt["sources"]), 2)

        cm = next(e for e in events if e["type"] == "claim_matrix")
        self.assertEqual(cm["run_mode"], "live_phase1_full")
        self.assertEqual(events[-1]["outcome"], "partial")

    def test_chrome_extension_bridge_end_to_end_and_zip_download(self):
        import base64
        from unittest.mock import MagicMock

        # 1. Verify /api/chrome-extension.zip serves a valid zip with manifest.json & scripts
        zip_res = self.client.get("/api/chrome-extension.zip")
        self.assertEqual(zip_res.status_code, 200)
        self.assertEqual(zip_res.headers.get("content-type"), "application/zip")

        # 2. Verify ClientExtensionBrowserDriver + /api/client-browser/observation + inspect_product_listing_live
        sony_url = "https://electronics.sony.com/audio/headphones/headband/p/wh1000xm6-b"
        emitted_commands: list[dict] = []
        fake_png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR-chrome-ext").decode("ascii")

        async def fake_emit_sse(payload: dict):
            emitted_commands.append(payload)
            if payload.get("type") == "client_browser_command" and payload.get("action") != "close":
                obs_res = self.client.post(
                    "/api/client-browser/observation",
                    json={
                        "run_id": payload["run_id"],
                        "command_id": payload["command_id"],
                        "url": sony_url,
                        "title": "Sony WH-1000XM6 Wireless Noise Canceling Headphones | WH1000XM6/B",
                        "status_code": 200,
                        "headings": ["WH-1000XM6 Wireless Noise Canceling Headphones"],
                        "body_excerpt": (
                            "Product Name: Sony WH-1000XM6\nBrand: Sony\nSKU: WH1000XM6/B\n"
                            "Observed Offer Price: $449.99 USD\n"
                            "HD Noise Canceling Processor QN3 with 12 microphones and 30-hour battery life."
                        ),
                        "candidate_lines": [
                            "HD Noise Canceling Processor QN3 with 12 microphones for industry-leading ANC.",
                            "Up to 30-hour battery life with 3-minute quick charge for 3 hours of playback.",
                        ],
                        "screenshot_data_url": f"data:image/png;base64,{fake_png_b64}",
                        "error": None,
                    },
                )
                self.assertEqual(obs_res.status_code, 200)

        fake_cred = MagicMock()
        screenshot_store.SCREENSHOT_STORE.start_run("ext_run_1")
        try:
            with patch.object(bat_research, "_init_foundry_chat_model", side_effect=RuntimeError("use DOM fallback")):
                baseline, shot_ids = asyncio.run(
                    bat_research.inspect_product_listing_live(
                        run_id="ext_run_1",
                        product_url=sony_url,
                        user_intent="10-hour flights and glasses comfort",
                        max_steps=2,
                        credential=fake_cred,
                        use_client_browser=True,
                        emit_sse=fake_emit_sse,
                    )
                )
        finally:
            screenshot_store.SCREENSHOT_STORE.finish_run("ext_run_1")

        self.assertEqual(baseline.data_provenance, "live_client_browser_observed")
        self.assertEqual(baseline.price, 449.99)
        self.assertIn("WH1000XM6", baseline.sku)
        self.assertEqual(len(shot_ids), 2)
        self.assertEqual(len(baseline.screenshots), 2)
        self.assertEqual(baseline.screenshots[0]["capture_kind"], "live_chrome_extension_screenshot")
        self.assertTrue(baseline.screenshots[0]["is_live_browser_capture"])
        self.assertIn("Chrome Extension", baseline.screenshots[0]["provenance_label"])
        actions = [c["action"] for c in emitted_commands if c.get("type") == "client_browser_command"]
        self.assertEqual(actions, ["goto", "scroll_down"])

    def test_windows_vm_playwright_worker_queue_and_heartbeat(self):
        import base64

        hb_res = self.client.post(
            "/api/client-browser/heartbeat",
            json={
                "worker_id": "win-vm-pdd-01",
                "hostname": "vm-pdd-win-chrome",
                "browser_version": "Chrome 135.0 (Windows Server 2022)",
                "mode": "windows_vm_playwright_chrome",
            },
        )
        self.assertEqual(hb_res.status_code, 200)
        st_res = self.client.get("/api/client-browser/status")
        self.assertEqual(st_res.status_code, 200)
        st_data = st_res.json()
        self.assertTrue(st_data["vm_worker_online"])
        self.assertEqual(st_data["workers"][0]["hostname"], "vm-pdd-win-chrome")

        fake_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc`\x00\x00\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
        fake_data_url = "data:image/png;base64," + base64.b64encode(fake_png).decode("ascii")

        async def fake_emit_sse(event_dict):
            if event_dict.get("type") == "client_browser_command" and event_dict.get("action") != "close":
                # Simulate Windows VM worker polling /api/client-browser/next-command
                poll = self.client.get(
                    "/api/client-browser/next-command",
                    params={"worker_id": "win-vm-pdd-01", "timeout": "0.1"},
                ).json()
                self.assertTrue(poll["has_command"])
                cmd = poll["command"]
                self.assertEqual(cmd["target"], "vm_worker")
                obs_res = self.client.post(
                    "/api/client-browser/observation",
                    json={
                        "run_id": cmd["run_id"],
                        "command_id": cmd["command_id"],
                        "url": cmd.get("url") or "https://electronics.sony.com/audio/headphones/headband/p/wh1000xm6-b",
                        "title": "Sony WH-1000XM6 Wireless Noise Canceling Headphones | Sony US",
                        "status_code": 200,
                        "headings": ["Sony WH-1000XM6 Wireless Noise Canceling Headphones"],
                        "body_excerpt": "[JSON-LD Product] name=Sony WH-1000XM6 | brand=Sony | sku=WH1000XM6/B | price=$449.99 USD",
                        "candidate_lines": ["Up to 30-hour battery life with quick charging"],
                        "screenshot_data_url": fake_data_url,
                        "worker_label": "Live Windows VM Native Chrome + Playwright (vm-pdd-win-chrome)",
                    },
                )
                self.assertEqual(obs_res.status_code, 200)

        class FakeStructuredModel:
            def invoke(self, messages):
                return bat_research.ExtractedListingSchema(
                    brand="Sony",
                    model="WH-1000XM6",
                    sku="WH1000XM6/B",
                    variant="Black",
                    price=449.99,
                    currency="USD",
                    seller="Sony Direct",
                    seller_claims=["Up to 30-hour battery life with quick charging"],
                )

        class FakeChatModel:
            def with_structured_output(self, schema):
                return FakeStructuredModel()

        screenshot_store.SCREENSHOT_STORE.start_run("vm_run_1")
        try:
            with patch.object(bat_research, "_init_foundry_chat_model", return_value=FakeChatModel()):
                baseline, shot_ids = asyncio.run(
                    bat_research.inspect_product_listing_live(
                        run_id="vm_run_1",
                        product_url="https://electronics.sony.com/audio/headphones/headband/p/wh1000xm6-b",
                        user_intent="Check comfort and ANC",
                        use_client_browser=True,
                        bridge_target="vm_worker",
                        emit_sse=fake_emit_sse,
                    )
                )
        finally:
            screenshot_store.SCREENSHOT_STORE.finish_run("vm_run_1")

        self.assertEqual(baseline.data_provenance, "live_vm_playwright_chrome_observed")
        self.assertEqual(len(shot_ids), 2)
        self.assertEqual(baseline.screenshots[0]["capture_kind"], "live_vm_playwright_chrome_screenshot")
        self.assertIn("vm-pdd-win-chrome", baseline.screenshots[0]["provenance_label"])

    def test_multi_store_targets_and_compatibility_matrix(self):
        scen_res = self.client.get("/api/scenarios")
        self.assertEqual(scen_res.status_code, 200)
        payload = scen_res.json()
        matrix = payload.get("site_compatibility_matrix") or []
        self.assertGreaterEqual(len(matrix), 5)
        for row in matrix:
            self.assertIn("azure_vm_playwright", row)
            self.assertIn("local_playwright", row)
            self.assertIn("azure_bat_tool", row)
            self.assertIn("webiq", row)
            self.assertNotIn("sample", row)
        for scen in payload["scenarios"]:
            targets = scen.get("store_targets") or []
            self.assertEqual(len(targets), 3)
            store_ids = {t["store_id"] for t in targets}
            self.assertEqual(store_ids, {"official", "amazon", "retailer"})
            for t in targets:
                self.assertIsNotNone(browser_driver._match_preset_key(t["url"]))

        report_res = self.client.get("/benchmark-report")
        self.assertEqual(report_res.status_code, 200)
        self.assertIn("Azure VM + Playwright", report_res.text)
        self.assertIn("Local Playwright", report_res.text)
        self.assertIn("Azure BAT Tool", report_res.text)
        self.assertIn("WebIQ Browse", report_res.text)

    def test_webiq_browse_mode_and_multi_query_search_discovery(self):
        fake_browse_res = {
            "status_code": 200,
            "access_status": "inspected",
            "url": "https://fellowproducts.com/products/ode-brew-grinder-gen-2",
            "title": "Fellow Ode Brew Grinder Gen 2 | Fellow",
            "content": (
                "# Fellow Ode Brew Grinder Gen 2\n"
                "$399.95 USD\n"
                "SKU: ODE-GEN2-BK\n"
                "- 64mm Gen 2 Brew Burrs engineered for pour-over, French press, and cold brew.\n"
                "- Anti-static technology reduces mess on your countertop.\n"
            ),
            "excerpt": "64mm Gen 2 Brew Burrs engineered for pour-over, French press, and cold brew.",
            "lastUpdatedAt": "2025-11-01",
            "crawledAt": "2025-11-01",
            "liveCrawlSucceeded": True,
            "liveCrawl": "fallback",
            "contentFormat": "markdown",
            "provenance": "live_webiq_browse",
        }

        screenshot_store.SCREENSHOT_STORE.start_run("webiq_browse_run_1")
        try:
            with patch.object(bat_research, "browse_webiq_url", new=AsyncMock(return_value=fake_browse_res)), \
                 patch.object(bat_research, "_init_foundry_chat_model", side_effect=RuntimeError("use markdown parser")):
                baseline, shot_ids = asyncio.run(
                    bat_research.inspect_product_listing_webiq_browse(
                        run_id="webiq_browse_run_1",
                        product_url="https://fellowproducts.com/products/ode-brew-grinder-gen-2",
                        user_intent="V60 pour-over clarity and low static retention",
                        webiq_key="test-webiq-key",
                    )
                )
        finally:
            screenshot_store.SCREENSHOT_STORE.finish_run("webiq_browse_run_1")

        self.assertEqual(baseline.data_provenance, "live_webiq_browse_observed")
        self.assertEqual(baseline.price, 399.95)
        self.assertEqual(len(shot_ids), 1)
        self.assertEqual(baseline.screenshots[0]["capture_kind"], "webiq_browse_diagnostic_card")
        self.assertFalse(baseline.screenshots[0]["is_live_browser_capture"])

        # Verify /api/plan supports execution_mode="webiq_browse"
        plan_res = self.client.post(
            "http://127.0.0.1:8011/api/plan",
            headers={
                "Host": "127.0.0.1:8011",
                "Origin": "http://127.0.0.1:8011",
                "X-WebIQ-Key": "test-webiq-key",
            },
            json={
                "product_url": "https://fellowproducts.com/products/ode-brew-grinder-gen-2",
                "user_intent": "V60 pour-over clarity",
                "execution_mode": "webiq_browse",
            },
        )
        self.assertEqual(plan_res.status_code, 200)
        plan_data = plan_res.json()["plan"]
        self.assertEqual(plan_data["execution_mode"], "webiq_browse")
        self.assertIn("WebIQ Browse", plan_data["steps"][0]["objective"])
        self.assertIn("/v3/browse", plan_data["steps"][0]["tools"][0])


    def test_selected_inspector_routes_every_unique_result_without_hidden_bat(self):
        sources = webiq.normalize_response({"webResults": [
            {"url": f"https://example.com/review-{n}", "title": f"Review {n}", "content": "SEARCH_ONLY_TEXT"}
            for n in range(5)
        ] + [
            {"url": "https://example.com/review-0", "title": "Duplicate", "content": "SEARCH_ONLY_TEXT"},
            {"url": "https://www.reddit.com/r/test/comments/review", "title": "Public visit attempt", "content": "SEARCH_ONLY_TEXT"},
            {"url": "https://www.youtube.com/watch?v=review", "title": "Public page text only", "content": "SEARCH_ONLY_TEXT"},
        ]}, "same query").sources
        observation = bat_research.PageObservation(
            url="https://example.com/review-0", title="Observed review", status_code=200,
            headings=["Measured result"], visible_text_excerpt="PAGE_CONTENT_FROM_SELECTED_INSPECTOR",
            price_candidates=[], sku_candidates=[], claim_candidates=[],
        )
        for mode in ("webiq_browse", "live", "client_browser", "vm_playwright"):
            with self.subTest(mode=mode):
                driver = AsyncMock()
                driver.observe.return_value = observation
                lifecycle = bat_research.SetupLifecycle(run_id=f"routing-{mode}", driver=driver)
                lifecycle.cleanup = AsyncMock()
                browse_response = webiq.WebIQBrowseResult({
                    "status_code": 200, "url": "https://example.com/review-0",
                    "title": "Observed review", "content": "PAGE_CONTENT_FROM_SELECTED_INSPECTOR",
                })
                with patch.object(bat_research, "browse_webiq_url", new=AsyncMock(return_value=browse_response)) as web_browse, \
                     patch.object(bat_research, "create_bat_browser_session", new=AsyncMock(return_value=lifecycle)) as cloud_bat, \
                     patch.object(bat_research, "ClientExtensionBrowserDriver", return_value=driver) as client_driver:
                    rows = asyncio.run(bat_research.browse_permitted_sources_live(
                        run_id=f"routing-{mode}", sources=sources, execution_mode=mode,
                        webiq_browse_key="request-only-test-key", emit_sse=AsyncMock(),
                    ))
                self.assertEqual(len(rows), 7)
                self.assertEqual(sum(row.status == "inspected" for row in rows), 7)
                self.assertTrue(all(row.inspection_method == mode for row in rows))
                self.assertTrue(all("SEARCH_ONLY_TEXT" not in row.observed_excerpt for row in rows))
                self.assertNotIn("page_content", rows[0].to_dict())
                if mode == "webiq_browse":
                    self.assertEqual(web_browse.await_count, 7)
                    cloud_bat.assert_not_awaited()
                    client_driver.assert_not_called()
                else:
                    web_browse.assert_not_awaited()
                    self.assertEqual(driver.goto.await_count, 7)
                    if mode == "live":
                        cloud_bat.assert_awaited_once()
                        client_driver.assert_not_called()
                    else:
                        cloud_bat.assert_not_awaited()
                        self.assertEqual(client_driver.call_args.kwargs["bridge_target"], "vm_worker" if mode == "vm_playwright" else "extension")

    def test_webiq_failed_browse_never_reuses_search_excerpt_or_another_inspector(self):
        sources = webiq.normalize_response({"webResults": [
            {"url": "https://example.com/review", "title": "Review", "content": "SEARCH_ONLY_TEXT"},
        ]}, "query").sources
        with patch.object(bat_research, "browse_webiq_url", new=AsyncMock(return_value=webiq.WebIQBrowseResult({
            "status_code": 403, "url": sources[0].url, "error_reason": "Forbidden",
        }))), patch.object(bat_research, "create_bat_browser_session", new=AsyncMock()) as bat:
            rows = asyncio.run(bat_research.browse_permitted_sources_live(
                run_id="failed-webiq", sources=sources, execution_mode="webiq_browse", webiq_browse_key="test-key",
            ))
        self.assertEqual(rows[0].status, "restricted_by_site")
        self.assertEqual(rows[0].observed_excerpt, "")
        self.assertEqual(rows[0].page_content, "")
        self.assertNotIn("SEARCH_ONLY_TEXT", str(rows[0].to_dict()))
        bat.assert_not_awaited()
        with self.assertRaises(bat_research.FoundryResearchError):
            asyncio.run(bat_research.browse_permitted_sources_live(
                run_id="missing-key", sources=sources, execution_mode="webiq_browse",
            ))



    def test_four_step_plan_keeps_cart_action_outside_research_and_inspector_consistent(self):
        for mode in ("webiq_browse", "live", "client_browser", "vm_playwright"):
            plan = pipeline.build_agent_plan(
                "https://example.com/product", "Battery", execution_mode=mode,
                has_live_webiq_key=True,
            )
            self.assertEqual(len(plan["steps"]), 4)
            self.assertEqual(plan["steps"][2]["id"], "permitted_source_browsing")
            self.assertIn("eligible", plan["steps"][2]["title"])
            self.assertEqual(plan["post_research_actions"][0]["id"], "prepare_cart_gate")

    def test_benchmark_recording_whitelists_metadata_and_removes_credentials_and_content(self):
        from scripts.refresh_benchmark_report import record_event, public_url
        run = {"events": []}
        record_event(run, {"type": "enrichment", "provider": "webiq", "status": "ready",
            "api_key": "MUST_NOT_RETAIN_SECRET",
            "sources": [{"url": "https://example.com/review?token=MUST_NOT_RETAIN_SECRET", "snippet": "MUST_NOT_RETAIN_CONTENT"}],
            "video_sources": []}, 1.0)
        record_event(run, {"type": "source_inspection", "source": {
            "url": "https://example.com/review", "inspection_method": "webiq_browse",
            "status": "inspected", "page_content": "MUST_NOT_RETAIN_CONTENT",
            "observed_excerpt": "MUST_NOT_RETAIN_CONTENT", "fetched_characters": 321,
        }}, 2.0)
        record_event(run, {"type": "claim_matrix", "executive_summary": "MUST_NOT_RETAIN_CONTENT",
            "model_deployment": "gpt-5.6-terra", "synthesis_status": "complete", "matrix": [{}], "evidence_gaps": []}, 3.0)
        payload = json.dumps(run)
        self.assertNotIn("MUST_NOT_RETAIN", payload)
        self.assertIn("321", payload)
        self.assertEqual(public_url("https://user:password@example.com/test?sig=secret"), "https://example.com/test")
        self.assertEqual(public_url("javascript:alert(1)"), "")
        self.assertEqual(run["synthesis_status"], "complete")

    def test_interrupted_benchmark_preserves_observed_inspection_progress(self):
        from scripts.refresh_benchmark_report import record_event, finalize_inspection_counts
        run = {"events": [], "outcome": "error"}
        for index, status in enumerate(("inspected", "error", "inspecting")):
            record_event(run, {"type": "source_inspection", "source": {
                "requested_url": f"https://example.com/review-{index}", "inspection_method": "vm_playwright",
                "status": status, "fetched_characters": 300 if status == "inspected" else 0,
            }}, float(index))
        finalize_inspection_counts(run)
        self.assertEqual(run["inspected"], 1)
        self.assertEqual(run["not_inspected"], 2)
        self.assertTrue(run["coverage_incomplete"])
        self.assertEqual(run["outcome"], "error")

    def test_community_recording_keeps_counts_and_scope_not_grounding_or_model_prose(self):
        from scripts.refresh_benchmark_report import record_event
        run = {"events": [], "community_exploration": True, "exploration_domains": ["forum.example.com"]}
        record_event(run, {
            "type": "community_exploration", "phase": "completed", "status": "completed",
            "actions": 6, "pages": 2, "new_pages": 2, "searches": 1, "discussion_blocks": 4,
            "reason": "MUST_NOT_RETAIN", "goal": "MUST_NOT_RETAIN",
            "raw_grounding": "MUST_NOT_RETAIN",
        }, 4.0)
        self.assertEqual(run["community_outcome"]["discussion_blocks"], 4)
        self.assertEqual(run["community_outcome"]["new_pages"], 2)
        self.assertNotIn("MUST_NOT_RETAIN", json.dumps(run))
        record_event(run, {"type": "community_exploration", "phase": "decision",
            "action": "search", "domain": "forum.example.com", "query": "MUST_NOT_RETAIN"}, 5.0)
        self.assertEqual(run["events"][-1]["domain"], "forum.example.com")
        self.assertNotIn("MUST_NOT_RETAIN", json.dumps(run))

    def test_five_path_runner_enables_exploration_only_for_supported_methods(self):
        import httpx
        from scripts import refresh_benchmark_report as recorder
        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                text='data: {"type":"status","stage":"start","run_id":"aaaaaaaaaaaa"}\n\n'
                     'data: {"type":"done","outcome":"partial"}\n\n')

        case = {"id": "fixture", "url": "https://example.com/product",
                "intent": "Reliability", "web_query": "owner experiences"}
        with httpx.Client(base_url="https://fixture.example", transport=httpx.MockTransport(respond)) as client, \
             patch.object(recorder, "LocalChrome"):
            for method in recorder.METHODS:
                run = recorder.execute_run(client, case, method, "private-key", "fixture",
                    community_exploration=True, exploration_domains=["forum.example.com"])
                enabled = method in recorder.COMMUNITY_METHODS
                self.assertEqual(run["community_exploration"], enabled)
                self.assertEqual(requests[-1]["community_exploration"], enabled)
                self.assertEqual("exploration_domains" in requests[-1], enabled)
                self.assertEqual(run["exploration_domains"], ["forum.example.com"] if enabled else [])
                if method == "search_only":
                    self.assertEqual(requests[-1]["execution_mode"], "webiq_browse")
                    self.assertFalse(requests[-1]["inspect_review_pages"])

    def test_private_community_action_receipt_retains_safe_justification_and_redacts_secrets(self):
        from scripts.visual_replay import VisualRecorder
        recorder = VisualRecorder("live", "private-key")
        recorder.capture({"type": "community_exploration", "phase": "decision", "step": 1,
            "action": "search", "domain": "forum.example.com",
            "goal": "Reliability", "reason": "Seek public discussion private-key",
            "raw_grounding": "MUST_NOT_RETAIN"}, None)
        saved = json.dumps(recorder.saved)
        self.assertIn("Reliability", saved)
        self.assertIn("[REDACTED]", saved)
        self.assertNotIn("private-key", saved)
        self.assertNotIn("MUST_NOT_RETAIN", saved)

    def test_runner_waits_only_for_api_concurrency_rejections(self):
        import httpx
        from scripts import refresh_benchmark_report as recorder
        attempts = []

        def respond(request):
            attempts.append(request)
            if len(attempts) <= 2:
                return httpx.Response(429, json={"detail":
                    "Another Live Web Research session is currently active (limit: 1). Please wait."})
            return httpx.Response(200, text='data: {"type":"done","outcome":"partial"}\n\n')

        case = {"id": "fixture", "url": "https://example.com/product",
                "intent": "Reliability", "web_query": "owner experiences"}
        with httpx.Client(base_url="https://fixture.example", transport=httpx.MockTransport(respond)) as client, \
             patch.object(recorder.time, "sleep") as sleep:
            run = recorder.execute_run(client, case, "search_only", "key", "fixture")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(run["admission_retries"], 2)
        self.assertEqual(run["outcome"], "partial")
        self.assertIn("queue_wait_s", run)
        with httpx.Client(base_url="https://fixture.example",
                transport=httpx.MockTransport(lambda request: httpx.Response(429, json={"detail": "Quota limit"}))) as client, \
             patch.object(recorder.time, "sleep") as sleep:
            run = recorder.execute_run(client, case, "search_only", "key", "fixture")
        sleep.assert_not_called()
        self.assertEqual(run["error_http_status"], 429)

    def test_sse_keepalives_do_not_replace_or_extend_total_run_budget(self):
        actual_wait_for = asyncio.wait_for

        async def short_wait(awaitable, timeout):
            return await actual_wait_for(awaitable, timeout=min(timeout, 0.01))

        async def slow_producer(**kwargs):
            await asyncio.sleep(0.06)
            yield 'data: {"type":"done","outcome":"partial"}\n\n'

        async def collect():
            return [chunk async for chunk in pipeline.run_due_diligence_pipeline(
                product_url="https://example.com/product",
                user_intent="Battery", execution_mode="live",
            )]

        for budget, should_timeout in ((0.2, False), (0.025, True)):
            with self.subTest(budget=budget), \
                 patch.dict(os.environ, {"PDD_RUN_TIMEOUT_SECONDS": str(budget)}), \
                 patch.object(pipeline, "_run_phase1_live_web_research", new=slow_producer), \
                 patch.object(pipeline.asyncio, "wait_for", new=short_wait):
                chunks = asyncio.run(collect())
            self.assertIn(": keep-alive\n\n", chunks)
            self.assertEqual(any("run_timeout_exceeded" in chunk for chunk in chunks), should_timeout)
            self.assertTrue(any('"type"' in chunk and '"done"' in chunk for chunk in chunks))





class TestUnattendedBrowserVerification(unittest.TestCase):
    @staticmethod
    def observation(*, challenge=False, status=200, title="Product review", text="Observed owner feedback."):
        return bat_research.PageObservation(
            url="https://example.com/review", title="Complete CAPTCHA" if challenge else title,
            status_code=status, headings=[], visible_text_excerpt="Verify you are human" if challenge else text,
            price_candidates=[], sku_candidates=[], claim_candidates=[],
            restricted=challenge or status in (401, 403, 429, 451),
        )

    def test_recoverable_challenges_are_distinct_from_hard_access_denials(self):
        classify = bat_research.classify_site_restriction
        self.assertEqual(classify(status_code=403, title="CAPTCHA", visible_text="Verify you are human"), "captcha")
        self.assertEqual(classify(status_code=403, title="Denied", visible_text="Access denied"), "access_denied")
        for status, expected in ((401, "login_required"), (429, "rate_limited"), (451, "legal_restriction")):
            self.assertEqual(classify(status_code=status, title="CAPTCHA", visible_text="CAPTCHA"), expected)
        self.assertEqual(classify(status_code=200, title="Sign in", visible_text="Sign in to continue"), "login_required")
        self.assertEqual(classify(status_code=200, title="Cloudflare product review", visible_text="Public product information"), "")

    def test_automatic_clearance_resumes_without_reloading_or_switching_inspectors(self):
        driver = AsyncMock()
        driver.observe.side_effect = [self.observation(challenge=True, status=403), self.observation()]
        emit = AsyncMock()
        with patch.object(bat_research.asyncio, "sleep", new=AsyncMock()) as wait:
            result = asyncio.run(bat_research.observe_with_verification(
                driver, stage="listing_digestion", capture_title="Initial listing", emit_sse=emit,
            ))
        self.assertFalse(result.restricted)
        self.assertEqual(result.verification_attempts, 1)
        self.assertEqual(result.verification_outcome, "cleared")
        self.assertEqual(driver.observe.await_count, 2)
        driver.goto.assert_not_awaited()
        wait.assert_awaited_once_with(3.0)
        self.assertTrue(any(call.args[0]["label"] == "Browser verification cleared" for call in emit.await_args_list))

    def test_main_frame_navigation_updates_stale_challenge_status(self):
        from types import SimpleNamespace
        from scripts.refresh_benchmark_report import LocalChrome
        page = SimpleNamespace(main_frame=object())
        cdp = bat_research.LiveCDPBrowserDriver("synthetic-fixture", run_id="status-test")
        cdp._page = page
        cdp._last_status_code = 403
        local = LocalChrome.__new__(LocalChrome)
        local.page, local.status = page, 403
        response = SimpleNamespace(status=200, frame=page.main_frame,
                                   request=SimpleNamespace(is_navigation_request=lambda: True))
        for receive in (cdp._record_main_frame_response, local._record_navigation_response):
            receive(response)
            receive(SimpleNamespace(status=403, frame=object(), request=response.request))
            receive(SimpleNamespace(status=403, frame=page.main_frame,
                                    request=SimpleNamespace(is_navigation_request=lambda: False)))
        self.assertEqual(cdp._last_status_code, 200)
        self.assertEqual(local.status, 200)

    def test_unresolved_captcha_has_two_rechecks_then_other_sources_continue(self):
        sources = webiq.normalize_response({"webResults": [
            {"url": f"https://example.com/review-{index}", "title": "Owner review", "content": "Search evidence"}
            for index in range(3)
        ]}, "review").sources
        driver = AsyncMock()
        driver.observe.side_effect = [
            self.observation(challenge=True), self.observation(challenge=True), self.observation(challenge=True),
            self.observation(), self.observation(status=403, title="Denied", text="Access denied"),
        ]
        lifecycle = bat_research.SetupLifecycle(run_id="verification-test", driver=driver)
        lifecycle.cleanup = AsyncMock()
        with patch.object(bat_research, "create_bat_browser_session", new=AsyncMock(return_value=lifecycle)), \
             patch.object(bat_research.asyncio, "sleep", new=AsyncMock()) as wait:
            rows = asyncio.run(bat_research.browse_permitted_sources_live(
                run_id="verification-test", sources=sources, credential=object(), emit_sse=AsyncMock(),
            ))
        self.assertEqual(driver.goto.await_count, 3)
        self.assertEqual(driver.observe.await_count, 5)
        self.assertEqual(wait.await_count, 2)
        self.assertEqual(rows[0].verification_attempts, 2)
        self.assertEqual(rows[0].verification_outcome, "unresolved")
        self.assertEqual(rows[0].status, "restricted_by_site")
        self.assertIn("unresolved", rows[0].restriction_reason)
        self.assertEqual(rows[0].page_content, "")
        self.assertEqual(rows[0].to_dict()["retrieved_response"]["verification_attempts"], 2)
        self.assertEqual(rows[1].status, "inspected")
        self.assertEqual(rows[2].verification_attempts, 0)
        self.assertEqual(rows[2].restriction_kind, "access_denied")
        lifecycle.cleanup.assert_awaited_once()

    def test_initial_listing_uses_the_same_verification_recovery(self):
        driver = AsyncMock()
        driver.observe.side_effect = [self.observation(challenge=True), self.observation()]
        lifecycle = bat_research.SetupLifecycle(run_id="listing-verification-test", driver=driver)
        lifecycle.cleanup = AsyncMock()
        with patch.object(bat_research, "create_bat_browser_session", new=AsyncMock(return_value=lifecycle)), \
             patch.object(bat_research, "_init_foundry_chat_model", side_effect=ValueError("Synthetic offline test")), \
             patch.object(bat_research.asyncio, "sleep", new=AsyncMock()):
            baseline, _ = asyncio.run(bat_research.inspect_product_listing_live(
                run_id="listing-verification-test", product_url="https://example.com/product",
                user_intent="Reliability", credential=object(), max_steps=1, emit_sse=AsyncMock(),
            ))
        self.assertEqual(baseline.data_provenance, "live_bat_observed")
        self.assertEqual(baseline.retrieved_response["verification_outcome"], "cleared")
        self.assertEqual(baseline.retrieved_response["verification_attempts"], 1)
        self.assertNotIn("CAPTCHA", baseline.retrieved_response["content"])
        self.assertEqual(driver.goto.await_count, 1)
        lifecycle.cleanup.assert_awaited_once()


class TestSiteOnlyResearchContract(unittest.TestCase):
    def test_full_search_grounding_reaches_synthesis_but_not_exported_source_text(self):
        from unittest.mock import MagicMock
        sentinel = "A_FINDING_BEYOND_THE_DISPLAY_EXCERPT"
        enrichment = webiq.normalize_response({"webResults": [{
            "url": "https://example.com/review", "title": "Owner review",
            "content": "Introductory material. " * 25 + sentinel,
        }]}, "review", provenance="live_webiq")
        self.assertNotIn(sentinel, enrichment.sources[0].citation()["snippet"])
        baseline = bat_research.build_unverified_seed_baseline("https://example.com/product", "Reliability")
        generated = bat_research.DueDiligenceSynthesisSchema(
            recommendation_badge="Further verification needed", fit_label="Partial fit",
            executive_summary="The attributed evidence addresses reliability with caveats.",
            claim_rows=[bat_research.SynthesizedClaimRow(
                seller_claim="Reliability", verdict="Context-dependent",
                cross_platform_evidence="Owner experience varies by usage.",
                user_intent_impact="Verify longer ownership duration.",
                source_label="Owner review", source_url="https://example.com/review",
            )],
        )
        invoke = AsyncMock(return_value=generated)
        model = MagicMock()
        model.with_structured_output.return_value.ainvoke = invoke
        with patch.object(bat_research, "_init_foundry_chat_model", return_value=model):
            synthesis = asyncio.run(bat_research.synthesize_due_diligence_with_gpt(
                baseline=baseline, user_intent="Reliability", enrichment=enrichment,
                browsed_sources=[], inspect_review_pages=False, credential=object(),
            ))
        prompt = invoke.call_args.args[0]
        self.assertIn(sentinel, prompt)
        self.assertIn("SEARCH-ONLY REVIEWS", prompt)
        self.assertNotIn("Fetched page context", prompt)
        report = pipeline._format_markdown_report(
            plan=pipeline.build_agent_plan("https://example.com/product", "Reliability",
                                           execution_mode="webiq_browse", inspect_review_pages=False),
            baseline=baseline, enrichment=enrichment, synthesis=synthesis,
        )
        self.assertNotIn(sentinel, report)
        self.assertNotIn("Introductory material.", report)
        self.assertIn("https://example.com/review", report)

    def test_public_community_visits_stop_at_actual_site_challenges(self):
        sources = webiq.normalize_response({"webResults": [
            {"url": url, "title": "Owner feedback", "content": "Search evidence"}
            for url in ("https://www.reddit.com/r/test/comments/owner/",
                        "https://www.amazon.com/product-reviews/example",
                        "https://www.youtube.com/watch?v=review")
        ]}, "review").sources
        driver = AsyncMock()
        driver.observe.return_value = bat_research.PageObservation(
            url="https://www.reddit.com/r/test/comments/owner/", title="Access challenge",
            status_code=403, headings=[], visible_text_excerpt="Sign in to continue",
            price_candidates=[], sku_candidates=[], claim_candidates=[],
            restricted=True, restriction_reason="Site denied public access",
        )
        lifecycle = bat_research.SetupLifecycle(run_id="site-challenge-test", driver=driver)
        lifecycle.cleanup = AsyncMock()
        with patch.object(bat_research, "create_bat_browser_session", new=AsyncMock(return_value=lifecycle)):
            rows = asyncio.run(bat_research.browse_permitted_sources_live(
                run_id="site-challenge-test", sources=sources, credential=object(),
            ))
        self.assertEqual(driver.goto.await_count, 3)
        self.assertTrue(all(row.status == "restricted_by_site" for row in rows))
        self.assertTrue(all(not row.page_content for row in rows))
        self.assertTrue(all(row.to_dict()["retrieved_response"]["content"] == "" for row in rows))
        lifecycle.cleanup.assert_awaited_once()

    def test_published_report_contains_only_one_matched_comparison_round(self):
        import re
        root = Path(__file__).resolve().parents[2]
        recording = json.loads((root / "docs" / "browser-benchmark-runs.json").read_text(encoding="utf-8"))
        runs = recording["runs"]
        self.assertEqual(len(recording["cases"]), 1)
        self.assertEqual(len(runs), 5)
        self.assertEqual({run["method"] for run in runs},
                         {"webiq_browse", "live", "client_browser", "vm_playwright", "search_only"})
        self.assertEqual({run["case_id"] for run in runs}, {recording["cases"][0]["id"]})
        for field in ("product_url", "intent", "web_query"):
            self.assertEqual(len({run[field] for run in runs}), 1, field)
        self.assertRegex(recording["round_id"], r"^\d{8}T\d{6}Z$")
        for field in ("source_commit", "server_revision", "grounding_policy", "deployment"):
            self.assertEqual(len({run[field] for run in runs}), 1, field)
        self.assertTrue(all(run["revision_consistent"] for run in runs))
        self.assertTrue(all(run["grounding_policy"] == "passage-3000-internal-200-display" for run in runs))
        self.assertEqual(len({run["visual_evidence_id"] for run in runs}), 5)
        newest = next(run for run in runs if run["method"] == "search_only")
        self.assertEqual(recording["source_commit"], newest["source_commit"])
        self.assertEqual(recording["server_revision"], newest["server_revision"])
        self.assertFalse(newest["inspect_review_pages"])
        self.assertEqual(newest["baseline_inspector"], "webiq_browse")
        self.assertEqual(newest["inspected"], 0)
        self.assertEqual(newest["evidence_metrics"]["inspected_context_characters"], 0)
        self.assertFalse(any(event["type"] in ("source_inspection", "browsed_sources") for event in newest["events"]))
        self.assertIn("not a controlled answer-quality experiment", recording["comparison_note"])
        self.assertIn("one runtime revision", recording["comparison_note"])
        self.assertTrue(all(run["recording_scope"] == "four-stage-site-research" for run in runs))
        self.assertTrue(all(run.get("visual_evidence_id") for run in runs))
        self.assertEqual(recording["recorded_at"],
                         max(run.get("completed_at") or run["started_at"] for run in runs))
        report = (root / "api" / "app" / "benchmark_report.html").read_text(encoding="utf-8")
        self.assertEqual(report, (root / "docs" / "browser-execution-methods-benchmark.html").read_text(encoding="utf-8"))
        embedded = re.findall(r'<script id="benchmark-run-data" type="application/json">(.*?)</script>', report, re.S)
        self.assertEqual(len(embedded), 1)
        self.assertEqual(json.loads(embedded[0]), recording)
        self.assertNotIn("Replay earlier attempt", report)
        self.assertNotIn("The original four runs used revision 22", report)
        self.assertIn("Review verification", report)

    def test_recording_retains_verification_metadata_not_source_content(self):
        from scripts.refresh_benchmark_report import record_event, finalize_inspection_counts
        run = {"events": []}
        record_event(run, {"type": "source_inspection", "source": {
            "url": "https://example.com/review", "inspection_method": "live",
            "access_status": "restricted_by_site", "restriction_kind": "verification_challenge",
            "verification_attempts": 2, "verification_outcome": "unresolved",
            "page_content": "publisher text must not be recorded", "retrieved_response": {"content": "raw"},
        }}, 4.2)
        finalize_inspection_counts(run)
        self.assertEqual(run["inspections"][0]["verification_attempts"], 2)
        self.assertEqual(run["inspections"][0]["verification_outcome"], "unresolved")
        self.assertEqual(run["inspections"][0]["restriction_kind"], "verification_challenge")
        self.assertNotIn("publisher text", json.dumps(run))
        self.assertNotIn("retrieved_response", json.dumps(run))

    def test_report_comparison_note_checks_snapshots_and_revision_stability(self):
        from tempfile import TemporaryDirectory
        from scripts import refresh_benchmark_report as recorder
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "api" / "app").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "api" / "app" / "benchmark_report.html").write_text(
                '<script id="benchmark-run-data" type="application/json">{}</script>', encoding="utf-8")
            output = root / "docs" / "browser-benchmark-runs.json"
            data = {"pipeline_contract": "one-inspector-four-research-stages",
                    "cases": [{"id": "matched"}], "runs": [{
                        "case_id": "matched", "method": "search_only", "inspect_review_pages": False,
                        "events": [], "started_at": "2026-10-21T00:00:00+00:00",
                        "server_revision": "revision-a", "source_commit": "commit-a",
                        "grounding_policy": "passage-3000-internal-200-display", "revision_consistent": True,
                        "product_url": "https://example.com/product", "intent": "Reliability", "web_query": "owner review",
                    }]}
            with patch.object(recorder, "ROOT", root), patch.object(recorder, "RESULTS", output), \
                    patch.object(recorder, "REPORT", root / "docs" / "report.html"):
                recorder.publish(data)
                self.assertIn("one runtime revision", json.loads(output.read_text())["comparison_note"])
                data["runs"][0]["revision_consistent"] = False
                recorder.publish(data)
                self.assertIn("do not share a verified", json.loads(output.read_text())["comparison_note"])
                self.assertNotIn("one runtime revision", json.loads(output.read_text())["comparison_note"])

    def test_routes_schemas_skills_and_plan_are_site_only(self):
        client = TestClient(portal_main.app)
        for path in ("/api/video/evidence", "/api/video/content-understanding",
                     "/api/video/content-understanding/status", "/api/replay/cu-proof",
                     "/api/replay/cu-binary-proof"):
            self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.post(path, json={}).status_code, 404)
        schemas = client.get("/openapi.json").json()
        self.assertFalse(any("/video/" in path for path in schemas["paths"]))
        for name in ("PlanRequest", "RunRequest"):
            fields = schemas["components"]["schemas"][name]["properties"]
            self.assertFalse(any("video" in field for field in fields))
        skills = client.get("/api/skills").json()["skills"]
        self.assertEqual(len(skills), 4)
        self.assertFalse(any("video" in skill["name"] for skill in skills))
        plan = pipeline.build_agent_plan("https://example.com/product", "Travel",
                                        execution_mode="webiq_browse", has_live_webiq_key=True)
        self.assertEqual(len(plan["steps"]), 4)
        self.assertEqual(plan["steps"][-1]["id"], "synthesize_verdict")
        self.assertNotIn("video", json.dumps(plan).lower())

    def test_both_discovery_paths_exclude_non_page_hosts_without_dropping_bh_photo(self):
        urls = ("https://www.youtube.com/watch?v=test", "https://youtu.be/test",
                "https://player.vimeo.com/test", "https://example.com/review.mp4",
                "https://www.bhphotovideo.com/c/product/review.html",
                "https://example.com/video-product-review.html",
                "https://notyoutube.com/review")
        expected = set(urls[:2] + urls[4:])
        web = webiq.normalize_response({"webResults": [
            {"url": url, "title": "Review", "content": "Attributed short excerpt"} for url in urls
        ]}, "review")
        toolbox = bat_research.parse_toolbox_web_search_response({"content": [{
            "text": "Review",
            "_meta": {"annotations": [{"url": url, "title": "Review"} for url in urls]},
        }]}, "review")
        self.assertEqual({source.url for source in web.sources}, expected)
        self.assertEqual({source.url for source in toolbox.sources}, expected)

    def test_legacy_recording_projection_preserves_outcomes_timings_and_attempts(self):
        from scripts.refresh_benchmark_report import site_only_recording
        original = {"pipeline_contract": "one-inspector-five-research-stages",
                    "cases": [{"id": "case", "video_query": "old query"}],
                    "runs": [{"case_id": "case", "elapsed_s": 73.09, "outcome": "partial",
                              "video_links": 4, "events": [
                                  {"type": "status", "stage": "analyze_video_reviews", "elapsed_s": 50},
                                  {"type": "video_analysis", "videos": []},
                                  {"type": "done", "outcome": "partial", "elapsed_s": 73.09}]}]}
        projected = site_only_recording(original)
        run = projected["runs"][0]
        self.assertEqual(run["elapsed_s"], 73.09)
        self.assertEqual(run["outcome"], "partial")
        self.assertEqual(run["recording_scope"], "historical-full-run")
        self.assertEqual(run["events"], [original["runs"][0]["events"][-1]])
        self.assertNotIn("video", json.dumps(projected))
        self.assertIn("video_links", original["runs"][0])

    def test_old_private_receipts_are_not_served_or_included_in_site_projection(self):
        from tempfile import TemporaryDirectory
        import replay_evidence as replay
        with TemporaryDirectory() as folder, patch.object(replay, "PRIVATE_ROOT", Path(folder)):
            path = Path(folder) / ("run-" + "e" * 12 + ".json")
            path.write_text(json.dumps({"method": "live", "sources": [],
                                       "videos": [{"private": "retired receipt"}]}), encoding="utf-8")
            response = TestClient(portal_main.app).get("/api/replay/evidence/" + "e" * 12)
            self.assertNotIn("videos", response.json()["saved"])
            self.assertIn("videos", json.loads(path.read_text(encoding="utf-8")))
            self.assertEqual(TestClient(portal_main.app).get("/api/replay/assets/" + "e" * 64 + ".jpg").status_code, 404)

    def test_deployment_packages_only_referenced_projected_receipts_and_verified_pngs(self):
        from tempfile import TemporaryDirectory
        import hashlib
        import io
        import tarfile
        from scripts.deploy_container_app import build_filtered_source_tarball
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("api/app", "agents", "skills", "browser_extension", "docs",
                         "api/private_replay/assets"):
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in ("Dockerfile", ".dockerignore", "requirements.txt", "api/__init__.py"):
                (root / name).write_text("", encoding="utf-8")
            run_id = "a" * 12
            png = b"\x89PNG\r\n\x1a\nretained-test-bytes"
            digest = hashlib.sha256(png).hexdigest()
            (root / "docs/browser-benchmark-runs.json").write_text(json.dumps({
                "runs": [{"visual_evidence_id": run_id}]}), encoding="utf-8")
            (root / f"api/private_replay/run-{run_id}.json").write_text(json.dumps({
                "method": "live", "videos": ["retired"],
                "community_actions": [{"action": "scroll", "reason": "Inspect visible discussion"}],
                "screenshots": [{"sha256": digest}]}), encoding="utf-8")
            (root / "api/private_replay/cu-binary-proof.json").write_text("{}", encoding="utf-8")
            (root / f"api/private_replay/assets/{digest}.png").write_bytes(png)
            (root / f"api/private_replay/assets/{digest}.jpg").write_bytes(b"retired")
            with tarfile.open(fileobj=io.BytesIO(build_filtered_source_tarball(root))) as archive:
                self.assertFalse(any(name.startswith("api/private_replay/") for name in archive.getnames()))
            with tarfile.open(fileobj=io.BytesIO(build_filtered_source_tarball(root, include_private_replay=True))) as archive:
                names = archive.getnames()
                receipt = json.load(archive.extractfile(f"api/private_replay/run-{run_id}.json"))
                self.assertNotIn("videos", receipt)
                self.assertEqual(receipt["community_actions"][0]["action"], "scroll")
                self.assertIn(f"api/private_replay/assets/{digest}.png", names)
                self.assertFalse(any("cu-binary" in name or name.endswith(".jpg") for name in names))


class TestVisualReplayEvidence(unittest.TestCase):
    def test_expiry_worker_removes_context_without_a_followup_request(self):
        import replay_evidence as replay

        async def check():
            async with replay._lifespan(None):
                replay.TRANSIENT["a" * 12] = (time.time() - 1, {"expired": True})
                await asyncio.sleep(.02)
                self.assertNotIn("a" * 12, replay.TRANSIENT)
                replay.TRANSIENT["b" * 12] = (time.time() + 900, {"temporary": True})
            self.assertFalse(replay.TRANSIENT)

        with patch.object(replay, "TRANSIENT", replay.OrderedDict()):
            asyncio.run(check())

    def test_private_routes_expiry_bounds_and_actual_assets(self):
        from tempfile import TemporaryDirectory
        import replay_evidence as replay
        from pydantic import ValidationError

        with TemporaryDirectory() as folder, patch.object(replay, "PRIVATE_ROOT", Path(folder)), patch.object(replay, "TRANSIENT", replay.OrderedDict()):
            client = TestClient(portal_main.app)
            run_id = "a" * 12
            private = Path(folder) / f"run-{run_id}.json"
            private.write_text('{"sources":[{"excerpt":"Actual browser observation"}]}')
            payload = {"run_id": run_id, "expires_at": time.time() + 3600,
                       "evidence": {"synthesis": {"summary": "Temporary generated result", "gaps": []}}}
            response = client.post("/api/replay/transient", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertLessEqual(response.json()["expires_at"], time.time() + 900)
            response = client.get(f"/api/replay/evidence/{run_id}")
            self.assertEqual(response.json()["temporary_status"], "available")
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertNotIn("Temporary grounded", private.read_text())
            replay.TRANSIENT[run_id] = (time.time() - 1, {"secret": "expired"})
            self.assertEqual(client.get(f"/api/replay/evidence/{run_id}").json()["temporary_status"], "expired_or_not_retained")
            self.assertNotIn(run_id, replay.TRANSIENT)
            self.assertEqual(client.post("/api/replay/transient", json={**payload, "expires_at": time.time() - 1}).status_code, 410)
            self.assertEqual(client.post("/api/replay/transient", json={**payload, "evidence": {"text": "x" * 150001}}).status_code, 413)
            self.assertEqual(client.post("/api/replay/transient", json={**payload, "evidence": {"sources": [{"excerpt": "Raw grounding"}]}}).status_code, 422)
            self.assertEqual(client.get("/api/replay/evidence/invalid").status_code, 404)
            self.assertEqual(client.get("/api/replay/assets/not-a-capture.svg").status_code, 404)
            self.assertEqual(client.get("/api/replay/cu-proof").status_code, 404)
            assets = Path(folder) / "assets"; assets.mkdir()
            filename = "a" * 64 + ".png"
            (assets / filename).write_bytes(b"\x89PNG\r\n\x1a\nfixture")
            image = client.get("/api/replay/assets/" + filename)
            self.assertEqual(image.headers["content-type"], "image/png")
            self.assertIn("no-store", image.headers["cache-control"])
            for index in range(33):
                client.post("/api/replay/transient", json={**payload, "run_id": f"{index:012x}"})
            self.assertEqual(len(replay.TRANSIENT), 32)
            self.assertNotIn("000000000000", replay.TRANSIENT)
            with self.assertRaises(ValidationError):
                replay.TransientEvidenceRequest(**{**payload, "expires_at": float("inf")})

    def test_recorder_retains_browser_only_and_checks_real_png_hash(self):
        import hashlib
        import httpx
        from tempfile import TemporaryDirectory
        from scripts import visual_replay as visual

        png = b"\x89PNG\r\n\x1a\nsynthetic-test-fixture"
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, content=png if request.method == "GET" else b"{}")

        with TemporaryDirectory() as folder, patch.object(visual, "PRIVATE_ROOT", Path(folder)), httpx.Client(base_url="https://fixture.example", transport=httpx.MockTransport(respond)) as client:
            recorder = visual.VisualRecorder("webiq_browse", "private-key")
            recorder.capture({"type": "product_baseline", "baseline": {"brand": "WebIQ-only private-key"}}, client)
            recorder.capture({"type": "browsed_sources", "sources": [{"url": "https://example.com/review", "observed_excerpt": "Temporary WebIQ excerpt"}]}, client)
            recorder.capture({"type": "claim_matrix", "synthesis_status": "complete", "executive_summary": "Temporary synthesis"}, client)
            payload = recorder.finish({"run_id": "a" * 12}, client)
            archive = (Path(folder) / ("run-" + "a" * 12 + ".json")).read_text()
            self.assertNotIn("WebIQ", archive)
            self.assertNotIn("synthesis", archive)
            self.assertNotIn("private-key", json.dumps(payload))
            self.assertEqual(payload["evidence"]["synthesis"]["summary"], "Temporary synthesis")
            self.assertNotIn("Temporary WebIQ excerpt", json.dumps(payload))
            self.assertNotIn("sources", payload["evidence"])
            browser = visual.VisualRecorder("client_browser", "")
            shot = {"is_live_browser_capture": True, "content_url": "/api/screenshots/cap-aaaaaaaaaaaa", "sha256": hashlib.sha256(png).hexdigest()}
            browser.capture({"type": "screenshot", "screenshot": shot}, client)
            self.assertEqual(len(browser.saved["screenshots"]), 1)
            self.assertEqual((Path(folder) / "assets" / (shot["sha256"] + ".png")).read_bytes(), png)
            with self.assertRaisesRegex(ValueError, "hash"):
                browser.capture({"type": "screenshot", "screenshot": {**shot, "sha256": "bad"}}, client)
            browser.capture({"type": "screenshot", "screenshot": {"is_live_browser_capture": False}}, client)
            self.assertEqual(len(browser.saved["screenshots"]), 1)
            self.assertEqual(browser.text("Observed content"), "Observed content")


if __name__ == "__main__":
    unittest.main(verbosity=2)
