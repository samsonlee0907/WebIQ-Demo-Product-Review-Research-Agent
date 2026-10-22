"""Configuration, capability status registry, and curated sample scenarios.

Explicitly separates:
  - Phase-1 Live Web Research (`live_phase1_web_research`, `live_listing_only_no_webiq_key`, `live_search_only_unverified`)
  - Curated sample walkthroughs (`sample_fixture`)
  - Unverified custom URLs (`illustrative_custom_url`)
  - Truthful integration/capability states (`phase1_configured`, `deferred_to_phase_2`, `simulation_only`, `sample_fixtures`)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# A source checkout is offline by default; live resources must be explicitly configured.
DEFAULT_FOUNDRY_TENANT_ID = ""
DEFAULT_FOUNDRY_SUBSCRIPTION_ID = ""
DEFAULT_FOUNDRY_RESOURCE_GROUP = ""
DEFAULT_FOUNDRY_ACCOUNT_NAME = ""
DEFAULT_FOUNDRY_PROJECT_NAME = ""
DEFAULT_FOUNDRY_PROJECT_ENDPOINT = ""
DEFAULT_FOUNDRY_MODEL_DEPLOYMENT = ""
DEFAULT_FOUNDRY_TOOLBOX_NAME = ""
DEFAULT_PLAYWRIGHT_CONNECTION_NAME = ""
DEFAULT_PLAYWRIGHT_WORKSPACE_NAME = ""
DEFAULT_PLAYWRIGHT_WORKSPACE_RESOURCE_ID = ""
DEFAULT_FOUNDRY_AUTHORIZED_OBJECT_ID = ""

FOUNDRY_TENANT_ID = (
    os.environ.get("FOUNDRY_TENANT_ID")
    or DEFAULT_FOUNDRY_TENANT_ID
).strip()
FOUNDRY_AUTHORIZED_OBJECT_ID = os.environ.get(
    "FOUNDRY_AUTHORIZED_OBJECT_ID", DEFAULT_FOUNDRY_AUTHORIZED_OBJECT_ID
).strip()
FOUNDRY_ACCOUNT_UPN = os.environ.get("FOUNDRY_ACCOUNT_UPN", "").strip()
FOUNDRY_SUBSCRIPTION_ID = os.environ.get(
    "FOUNDRY_SUBSCRIPTION_ID", DEFAULT_FOUNDRY_SUBSCRIPTION_ID
).strip()
FOUNDRY_RESOURCE_GROUP = os.environ.get(
    "FOUNDRY_RESOURCE_GROUP", DEFAULT_FOUNDRY_RESOURCE_GROUP
).strip()
FOUNDRY_ACCOUNT_NAME = os.environ.get(
    "FOUNDRY_ACCOUNT_NAME", DEFAULT_FOUNDRY_ACCOUNT_NAME
).strip()
FOUNDRY_PROJECT_NAME = os.environ.get(
    "FOUNDRY_PROJECT_NAME", DEFAULT_FOUNDRY_PROJECT_NAME
).strip()
FOUNDRY_PROJECT_ENDPOINT = (
    os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    or os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    or DEFAULT_FOUNDRY_PROJECT_ENDPOINT
).strip()
FOUNDRY_MODEL_DEPLOYMENT = (
    os.environ.get("FOUNDRY_MODEL_DEPLOYMENT")
    or os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")
    or DEFAULT_FOUNDRY_MODEL_DEPLOYMENT
).strip()
FOUNDRY_TOOLBOX_NAME = (
    os.environ.get("FOUNDRY_TOOLBOX_NAME")
    or os.environ.get("TOOLBOX_NAME")
    or DEFAULT_FOUNDRY_TOOLBOX_NAME
).strip()
PLAYWRIGHT_CONNECTION_NAME = os.environ.get(
    "PLAYWRIGHT_CONNECTION_NAME", DEFAULT_PLAYWRIGHT_CONNECTION_NAME
).strip()
PLAYWRIGHT_WORKSPACE_NAME = os.environ.get(
    "PLAYWRIGHT_WORKSPACE_NAME", DEFAULT_PLAYWRIGHT_WORKSPACE_NAME
).strip()
PLAYWRIGHT_WORKSPACE_RESOURCE_ID = os.environ.get(
    "PLAYWRIGHT_WORKSPACE_RESOURCE_ID", DEFAULT_PLAYWRIGHT_WORKSPACE_RESOURCE_ID
).strip()



# Backward-compatible module aliases
PROJECT_ENDPOINT = FOUNDRY_PROJECT_ENDPOINT
MODEL_DEPLOYMENT_NAME = FOUNDRY_MODEL_DEPLOYMENT
ENVIRONMENT_NAME = os.environ.get("AZURE_AI_ENVIRONMENT_NAME", "product-due-diligence-dev").strip()
TOOLBOX_NAME = FOUNDRY_TOOLBOX_NAME

# Web IQ FAQ / Legal §2.5.1: displayed per-source grounding excerpts must not exceed
# the lesser of 200 characters or the publisher limit.
MAX_DISPLAY_EXCERPT_CHARS = 200

# Hosted / Azure Container Apps runtime & security settings
PDD_HOSTED_MODE = os.environ.get("PDD_HOSTED_MODE", "").strip().lower() in {"1", "true", "yes"}
FOUNDRY_MANAGED_IDENTITY_CLIENT_ID = (
    os.environ.get("FOUNDRY_MANAGED_IDENTITY_CLIENT_ID")
    or os.environ.get("AZURE_CLIENT_ID")
    or ""
).strip()
PDD_TRUSTED_INGRESS_PROXY = os.environ.get("PDD_TRUSTED_INGRESS_PROXY", "").strip().lower()
PDD_RUN_TIMEOUT_SECONDS = float(os.environ.get("PDD_RUN_TIMEOUT_SECONDS", "1200.0"))
PDD_MAX_CONCURRENT_RUNS = int(os.environ.get("PDD_MAX_CONCURRENT_RUNS", "2"))
PDD_MAX_CONCURRENT_LIVE_RUNS = int(os.environ.get("PDD_MAX_CONCURRENT_LIVE_RUNS", "1"))


def is_hosted_mode() -> bool:
    """Return True when running in hosted cloud mode (`PDD_HOSTED_MODE=true`)."""
    return os.environ.get("PDD_HOSTED_MODE", "").strip().lower() in {"1", "true", "yes"}


def get_allowed_hosts() -> frozenset[str]:
    """Return lowercased allowed public hostnames configured in `PDD_ALLOWED_HOSTS`."""
    raw = os.environ.get("PDD_ALLOWED_HOSTS", "")
    items = set()
    for part in raw.split(","):
        cleaned = part.strip().lower()
        if cleaned and cleaned != "*":
            if cleaned.startswith("https://") or cleaned.startswith("http://"):
                cleaned = cleaned.split("://", 1)[1].split("/", 1)[0]
            cleaned = cleaned.split(":", 1)[0]
            if cleaned:
                items.add(cleaned)
    return frozenset(items)


def get_allowed_origins() -> frozenset[str]:
    """Return explicit HTTPS origins allowed by `PDD_ALLOWED_ORIGINS` / `PDD_ALLOWED_HOSTS`."""
    raw = os.environ.get("PDD_ALLOWED_ORIGINS", "")
    origins = set()
    for part in raw.split(","):
        cleaned = part.strip().rstrip("/").lower()
        if cleaned.startswith("https://") and "*" not in cleaned:
            origins.add(cleaned)
    for host in get_allowed_hosts():
        origins.add(f"https://{host}")
    return frozenset(origins)

DISCLAIMER = (
    "Site Research Notice: This portal compares four live URL-browsing "
    "engines — (1) Azure VM + Playwright, (2) Local Playwright (desktop extension), "
    "(3) Azure BAT Tool (configured workspace/toolbox), and (4) WebIQ Browse (POST /v3/browse) — "
    "alongside two-path search discovery (WebIQ /v3/search/web when a WebIQ key is "
    "supplied, or Foundry Toolbox web_search when no key is supplied). "
    f"Live query planning and evidence synthesis require a configured Foundry deployment ({FOUNDRY_MODEL_DEPLOYMENT or 'not configured'}). "
    "Curated Sample Walkthrough mode is explicitly selectable and is never used as a hidden fallback when live "
    "calls fail. Cart actions remain a local read-only simulation; checkout and payment routes are hard-blocked."
)

CAPABILITY_STATUS: dict[str, dict[str, str]] = {
    "browser_listing_digestion": {
        "state": "not_connected",
        "live_phase1_state": "configuration_supplied_not_probed" if FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL_DEPLOYMENT else "configuration_required",
        "label": "4 Live Browse Engines: Azure VM + Playwright • Local Playwright • Azure BAT Tool • WebIQ Browse (/v3/browse)",
        "detail": (
            "In Live Research modes, inspects the target product URL using your chosen engine: "
            "(1) Azure VM + Playwright, (2) Local Playwright (desktop extension), "
            "(3) Azure BAT Tool (configured workspace/toolbox), or (4) Microsoft WebIQ Browse "
            "(POST https://api.microsoft.ai/v3/browse with liveCrawl='fallback' & markdown extraction), "
            "then extracts structured claims with the configured Foundry model."
        ),
    },
    "web_iq_review_search": {
        "state": "sample_fixtures",
        "live_phase1_state": "two_path_webiq_search_or_toolbox_web_search",
        "label": "Two-Path Discovery: WebIQ Search (/v3/search/web) OR Foundry Toolbox web_search",
        "detail": (
            "Path 1 (when WebIQ key is supplied): invokes Microsoft WebIQ /v3/search/web with primary + supplementary "
            "queries to enrich the candidate review sites. "
            "Path 2 (when no WebIQ key is supplied): automatically invokes the Foundry Toolbox 'web_search' MCP tool "
            "registered in the configured toolbox to discover independent review sites."
        ),
    },
    "permitted_source_browsing": {
        "state": "not_connected",
        "live_phase1_state": "bounded_readonly_bat_or_webiq_browse",
        "label": "Bounded permitted-source deep browsing via selected engine (Playwright / BAT / WebIQ /v3/browse)",
        "detail": (
            "Attempts normal read-only visits with the selected inspector, including community and retailer review pages. "
            "No blanket Reddit exclusion; actual access denials, sign-in walls and challenges remain enforced. "
            "Search-only reviews deliberately skips these visits. No media is downloaded or processed."
            " BAT, Local and VM optionally support model-guided, domain-scoped link/scroll exploration "
            "with per-action browser observations; this is off by default and does not restrict general discovery."
        ),
    },
    "model_synthesis": {
        "state": "not_connected",
        "live_phase1_state": "configuration_supplied_not_probed" if FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL_DEPLOYMENT else "configuration_required",
        "label": "Configured Foundry model for live planning/synthesis; templates in sample mode",
        "detail": (
            "Uses the configured Foundry deployment to plan multi-platform search queries and synthesize seller claims "
            "against retrieved listing, attributed search excerpts and inspected pages with direct citations."
        ),
    },
    "cart_action": {
        "state": "simulation_only",
        "live_phase1_state": "simulation_only",
        "label": "Simulation only (never mutates retailer carts)",
        "detail": (
            "Validates SKU, variant, seller, and price ceiling against an in-memory demo ticket and renders "
            "a simulated SVG confirmation card. Checkout and payment routes are hard-blocked."
        ),
    },
}

SKILLS_METADATA: dict[str, dict[str, Any]] = {
    "product-listing-digestion": {
        "name": "product-listing-digestion",
        "title": "Product Listing Digestion & Claim Extraction",
        "stage": "Stage 1 — Product Baseline",
        "description": "Separates testable seller claims (battery, acoustics, dimensions, warranty) from marketing puffery.",
        "file_stem": "skill_product_listing_digestion.SKILL.md",
        "repo_rel_path": "skills/product-listing-digestion/SKILL.md",
    },
    "multi-platform-review-discovery": {
        "name": "multi-platform-review-discovery",
        "title": "Multi-Platform Review-Site Discovery",
        "stage": "Stage 2 — Cross-Platform Discovery",
        "description": "Queries specialist labs, editorial reviews, enthusiast forums, and community threads with <=200-char attributed excerpts.",
        "file_stem": "skill_multi_platform_review_discovery.SKILL.md",
        "repo_rel_path": "skills/multi-platform-review-discovery/SKILL.md",
    },
    "claim-reality-matrix": {
        "name": "claim-reality-matrix",
        "title": "Claim-vs-Reality Matrix & Evidence Quality Signals",
        "stage": "Stage 4 — Decision Synthesis",
        "description": "Compares seller claims against independent evidence and scores coverage quality (not proof of review authenticity).",
        "file_stem": "skill_claim_reality_matrix.SKILL.md",
        "repo_rel_path": "skills/claim-reality-matrix/SKILL.md",
    },
    "human-gated-cart-action": {
        "name": "human-gated-cart-action",
        "title": "Human-Gated Cart Simulation & Checkout Guardrail",
        "stage": "Stage 5 — Guarded Cart Simulation",
        "description": "Locks simulated cart actions to verified SKU, variant, seller, and price ceiling while blocking checkout.",
        "file_stem": "skill_human_gated_cart_action.SKILL.md",
        "repo_rel_path": "skills/human-gated-cart-action/SKILL.md",
    },
}

TOOLBOX_META: dict[str, dict[str, Any]] = {
    "tb-product-due-diligence": {
        "name": "tb-product-due-diligence",
        "description": "Reference Foundry Toolbox definition (skills + tool_search + MCP connection descriptors; not active in local sample mode).",
        "skills": list(SKILLS_METADATA.keys()),
        "mcp_connections": ["browser_automation", "web_search"],
        "status": "reference_definition_only",
    }
}

SCENARIOS: dict[str, dict[str, Any]] = {
    "sony-wh1000xm6": {
        "key": "sony-wh1000xm6",
        "title": "Sony WH-1000XM6 Headphones",
        "short_label": "Travel ANC Headphones",
        "tagline": "10-hr flights • Thin glasses comfort • Windows + iPhone multipoint • Under $460",
        "category": "Wireless Noise-Canceling Headphones",
        "sample_badge": "Multi-Store Product Preset",
        "toolbox": "tb-product-due-diligence",
        "agent": "pdd-shopping-agent",
        "skills": list(SKILLS_METADATA.keys()),
        "product_url": "https://www.bestbuy.com/site/sony-wh-1000xm6-wireless-noise-canceling-over-the-ear-headphones-black/6580001.p",
        "store_targets": [
            {
                "store_id": "official",
                "label": "🏷️ Sony Official Store",
                "short_label": "Official Store",
                "domain": "electronics.sony.com",
                "url": "https://electronics.sony.com/audio/headphones/headband/p/wh1000xm6-b",
                "observed_price_hint": "$459.99 USD",
                "recommended_mode": "vm_playwright",
                "compatibility": {
                    "vm_playwright": "verified",
                    "client_browser": "verified",
                    "live": "blocked_403",
                    "webiq_browse": "indexed_markdown",
                    "sample": "verified",
                },
                "note": "Verified HTTP 200 ($459.99, SKU WH-1000XM6/B) on Azure VM + Playwright & Local Playwright; Cloud BAT returns HTTP 403; WebIQ /v3/browse returns indexed markdown without PNGs.",
            },
            {
                "store_id": "amazon",
                "label": "📦 Amazon Storefront",
                "short_label": "Amazon",
                "domain": "amazon.com",
                "url": "https://www.amazon.com/dp/B0F3PQHWTZ",
                "observed_price_hint": "$458.00 USD",
                "recommended_mode": "vm_playwright",
                "compatibility": {
                    "vm_playwright": "verified",
                    "client_browser": "verified",
                    "live": "interstitial",
                    "webiq_browse": "partial_cache",
                    "sample": "verified",
                },
                "note": "Verified HTTP 200 ($458.00, ASIN B0F3PQHWTZ) on Azure VM + Playwright (auto-clicks splash) & Local Playwright; WebIQ /v3/browse cannot click splash or read live Buy Box.",
            },
            {
                "store_id": "retailer",
                "label": "🏬 Best Buy Retailer",
                "short_label": "Best Buy",
                "domain": "bestbuy.com",
                "url": "https://www.bestbuy.com/site/sony-wh-1000xm6-wireless-noise-canceling-over-the-ear-headphones-black/6580001.p",
                "observed_price_hint": "$449.99 USD (Sample)",
                "recommended_mode": "client_browser",
                "compatibility": {
                    "vm_playwright": "blocked_asn",
                    "client_browser": "verified",
                    "live": "blocked_http2",
                    "webiq_browse": "indexed_markdown",
                    "sample": "verified",
                },
                "note": "Akamai blocks Azure datacenter IPs (AS8075); use Local Playwright (residential IP) or WebIQ /v3/browse (if indexed).",
            },
        ],
        "user_intent": "I fly 10-hour international routes, wear thin prescription glasses, need reliable multipoint switching between a Windows work laptop and iPhone, and want a durable folding hinge under $450.",
        "default_webiq_query": "Sony WH-1000XM6 review RTINGS SoundGuys Head-Fi Wirecutter glasses comfort multipoint battery",
    },
    "asus-pg32ucdm-oled": {
        "key": "asus-pg32ucdm-oled",
        "title": "ASUS ROG Swift PG32UCDM 32\" 4K QD-OLED",
        "short_label": "4K QD-OLED Coding Monitor",
        "tagline": "macOS text clarity • Silent fanless cooling • Burn-in warranty • 90W USB-C KVM",
        "category": "4K QD-OLED Desktop Monitor",
        "sample_badge": "Multi-Store Product Preset",
        "toolbox": "tb-product-due-diligence",
        "agent": "pdd-shopping-agent",
        "skills": list(SKILLS_METADATA.keys()),
        "product_url": "https://www.bhphotovideo.com/c/product/1810032-REG/asus_pg32ucdm_32_rog_swift_oled.html",
        "store_targets": [
            {
                "store_id": "official",
                "label": "🏷️ ASUS ROG Official Store",
                "short_label": "Official Store",
                "domain": "rog.asus.com",
                "url": "https://rog.asus.com/us/monitors/27-to-31-5-inches/rog-swift-oled-pg32ucdm/",
                "observed_price_hint": "$1,199.00 USD",
                "recommended_mode": "vm_playwright",
                "compatibility": {
                    "vm_playwright": "verified",
                    "client_browser": "verified",
                    "live": "verified",
                    "webiq_browse": "indexed_markdown",
                    "sample": "verified",
                },
                "note": "Verified HTTP 200 ($1,199.00) across Azure VM + Playwright, Local Playwright, Cloud BAT, and WebIQ /v3/browse (markdown).",
            },
            {
                "store_id": "amazon",
                "label": "📦 Amazon Storefront",
                "short_label": "Amazon",
                "domain": "amazon.com",
                "url": "https://www.amazon.com/dp/B0CV26XVMD",
                "observed_price_hint": "$900.60 – $1,099.99 USD",
                "recommended_mode": "vm_playwright",
                "compatibility": {
                    "vm_playwright": "verified",
                    "client_browser": "verified",
                    "live": "verified",
                    "webiq_browse": "partial_cache",
                    "sample": "verified",
                },
                "note": "Verified HTTP 200 (ASIN B0CV26XVMD) across Azure VM + Playwright, Local Playwright, and Cloud BAT.",
            },
            {
                "store_id": "retailer",
                "label": "🏬 B&H Photo Retailer",
                "short_label": "B&H Photo",
                "domain": "bhphotovideo.com",
                "url": "https://www.bhphotovideo.com/c/product/1810032-REG/asus_pg32ucdm_32_rog_swift_oled.html",
                "observed_price_hint": "$1,299.00 USD (Sample)",
                "recommended_mode": "client_browser",
                "compatibility": {
                    "vm_playwright": "blocked_asn",
                    "client_browser": "verified",
                    "live": "blocked_403",
                    "webiq_browse": "indexed_markdown",
                    "sample": "verified",
                },
                "note": "Cloudflare challenges Azure datacenter IPs (AS8075); use Local Playwright or WebIQ /v3/browse (if indexed).",
            },
        ],
        "user_intent": "I code 9 hours a day on a MacBook Pro in a bright room and play PC games at night. I care about macOS text fringing, silent fanless operation, USB-C 90W charging, and OLED burn-in coverage.",
        "default_webiq_query": "ASUS ROG Swift PG32UCDM review RTINGS TFTCentral MacRumors Notebookcheck text clarity macOS",
    },
    "fellow-ode-gen2": {
        "key": "fellow-ode-gen2",
        "title": "Fellow Ode Brew Grinder Gen 2",
        "short_label": "Single-Dose Coffee Grinder",
        "tagline": "Low static retention • Quiet morning grind • V60 & AeroPress vs. Espresso check",
        "category": "Single-Dose Conical/Flat Burr Coffee Grinder",
        "sample_badge": "Multi-Store Product Preset",
        "toolbox": "tb-product-due-diligence",
        "agent": "pdd-shopping-agent",
        "skills": list(SKILLS_METADATA.keys()),
        "product_url": "https://fellowproducts.com/products/ode-brew-grinder-gen-2",
        "store_targets": [
            {
                "store_id": "official",
                "label": "🏷️ Fellow Official Store",
                "short_label": "Official Store",
                "domain": "fellowproducts.com",
                "url": "https://fellowproducts.com/products/ode-brew-grinder-gen-2",
                "observed_price_hint": "$399.95 USD",
                "recommended_mode": "vm_playwright",
                "compatibility": {
                    "vm_playwright": "verified",
                    "client_browser": "verified",
                    "live": "verified",
                    "webiq_browse": "indexed_markdown",
                    "sample": "verified",
                },
                "note": "Verified HTTP 200 ($399.95) across Azure VM + Playwright, Local Playwright, Cloud BAT, and WebIQ /v3/browse.",
            },
            {
                "store_id": "amazon",
                "label": "📦 Amazon Storefront",
                "short_label": "Amazon",
                "domain": "amazon.com",
                "url": "https://www.amazon.com/dp/B0BLRMCM9Y",
                "observed_price_hint": "$399.95 USD",
                "recommended_mode": "vm_playwright",
                "compatibility": {
                    "vm_playwright": "verified",
                    "client_browser": "verified",
                    "live": "verified",
                    "webiq_browse": "partial_cache",
                    "sample": "verified",
                },
                "note": "Verified HTTP 200 ($399.95, ASIN B0BLRMCM9Y) across Azure VM + Playwright, Local Playwright, and Cloud BAT.",
            },
            {
                "store_id": "retailer",
                "label": "🏬 Breville Official (BES876 Benchmark)",
                "short_label": "Breville Store",
                "domain": "breville.com",
                "url": "https://www.breville.com/en-us/product/bes876",
                "observed_price_hint": "Official Brand Store",
                "recommended_mode": "vm_playwright",
                "compatibility": {
                    "vm_playwright": "verified",
                    "client_browser": "verified",
                    "live": "verified",
                    "webiq_browse": "indexed_markdown",
                    "sample": "verified",
                },
                "note": "Verified HTTP 200 on Breville Official Store via Azure VM + Playwright, Local Playwright, and WebIQ /v3/browse.",
            },
        ],
        "user_intent": "I brew light-roast V60 pour-over and AeroPress every morning in a quiet apartment and want to know if static retention is truly under 0.1g and whether it can also grind for espresso under $360.",
        "default_webiq_query": "Fellow Ode Gen 2 review Home-Barista Wirecutter CoffeeGeek retention static noise espresso",
    },
}

SITE_COMPATIBILITY_MATRIX: list[dict[str, str]] = [
    {
        "site": "Sony Official Store (electronics.sony.com)",
        "category": "Official Brand Store",
        "azure_vm_playwright": "✅ HTTP 200 ($459.99, SKU WH-1000XM6/B + 4 PNGs)",
        "local_playwright": "✅ HTTP 200 (Local IP + Full DOM/PNGs)",
        "azure_bat_tool": "❌ HTTP 403 (Akamai TLS/Bot Gate)",
        "webiq": "⚠️ Indexed Markdown / 202 LiveCrawl (No viewport PNGs or interactive tabs)",
        "recommended": "vm_playwright",
    },
    {
        "site": "Amazon Storefront (amazon.com)",
        "category": "Marketplace / Retailer",
        "azure_vm_playwright": "✅ HTTP 200 ($458 / $900 / $399, auto-clicks splash)",
        "local_playwright": "✅ HTTP 200 (Local Session + Buy Box)",
        "azure_bat_tool": "⚠️ Splash Interstitial on Sony ASIN",
        "webiq": "⚠️ 403/404 or Stale Catalog Cache (Cannot click splash or read live Buy Box)",
        "recommended": "vm_playwright",
    },
    {
        "site": "ASUS ROG Official (rog.asus.com)",
        "category": "Official Brand Store",
        "azure_vm_playwright": "✅ HTTP 200 ($1,199.00 + Full Specs/PNGs)",
        "local_playwright": "✅ HTTP 200 ($1,199.00 + Full Specs/PNGs)",
        "azure_bat_tool": "✅ HTTP 200 ($1,199.00 + Full Specs/PNGs)",
        "webiq": "✅ HTTP 200 Indexed Markdown via /v3/browse (No viewport PNGs)",
        "recommended": "vm_playwright",
    },
    {
        "site": "Fellow Official Store (fellowproducts.com)",
        "category": "Official Brand Store",
        "azure_vm_playwright": "✅ HTTP 200 ($399.95 + JSON-LD/PNGs)",
        "local_playwright": "✅ HTTP 200 ($399.95 + JSON-LD/PNGs)",
        "azure_bat_tool": "✅ HTTP 200 ($399.95 + JSON-LD/PNGs)",
        "webiq": "✅ HTTP 200 Indexed Markdown via /v3/browse ($399.95 text, no PNGs)",
        "recommended": "vm_playwright",
    },
    {
        "site": "Breville Official Store (breville.com)",
        "category": "Official Brand Store",
        "azure_vm_playwright": "✅ HTTP 200 (Full Specs + PNGs)",
        "local_playwright": "✅ HTTP 200 (Full Specs + PNGs)",
        "azure_bat_tool": "✅ HTTP 200 (Full Specs + PNGs)",
        "webiq": "✅ HTTP 200 Indexed Markdown via /v3/browse (No viewport PNGs)",
        "recommended": "vm_playwright",
    },
    {
        "site": "Best Buy / B&H Photo (bestbuy.com / bhphotovideo.com)",
        "category": "Major Big-Box Retailer",
        "azure_vm_playwright": "❌ Blocked (Azure Datacenter ASN AS8075)",
        "local_playwright": "✅ HTTP 200 (Passes via Residential IP)",
        "azure_bat_tool": "❌ Blocked (ERR_HTTP2 / Cloudflare 403)",
        "webiq": "⚠️ Indexed Cache if Crawled; 202/404 on Unindexed SKU URLs (No PNGs)",
        "recommended": "client_browser",
    },
    {
        "site": "Independent Review Sites & Reddit (rtings.com / soundguys.com / reddit.com)",
        "category": "Editorial Labs & Community",
        "azure_vm_playwright": "Historical labs accessible; community pages now attempted, access unverified",
        "local_playwright": "Historical labs/forums accessible; individual site access varies",
        "azure_bat_tool": "Historical labs accessible; community pages now attempted, access unverified",
        "webiq": "✅ Fast /v3/browse Markdown on Editorial Labs + /v3/search for Discovery",
        "recommended": "vm_playwright",
    },
]


def load_skill_markdown(skill_name: str) -> str:
    meta = SKILLS_METADATA.get(skill_name)
    if not meta:
        return f"# {skill_name}\nSkill metadata not found."
    here = Path(__file__).resolve().parent
    candidates = [
        here.parents[1] / meta["repo_rel_path"],
        here / meta["file_stem"],
    ]
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return f"# {meta['title']}\n\n{meta['description']}"
