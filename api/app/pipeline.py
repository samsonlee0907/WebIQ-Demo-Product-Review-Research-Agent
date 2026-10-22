"""Product Due-Diligence Pipeline Orchestrator (Shopper-First & Truthful Provenance).

Key design guarantees:
  - Everything starts from two user inputs: `product_url` and `user_intent`.
  - Clearly distinguishes:
      * `sample_demo`: Curated sample fixtures for the 3 built-in products
      * `mixed_live_search_sample_analysis`: Live request-scoped Web IQ search + local sample analysis
      * `illustrative_custom_url`: Unverified custom URL mode (never fabricates prices, SKUs,
        or guessed review citations when live connectors are unavailable)
  - Enforces Microsoft Web IQ FAQ & Legal Terms (§2.5.1):
      * Capped <= 200-character per-source excerpts
      * One-click source attribution next to claims
      * No indefinite persistence of raw live Web IQ passages
  - Leads results with decision-relevant findings ("What Fits Your Needs") and watch-outs
    ("Key Risks & Caveats"), without unsupported numeric scores (e.g. 91/100) or fake "Strong Buy" labels.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

try:
    from .community_exploration import DEFAULT_DOMAINS, LIMITS, ExplorationOutcome, explore_communities, normalize_domains
    from .bat_research import (
        CLIENT_BROWSER_BROKER,
        BrowsedSourceEvidence,
        FoundryResearchError,
        browse_permitted_sources_live,
        build_deterministic_query_plan,
        build_unverified_seed_baseline,
        inspect_product_listing_live,
        inspect_product_listing_webiq_browse,
        plan_review_queries_with_gpt,
        redact_secrets,
        search_toolbox_web_search,
        synthesize_due_diligence_with_gpt,
    )
    from .browser_driver import BROWSER_DRIVER, ProductBaseline
    from .config import (
        CAPABILITY_STATUS,
        DISCLAIMER,
        FOUNDRY_MODEL_DEPLOYMENT,
        FOUNDRY_PROJECT_ENDPOINT,
        FOUNDRY_TENANT_ID,
        FOUNDRY_TOOLBOX_NAME,
        MAX_DISPLAY_EXCERPT_CHARS,
        PDD_RUN_TIMEOUT_SECONDS,
        PLAYWRIGHT_WORKSPACE_NAME,
        SKILLS_METADATA,
        load_skill_markdown,
    )
    from .screenshot_store import SCREENSHOT_STORE, render_evidence_svg
    from .url_safety import UrlValidationError, safe_display_url, validate_public_https_url
    from .webiq import (
        Enrichment,
        WebIQError,
        normalize_response,
        search_webiq_reviews,
        truncate_display_excerpt,
        validate_request,
    )
except ImportError:
    from community_exploration import DEFAULT_DOMAINS, LIMITS, ExplorationOutcome, explore_communities, normalize_domains
    from bat_research import (
        CLIENT_BROWSER_BROKER,
        BrowsedSourceEvidence,
        FoundryResearchError,
        browse_permitted_sources_live,
        build_deterministic_query_plan,
        build_unverified_seed_baseline,
        inspect_product_listing_live,
        inspect_product_listing_webiq_browse,
        plan_review_queries_with_gpt,
        redact_secrets,
        search_toolbox_web_search,
        synthesize_due_diligence_with_gpt,
    )
    from browser_driver import BROWSER_DRIVER, ProductBaseline
    from config import (
        CAPABILITY_STATUS,
        DISCLAIMER,
        FOUNDRY_MODEL_DEPLOYMENT,
        FOUNDRY_PROJECT_ENDPOINT,
        FOUNDRY_TENANT_ID,
        FOUNDRY_TOOLBOX_NAME,
        MAX_DISPLAY_EXCERPT_CHARS,
        PDD_RUN_TIMEOUT_SECONDS,
        PLAYWRIGHT_WORKSPACE_NAME,
        SKILLS_METADATA,
        load_skill_markdown,
    )
    from screenshot_store import SCREENSHOT_STORE, render_evidence_svg
    from url_safety import UrlValidationError, safe_display_url, validate_public_https_url
    from webiq import (
        Enrichment,
        WebIQError,
        normalize_response,
        search_webiq_reviews,
        truncate_display_excerpt,
        validate_request,
    )



class ArtifactStorageUnavailable(Exception):
    """Raised when artifact storage cannot be accessed."""


_ARTIFACT_DIR = Path(tempfile.gettempdir()) / "pdd_artifacts"
_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACTS: dict[str, dict[str, Any]] = {}
_LIVE_ARTIFACT_TTL_SECONDS = 15 * 60
_LIVE_ARTIFACT_LIMIT = 32


def register_artifact(
    filename: str,
    content: bytes,
    media_type: str = "text/markdown",
    *,
    contains_live_webiq: bool = False,
) -> dict[str, Any]:
    artifact_id = f"rep-{uuid.uuid4().hex[:12]}"
    target = _ARTIFACT_DIR / f"{artifact_id}_{filename}"
    if not contains_live_webiq:
        try:
            target.write_bytes(content)
        except OSError as exc:
            raise ArtifactStorageUnavailable("Report storage is unavailable.") from exc
    else:
        for old_id, old_meta in list(ARTIFACTS.items()):
            if not old_meta["persisted"] and old_meta["expires_at"] <= time.monotonic():
                del ARTIFACTS[old_id]
        live_ids = [key for key, value in ARTIFACTS.items() if not value["persisted"]]
        for old_id in live_ids[:max(0, len(live_ids) - _LIVE_ARTIFACT_LIMIT + 1)]:
            del ARTIFACTS[old_id]
    meta = {
        "id": artifact_id,
        "filename": filename,
        "path": str(target) if not contains_live_webiq else None,
        "media_type": media_type,
        "url": f"/api/artifacts/{artifact_id}",
        "persisted": not contains_live_webiq,
        "retention_policy": (
            "Memory-only export; expires after 15 minutes or when the server stops"
            if contains_live_webiq
            else "local_sample_report"
        ),
    }
    if contains_live_webiq:
        meta["content"] = content
        meta["expires_at"] = time.monotonic() + _LIVE_ARTIFACT_TTL_SECONDS
    ARTIFACTS[artifact_id] = meta
    return meta


def resolve_artifact(artifact_id: str) -> dict[str, Any] | None:
    meta = ARTIFACTS.get(artifact_id)
    if not meta:
        return None
    if not meta["persisted"]:
        if meta["expires_at"] <= time.monotonic():
            del ARTIFACTS[artifact_id]
            return None
        return meta
    if not Path(meta["path"]).is_file():
        return None
    return meta


def _extract_intent_focus_areas(user_intent: str) -> list[str]:
    intent_lower = user_intent.lower()
    areas: list[str] = []
    keyword_map = [
        (("glasses", "eyewear", "comfort", "clamp", "ear", "flight", "wear"), "Long-Session Comfort & Eyewear Fit"),
        (("anc", "noise", "quiet", "dba", "silent", "fan", "cabin"), "Noise Attenuation / Operating Acoustics (dBA)"),
        (("multipoint", "bluetooth", "switch", "laptop", "macos", "usb-c", "kvm"), "Multi-Device Switching & OS Compatibility"),
        (("text", "coding", "fringing", "ppi", "clarity", "font", "ide"), "Text Clarity & Daily Productivity Readability"),
        (("burn-in", "hinge", "durable", "longevity", "warranty", "build"), "Build Durability, Longevity & Warranty Terms"),
        (("retention", "static", "grind", "espresso", "pour-over", "v60"), "Grind Retention, Cleanliness & Brew Range Fit"),
        (("battery", "hours", "charge"), "Real-World Battery Endurance vs. Claim"),
        (("budget", "price", "$", "value", "worth"), "Price & Budget Alignment"),
    ]
    for keywords, label in keyword_map:
        if any(k in intent_lower for k in keywords):
            areas.append(label)
    if not areas:
        areas.append("Core Performance vs. Seller Claims")
        areas.append("Long-Term Reliability & Value")
    return areas[:5]


def build_agent_plan(
    product_url: str,
    user_intent: str,
    *,
    execution_mode: str = "auto",
    has_live_webiq_key: bool = False,
    use_client_browser: bool = False,
    inspect_review_pages: bool = True,
    community_exploration: bool = False,
    exploration_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Describe the four research stages and separate, simulation-only approval gate."""
    clean_url = validate_public_https_url(product_url, resolve_dns=False)
    mode = (execution_mode or "auto").strip().lower()
    if use_client_browser and mode not in ("client_browser", "vm_playwright"):
        mode = "client_browser"
    labels = {
        "webiq_browse": "WebIQ Browse", "live": "Azure BAT Tool",
        "client_browser": "Local Playwright", "vm_playwright": "Azure VM + Playwright",
    }
    live = mode in labels
    domains = normalize_domains(exploration_domains if exploration_domains is not None else DEFAULT_DOMAINS) if community_exploration else []
    if community_exploration and (not inspect_review_pages or mode not in ("live", "client_browser", "vm_playwright")):
        raise ValueError("Community exploration requires BAT, Local or VM with review inspection enabled.")
    inspector = labels.get(mode, "Sample walkthrough")
    inspection_tool = "WebIQ Browse (/v3/browse)" if mode == "webiq_browse" else inspector
    discovery = ("WebIQ Search (/v3/search/web)"
                 if has_live_webiq_key else "Foundry Toolbox web_search")
    specs = [
        ("digest_product_url", "Inspect the product listing", "product-listing-digestion",
         f"Read {safe_display_url(clean_url)} with {inspector}; distinguish observed identity, offer and seller claims from unknowns.",
         [inspection_tool, f"Foundry Model ({FOUNDRY_MODEL_DEPLOYMENT})"]),
        ("discover_reviews", "Plan and search relevant review sites", "multi-platform-review-discovery",
         f"Plan intent-specific queries with {FOUNDRY_MODEL_DEPLOYMENT}; discover independent labs, editorial reviews and owner communities via {discovery}.",
         [f"Foundry Model ({FOUNDRY_MODEL_DEPLOYMENT})", discovery]),
        ("permitted_source_browsing", "Inspect each eligible discovered page", "multi-platform-review-discovery",
         f"Use the same {inspector} for every unique eligible review URL. Record access restrictions and failures; never substitute another inspector.",
         [inspection_tool]),
        ("synthesize_verdict", "Reconcile claims and explain the decision", "claim-reality-matrix",
         "Separate search excerpts from inspected-page observations. Cite evidence, explain trade-offs and list unverified priorities.",
         [f"Foundry Model ({FOUNDRY_MODEL_DEPLOYMENT})"]),
    ]
    if not inspect_review_pages:
        specs[2] = (
            "permitted_source_browsing", "Skip review-page inspection by choice", "multi-platform-review-discovery",
            "Use returned search grounding for reviews. Do not open discovered review URLs; the initial listing is still inspected.",
            [],
        )
    if community_exploration:
        specs[2] = (
            "permitted_source_browsing", "Inspect results and explore public communities", "multi-platform-review-discovery",
            f"Inspect discovered pages with {inspector}, then use model decisions to search, follow observed links "
            f"and scroll within {', '.join(domains)}. Capture discussion observations and explicit access/budget gaps.",
            [inspection_tool, f"Foundry Model ({FOUNDRY_MODEL_DEPLOYMENT})", discovery],
        )
    steps = [
        {"step": number, "id": key, "title": title, "skill": skill,
         "execution_mode": "Live research" if live else "Illustrative sample; no live page inspection",
         "tools": tools if live else ["Explicitly labelled sample fixtures"],
         "objective": objective if live else "Illustrate this stage without claiming a live execution: " + objective}
        for number, (key, title, skill, objective, tools) in enumerate(specs, start=1)
    ]
    return {
        "product_url": clean_url, "user_intent": user_intent,
        "intent_focus_areas": _extract_intent_focus_areas(user_intent),
        "selected_skills": ["product-listing-digestion", "multi-platform-review-discovery",
                            "claim-reality-matrix", "human-gated-cart-action"],
        "execution_mode": mode if live else "sample",
        "inspect_review_pages": inspect_review_pages,
        "community_exploration": community_exploration,
        "exploration_domains": domains,
        "exploration_limits": LIMITS if community_exploration else None,
        "comparison_method": mode if inspect_review_pages else "search_only",
        "execution_mode_summary": (f"{inspector} | {discovery} | {FOUNDRY_MODEL_DEPLOYMENT}"
                                   + (" | Search-only reviews" if not inspect_review_pages else "")) if live else "Illustrative sample walkthrough",
        "foundry_config": {"project_endpoint": FOUNDRY_PROJECT_ENDPOINT,
                          "model_deployment": FOUNDRY_MODEL_DEPLOYMENT,
                          "toolbox_name": FOUNDRY_TOOLBOX_NAME,
                          "playwright_workspace": PLAYWRIGHT_WORKSPACE_NAME,
                          "tenant_id": FOUNDRY_TENANT_ID},
        "webiq_endpoints": {"web_browse": "POST https://api.microsoft.ai/v3/browse",
                            "web_reviews": "POST https://api.microsoft.ai/v3/search/web",
                            "legal_note": "Attributed excerpts are capped at <=200 characters; publisher limits and access policies apply."},
        "steps": steps,
        "post_research_actions": [
            {"step": 5, "id": "prepare_cart_gate", "title": "Optional guarded cart simulation",
             "skill": "human-gated-cart-action", "execution_mode": "Simulation only",
             "tools": ["CartApprovalTicket"],
             "objective": "Require human approval; never mutate retailer carts or perform checkout."}
        ],
    }


# Curated sample web excerpts (all <= 200 chars per Web IQ FAQ display policy)
_MULTI_PLATFORM_WEB_SOURCES: dict[str, list[dict[str, Any]]] = {
    "xm6": [
        {
            "title": "Sony WH-1000XM6 Headphones Lab Review & Acoustic Measurements",
            "url": "https://www.rtings.com/headphones/reviews/sony/wh-1000xm6-wireless",
            "published_at": "2026-07-18T12:00:00Z",
            "updated_at": "2026-08-20T09:30:00Z",
            "crawled_at": "2026-09-02T04:15:00Z",
            "content": (
                "Sample Excerpt: Measured 31.2 dB bass ANC attenuation and 24.1 hr battery with LDAC + Multipoint. "
                "Thin wire glasses lose 1.1 dB seal; thick 5mm acetate frames lose 4.3 dB."
            ),
        },
        {
            "title": "Sony WH-1000XM6 Long-Term Review: Hinge Redesign & Multipoint",
            "url": "https://www.soundguys.com/sony-wh-1000xm6-review-124890/",
            "published_at": "2026-07-25T15:00:00Z",
            "updated_at": "2026-08-10T11:00:00Z",
            "crawled_at": "2026-09-02T04:16:00Z",
            "content": (
                "Sample Excerpt: Internal stainless sleeve reinforces the folding hinge. Bluetooth 5.4 multipoint "
                "switches cleanly between Windows 11 and iOS; 990 kbps LDAC can stutter in busy Wi-Fi."
            ),
        },
        {
            "title": "WH-1000XM6 Owners Thread: Glasses Comfort & Ear Pad Depth",
            "url": "https://www.head-fi.org/threads/sony-wh-1000xm6-owners-impressions-and-measurements.974120/",
            "published_at": "2026-08-03T19:45:00Z",
            "updated_at": "2026-08-28T21:10:00Z",
            "crawled_at": "2026-09-02T04:18:00Z",
            "content": (
                "Sample Excerpt: Cup depth is 3mm deeper than XM5. Thin titanium/wire glasses stay comfortable "
                "on 8-10 hr flights; thick acetate temples cause pressure after ~3 hours."
            ),
        },
        {
            "title": "Best Noise-Canceling Headphones for Frequent Flyers",
            "url": "https://www.nytimes.com/wirecutter/reviews/best-noise-cancelling-headphones/",
            "published_at": "2026-08-12T10:00:00Z",
            "updated_at": None,
            "crawled_at": "2026-09-02T04:20:00Z",
            "content": (
                "Sample Excerpt: Strongest cabin rumble reduction and clearest multipoint handoff across our "
                "flight panel, though Bose QC Ultra remains slightly softer on thick glasses frames."
            ),
        },
        {
            "title": "6 Months Commuting & Flying with WH-1000XM6 — Owner Notes",
            "url": "https://www.reddit.com/r/headphones/comments/1m9xm6p/6_months_with_sony_wh1000xm6_flights_glasses/",
            "published_at": "2026-08-29T14:22:00Z",
            "updated_at": None,
            "crawled_at": "2026-09-02T04:22:00Z",
            "content": (
                "Sample Excerpt: Zero hinge cracks after 6 months of daily folding. Expect ~24 hours battery "
                "with laptop + phone multipoint rather than the 30-hour single-device rating."
            ),
        },
    ],
    "pg32ucdm": [
        {
            "title": "ASUS ROG Swift OLED PG32UCDM Review: Text Clarity & Calibration",
            "url": "https://www.rtings.com/monitor/reviews/asus/rog-swift-oled-pg32ucdm",
            "published_at": "2026-06-14T13:00:00Z",
            "updated_at": "2026-08-19T16:00:00Z",
            "crawled_at": "2026-09-01T08:00:00Z",
            "content": (
                "Sample Excerpt: 140 PPI 3rd-gen 4K QD-OLED scores 8.5/10 for text clarity with minimal fringing. "
                "Polarizer-free panel shows slight magenta black raise above 250 lux room light."
            ),
        },
        {
            "title": "ASUS ROG Swift PG32UCDM Thermal & Firmware Analysis",
            "url": "https://tftcentral.co.uk/reviews/asus-rog-swift-oled-pg32ucdm",
            "published_at": "2026-06-20T09:00:00Z",
            "updated_at": "2026-08-01T12:00:00Z",
            "crawled_at": "2026-09-01T08:05:00Z",
            "content": (
                "Sample Excerpt: Passive graphene heatsink runs at 0 dBA (no cooling fan). Firmware MCM105 "
                "stabilizes USB-C 90W PD wake-from-sleep with Apple Silicon MacBook Pros."
            ),
        },
        {
            "title": "PG32UCDM with MacBook Pro for 9hr/day Xcode & VS Code",
            "url": "https://forums.macrumors.com/threads/asus-pg32ucdm-4k-qd-oled-for-macos-coding-long-term.2431980/",
            "published_at": "2026-07-11T17:30:00Z",
            "updated_at": "2026-08-25T20:15:00Z",
            "crawled_at": "2026-09-01T08:10:00Z",
            "content": (
                "Sample Excerpt: Crisp macOS 2560x1440 HiDPI scaling over single USB-C cable (90W PD + KVM). "
                "Position room lamps behind the screen to avoid QD-OLED ambient black tint."
            ),
        },
        {
            "title": "QD-OLED Monitor Productivity & Thermal Benchmark",
            "url": "https://www.notebookcheck.net/ASUS-ROG-Swift-OLED-PG32UCDM-Monitor-Review.841200.0.html",
            "published_at": "2026-07-04T11:00:00Z",
            "updated_at": None,
            "crawled_at": "2026-09-01T08:12:00Z",
            "content": (
                "Sample Excerpt: Peak panel temperature reaches 38.4°C with silent fanless cooling, backed by "
                "a 3-year ASUS warranty that explicitly includes OLED burn-in coverage."
            ),
        },
    ],
    "ode": [
        {
            "title": "Fellow Ode Gen 2 Owner Thread: Retention & Ionizer Cleaning",
            "url": "https://www.home-barista.com/grinders/fellow-ode-gen-2-long-term-owner-experience-t89412.html",
            "published_at": "2026-05-19T14:00:00Z",
            "updated_at": "2026-08-15T18:40:00Z",
            "crawled_at": "2026-09-01T09:00:00Z",
            "content": (
                "Sample Excerpt: Gen 2 64mm brew burrs easily handle light-roast V60 and AeroPress. Wipe the "
                "exit-chute ionizer needles every 2-3 weeks to keep static retention under 0.1g."
            ),
        },
        {
            "title": "The Best Coffee Grinder for Pour-Over and Filter Coffee",
            "url": "https://www.nytimes.com/wirecutter/reviews/best-coffee-grinder/",
            "published_at": "2026-07-08T10:30:00Z",
            "updated_at": None,
            "crawled_at": "2026-09-01T09:05:00Z",
            "content": (
                "Sample Excerpt: Top pick for filter coffee: grinds 20g in under 5 seconds at ~69 dBA with "
                "minimal counter chaff, but it is not designed for espresso."
            ),
        },
        {
            "title": "Fellow Ode Gen 2 1-Year Review: Particle Uniformity & Workflow",
            "url": "https://www.coffeegeek.com/reviews/fellow-ode-gen-2-brew-grinder-review/",
            "published_at": "2026-06-28T16:15:00Z",
            "updated_at": None,
            "crawled_at": "2026-09-01T09:08:00Z",
            "content": (
                "Sample Excerpt: Measured 0.08g average retention across 100 doses. Quiet PID motor tone "
                "suits morning apartments; grind range (320-1100 µm) is strictly for filter brew."
            ),
        },
        {
            "title": "Fellow Products Customer Service & Warranty Experiences",
            "url": "https://www.trustpilot.com/review/fellowproducts.com",
            "published_at": "2026-08-11T12:00:00Z",
            "updated_at": None,
            "crawled_at": "2026-09-01T09:10:00Z",
            "content": (
                "Sample Excerpt: Responsive 2-year warranty support and quick replacement parts shipping "
                "when owners register their grinder serial number."
            ),
        },
    ],
}



def _match_scenario_family(baseline: ProductBaseline, user_intent: str) -> str | None:
    if baseline.is_custom_url:
        return None
    text = f"{baseline.brand} {baseline.model} {baseline.sku}".lower()
    if any(k in text for k in ("xm6", "1000xm6", "wh-1000", "sony")):
        return "xm6"
    if any(k in text for k in ("pg32ucdm", "qd-oled", "rog swift")):
        return "pg32ucdm"
    if any(k in text for k in ("ode", "fellow")):
        return "ode"
    return None


def _build_local_enrichment(
    baseline: ProductBaseline,
    user_intent: str,
    web_query: str,
) -> Enrichment:
    """Return curated sample fixtures for the 3 presets, or an empty unverified enrichment for custom URLs."""
    family = _match_scenario_family(baseline, user_intent)
    if family and family in _MULTI_PLATFORM_WEB_SOURCES:
        web_items = [
            {
                "title": s["title"],
                "url": s["url"],
                "content": s["content"],
                "publishedAt": s["published_at"],
                "lastUpdatedAt": s["updated_at"],
                "crawledAt": s["crawled_at"],
            }
            for s in _MULTI_PLATFORM_WEB_SOURCES[family]
        ]
        base_enrichment = normalize_response(
            {"webResults": web_items}, web_query, "", provenance="sample_fixture"
        )
        return Enrichment(
            query=base_enrichment.query,
            retrieved_at=base_enrichment.retrieved_at,
            sources=base_enrichment.sources,
            provenance="sample_fixture",
        )

    # Custom URL without live Web IQ key: do NOT invent fake RTINGS/Trustpilot/YouTube citations!
    return Enrichment(
        query=web_query,
        retrieved_at="",
        sources=(),
        provenance="illustrative_custom_url",
    )


def _synthesize_claim_matrix_and_signals(
    baseline: ProductBaseline,
    user_intent: str,
    enrichment: Enrichment,
    *,
    run_mode: str,
) -> dict[str, Any]:
    """Synthesize decision-first findings, watch-outs, source-attributed claim checks, and evidence quality signals."""
    family = _match_scenario_family(baseline, user_intent) if enrichment.provenance == "sample_fixture" else None
    src_by_platform = {s.platform: s for s in enrichment.sources}
    first_src_url = enrichment.sources[0].url if enrichment.sources else baseline.canonical_url

    def _src_link(platform_prefix: str, fallback_label: str = "Source") -> dict[str, str]:
        for plat, src in src_by_platform.items():
            if platform_prefix.lower() in plat.lower():
                return {"label": plat, "url": src.url, "provenance": src.provenance}
        return {"label": fallback_label, "url": first_src_url, "provenance": enrichment.provenance}

    if family == "xm6":
        what_fits = [
            "Strong low-frequency cabin noise reduction (31.2 dB in sample lab fixture) well suited for 10-hour flights.",
            "Comfortable seal with thin wire/titanium prescription glasses (~1.1 dB leakage vs 4.3 dB on thick acetate frames).",
            "Reliable Windows 11 + iPhone Bluetooth 5.4 multipoint switching (~1.4s call handoff) and metal-reinforced folding hinge.",
            "Sample price ($399.99) sits $50 below your stated $450 budget ceiling.",
        ]
        watch_outs = [
            "Battery life with LDAC + two-device Multipoint + ANC is ~24 hours in sample testing, below the 30-hour headline claim.",
            "Thick acetate glasses frames (>4-5mm temples) break the lower ear-pad seal and cause temple pressure after ~3 hours.",
            "High-bitrate LDAC (990 kbps) can occasionally stutter when multipoint is active in congested Wi-Fi offices.",
        ]
        matrix = [
            {
                "seller_claim": "Up to 30-hour battery life with Active Noise Cancellation enabled",
                "verdict": "Optimistic Headline Claim (~24h with Multipoint)",
                "Cross_platform_evidence": "Sample RTINGS (24.1h) and owner fixtures show ~24 hours when Multipoint + LDAC are active; 30h requires single-device AAC.",
                "user_intent_impact": "Still covers two 10-hour flights, but expect ~24h rather than 30h with laptop + iPhone connected.",
                "source_attribution": _src_link("RTINGS", "RTINGS Sample"),
                "provenance": enrichment.provenance,
            },
            {
                "seller_claim": "Dual-Processor V2 + QN3 Active Noise Cancellation",
                "verdict": "Supported in Sample Fixtures",
                "Cross_platform_evidence": "Sample RTINGS (31.2 dB bass attenuation) and Wirecutter fixtures rank it top-tier for cabin rumble.",
                "user_intent_impact": "Directly matches your 10-hour international flight quietness priority.",
                "source_attribution": _src_link("Wirecutter", "Wirecutter Sample"),
                "provenance": enrichment.provenance,
            },
            {
                "seller_claim": "All-day pressure-free comfort for eyewear and glasses users",
                "verdict": "Context-Dependent (Thin Frames Yes; Thick Frames No)",
                "Cross_platform_evidence": "Sample Head-Fi and RTINGS fixtures confirm thin metal frames lose only 1.1 dB seal, while thick 5mm acetate frames lose 4.3 dB.",
                "user_intent_impact": "Fits your thin prescription glasses; avoid thick acetate frames on long flights.",
                "source_attribution": _src_link("Head-Fi", "Head-Fi Sample"),
                "provenance": enrichment.provenance,
            },
            {
                "seller_claim": "Seamless dual-device Bluetooth 5.4 Multipoint & reinforced hinge",
                "verdict": "Supported in Sample Fixtures",
                "Cross_platform_evidence": "Sample SoundGuys and Reddit fixtures note an internal metal hinge sleeve and reliable Windows 11 + iPhone handoff.",
                "user_intent_impact": "Addresses both your Windows + iPhone workflow and folding hinge durability priority.",
                "source_attribution": _src_link("SoundGuys", "SoundGuys Sample"),
                "provenance": enrichment.provenance,
            },
        ]
        badge = "Sample Walkthrough: Fits Stated Needs (With Caveats)"
        fit_label = "Sample Fit Assessment (Qualitative — Demo Data)"
        summary = (
            "In this curated sample walkthrough, the Sony WH-1000XM6 ($399.99 sample offer vs. your $450 ceiling) "
            "aligns well with thin prescription glasses, 10-hour flight ANC, Windows + iPhone multipoint, and hinge reinforcement. "
            "Key watch-out: expect ~24 hours of battery with Multipoint active rather than the 30-hour headline rating."
        )
    elif family == "pg32ucdm":
        what_fits = [
            "140 PPI 3rd-gen 4K QD-OLED density makes macOS HiDPI coding text crisp at normal viewing distance.",
            "Passive graphene heatsink is completely fanless (0 dBA) for silent daytime work.",
            "Single-cable USB-C 90W Power Delivery + built-in KVM and 3-year OLED burn-in warranty.",
        ]
        watch_outs = [
            "Polarizer-free QD-OLED panel shows a magenta-grey black-level raise when bright room light (>250 lux) hits the screen directly.",
            "Requires updating to firmware MCM105+ for reliable USB-C wake-from-sleep on Apple Silicon Macs.",
        ]
        matrix = [
            {
                "seller_claim": "3rd-Gen 4K QD-OLED 140 PPI subpixel layout delivers sharp desktop text clarity",
                "verdict": "Supported in Sample Fixtures",
                "Cross_platform_evidence": "Sample RTINGS (8.5/10 text clarity) and MacRumors fixtures report crisp macOS HiDPI coding text.",
                "user_intent_impact": "Addresses 1440p OLED text fringing concerns for 9 hr/day coding.",
                "source_attribution": _src_link("RTINGS", "RTINGS Sample"),
                "provenance": enrichment.provenance,
            },
            {
                "seller_claim": "Infinite contrast ratio with deepest true blacks in any room environment",
                "verdict": "Context-Dependent (Sensitive to Bright Direct Room Light)",
                "Cross_platform_evidence": "Sample RTINGS and MacRumors fixtures note ambient light >250 lux raises black levels due to lack of a polarizer.",
                "user_intent_impact": "Important for your bright-room coding setup: keep lamps/windows behind the display.",
                "source_attribution": _src_link("MacRumors", "MacRumors Sample"),
                "provenance": enrichment.provenance,
            },
            {
                "seller_claim": "Custom fanless graphene heatsink + 3-year OLED burn-in warranty + 90W USB-C KVM",
                "verdict": "Supported in Sample Fixtures",
                "Cross_platform_evidence": "Sample TFTCentral and Notebookcheck fixtures confirm 0 dBA passive cooling and 3-year burn-in coverage.",
                "user_intent_impact": "Meets silent operation, MacBook USB-C charging, and burn-in warranty needs.",
                "source_attribution": _src_link("TFTCentral", "TFTCentral Sample"),
                "provenance": enrichment.provenance,
            },
        ]
        badge = "Sample Walkthrough: Fits Stated Needs (Bright-Room Caveat)"
        fit_label = "Sample Fit Assessment (Qualitative — Demo Data)"
        summary = (
            "In this curated sample walkthrough, the ASUS PG32UCDM ($1,199.00 sample offer) delivers crisp 140 PPI "
            "macOS text clarity, 0 dBA fanless cooling, 90W USB-C PD, and a 3-year burn-in warranty. "
            "Key watch-out for your bright room: avoid direct light hitting the front of the screen to prevent QD-OLED magenta black raise."
        )
    elif family == "ode":
        what_fits = [
            "Low static retention (~0.08g average in sample fixtures) keeps apartment counters clean without water spritzing.",
            "Quiet (~69.4 dBA), fast (<5s for 20g) PID motor with auto-stop fits early-morning V60 and AeroPress routines.",
            "Sample price ($345.00) is within your $360 budget ceiling.",
        ]
        watch_outs = [
            "Strictly a filter/brew grinder (~320 µm finest floor) — it CANNOT grind fine enough for 9-bar espresso.",
            "Exit-chute anti-static ionizer needles require a quick brush wipe every 2–3 weeks or static chaff returns.",
        ]
        matrix = [
            {
                "seller_claim": "Anti-static ionizer technology for <0.1g single-dose retention",
                "verdict": "Supported (Requires Biweekly Brush Wipe)",
                "Cross_platform_evidence": "Sample CoffeeGeek (0.08g avg) and Home-Barista fixtures confirm <0.1g retention when ionizer pins are kept clean.",
                "user_intent_impact": "Delivers clean single-dose morning workflow with simple biweekly maintenance.",
                "source_attribution": _src_link("Home-Barista", "Home-Barista Sample"),
                "provenance": enrichment.provenance,
            },
            {
                "seller_claim": "Whisper-quiet cafe-grade grinding with automatic shutoff",
                "verdict": "Supported in Sample Fixtures",
                "Cross_platform_evidence": "Sample Wirecutter and CoffeeGeek fixtures note a low-pitched motor that finishes 20g in under 5 seconds.",
                "user_intent_impact": "Fits quiet apartment mornings well.",
                "source_attribution": _src_link("Wirecutter", "Wirecutter Sample"),
                "provenance": enrichment.provenance,
            },
            {
                "seller_claim": "Full-range precision for every morning brewed coffee style",
                "verdict": "Filter/Pour-Over Only — Cannot Grind for Espresso",
                "Cross_platform_evidence": "Sample Home-Barista and Wirecutter fixtures confirm Gen 2 burrs excel at V60/AeroPress but cannot grind for espresso.",
                "user_intent_impact": "Directly answers your espresso question: great for V60/AeroPress, but do not buy if you need espresso.",
                "source_attribution": _src_link("CoffeeGeek", "CoffeeGeek Sample"),
                "provenance": enrichment.provenance,
            },
        ]
        badge = "Sample Walkthrough: Great for V60/AeroPress (No Espresso)"
        fit_label = "Sample Fit Assessment (Qualitative — Demo Data)"
        summary = (
            "In this curated sample walkthrough, the Fellow Ode Gen 2 ($345.00 sample offer vs. your $360 ceiling) "
            "excels at quiet (69.4 dBA), low-retention (0.08g) V60 pour-over and AeroPress grinding. "
            "Critical caveat for your question: it cannot grind fine enough for traditional espresso."
        )
    else:
        # Unverified Custom URL path — never masquerade guessed specs or fake citations as research!
        has_live_webiq = enrichment.provenance == "live_webiq"
        what_fits = [
            f"Prepared a structured verification checklist tailored to your priorities: \"{user_intent[:110]}\".",
            (
                f"Retrieved {len(enrichment.sources)} live Web IQ search excerpt(s) (capped at <=200 chars)."
                if has_live_webiq
                else "No fake prices, SKUs, or unverified product claims were fabricated for this custom URL."
            ),
        ]
        watch_outs = [
            "Live Browser Automation is not connected in this prototype, so your product URL was not fetched or scraped.",
            (
                "Sample mode does not verify third-party claims against inspected pages."
                if not has_live_webiq
                else "Retrieved excerpts have not been verified against this listing. No model has checked or endorsed their claims."
            ),
        ]
        matrix = []
        if has_live_webiq:
            for idx, src in enumerate(enrichment.sources[:4], start=1):
                matrix.append(
                    {
                        "seller_claim": f"Priority check #{idx} ({src.platform})",
                        "verdict": "Live Search Excerpt Retrieved (Manual Review Recommended)",
                        "Cross_platform_evidence": truncate_display_excerpt(src.content, MAX_DISPLAY_EXCERPT_CHARS),
                        "user_intent_impact": f"Review full source on {src.platform} against your criteria.",
                        "source_attribution": {"label": src.platform, "url": src.url, "provenance": "live_webiq"},
                        "provenance": "live_webiq",
                    }
                )
        else:
            for claim in baseline.seller_claims:
                matrix.append(
                    {
                        "seller_claim": claim,
                        "verdict": "Unverified in Sample Mode (Requires Live Inspection)",
                        "Cross_platform_evidence": "Not fetched — custom URLs are not scraped or guessed in Sample Mode.",
                        "user_intent_impact": "Inspect the retailer listing and independent lab reviews directly before purchasing.",
                        "source_attribution": {
                            "label": "Open Product URL",
                            "url": baseline.canonical_url,
                            "provenance": "illustrative_custom_url",
                        },
                        "provenance": "illustrative_custom_url",
                    }
                )
        badge = (
            "Live Search Only — Product Claims Not Verified"
            if has_live_webiq
            else "Unverified Custom URL — Checklist Only (Sample Mode)"
        )
        fit_label = "Custom URL Status (No Fabricated Score)"
        summary = (
            f"Custom URL Notice: This sample run does not fetch the listing or call a synthesis model. "
            f"Rather than guessing specs or fabricating reviews for {safe_display_url(baseline.canonical_url)}, "
            f"this page shows the verification checklist for your stated needs"
            + (f" alongside {len(enrichment.sources)} live Web IQ search excerpt(s)." if has_live_webiq else ".")
        )
        if has_live_webiq:
            summary = (
                f"Retrieved {len(enrichment.sources)} web excerpt(s). "
                "The product listing was not fetched and no model synthesized a buying recommendation. "
                "Sample measurements are not used as evidence for these live links."
            )
            fit_label = "Search results for manual review, not a recommendation"
            what_fits = [f"Search was guided by your priorities: {user_intent[:110]}."]

    if family is not None:
        summary = (
            "Illustrative walkthrough only: the findings and budget comparisons are prewritten for the sample scenario, "
            "not verified facts or a personalized assessment of edited priorities. " + summary
        )
    platforms_used = sorted({s.platform for s in enrichment.sources})
    is_preset_sample = family is not None
    signals = [
        {
            "signal": "Source Diversity Coverage",
            "status": f"{len(platforms_used)} Platforms (Sample Fixture)" if is_preset_sample else (f"{len(platforms_used)} Live Search Platforms" if platforms_used else "Not Fetched (Sample Mode)"),
            "detail": (
                f"Covers {', '.join(platforms_used)}. Indicator of cross-source variety, not proof that external posts are genuine."
                if platforms_used
                else "No external review platforms were queried for this custom URL in Sample Mode."
            ),
        },
        {
            "signal": "Long-Term Use Coverage",
            "status": "Included in Sample Fixtures" if is_preset_sample else "Verify Manually",
            "detail": "Checks whether multi-month ownership notes are present alongside day-one impressions (coverage indicator only).",
        },
        {
            "signal": "Model & Variant Match Check",
            "status": f"Sample SKU ({baseline.sku})" if is_preset_sample else "Unverified Custom URL",
            "detail": "Checks whether cited reviews refer to the same model/generation rather than an older variant.",
        },
        {
            "signal": "Sponsorship & Unit Provenance Note",
            "status": "Sample Fixture Metadata" if is_preset_sample else "Unverified",
            "detail": "Notes stated review-unit provenance in sample data; cannot independently verify reviewer financial disclosures.",
        },
        {
            "signal": "Recurring Caveat Clustering",
            "status": "Caveats Highlighted Above" if is_preset_sample else "Checklist Only",
            "detail": "Surfaces repeated trade-offs (e.g., multipoint battery dip, bright-room black raise, filter-only burrs) before purchase.",
        },
    ]

    return {
        "fit_score": None,
        "fit_label": fit_label,
        "recommendation_badge": badge,
        "executive_summary": summary,
        "what_fits": what_fits,
        "watch_outs": watch_outs,
        "platforms_covered": platforms_used,
        "claim_matrix": matrix,
        "authenticity_signals": signals,
        "evidence_quality_disclaimer": (
            "These indicators summarize coverage breadth and consistency in the loaded data "
            "(sample fixtures or search excerpts). They are not cryptographic or forensic proof that third-party reviews are genuine."
        ),
        "run_mode": run_mode,
        "is_custom_url": baseline.is_custom_url,
    }


def _format_markdown_report(
    *,
    plan: dict[str, Any],
    baseline: ProductBaseline,
    enrichment: Enrichment,
    synthesis: dict[str, Any],
) -> str:
    """Render a transparent Markdown report with <=200-char excerpts, source links, and provenance labels."""
    if baseline.price is not None:
        price_str = f"${baseline.price:.2f} {baseline.currency}"
    elif baseline.data_provenance.startswith("live_bat"):
        price_str = "Not visible on inspected page (not guessed)"
    else:
        price_str = "Unverified (Custom URL in Sample Mode)"
    lines: list[str] = [
        f"# Product Due-Diligence Summary: {baseline.brand} {baseline.model}",
        "",
        f"> **Mode & Provenance:** `{synthesis['run_mode']}` — {baseline.limitation_notice or DISCLAIMER}",
        "",
        f"**Assessment:** **{synthesis['recommendation_badge']}**  ",
        f"**Product URL:** {baseline.canonical_url}  ",
        f"**SKU / Variant:** `{baseline.sku}` — {baseline.variant}  ",
        f"**Observed / Sample Offer:** **{price_str}** via *{baseline.seller}*  ",
        f"**What Matters to You:** > *{plan['user_intent']}*",
        "",
        "## Executive Summary",
        synthesis["executive_summary"],
        "",
        "### What Fits Your Needs",
    ]
    for item in synthesis["what_fits"]:
        lines.append(f"- ✅ {item}")

    lines.extend(["", "### Key Risks & Watch-Outs Before Buying"])
    for item in synthesis["watch_outs"]:
        lines.append(f"- ⚠️ {item}")

    evidence_gaps = synthesis.get("evidence_gaps") or []
    if evidence_gaps:
        lines.extend(["", "### Explicit Evidence Gaps & Unverified Items"])
        for gap in evidence_gaps:
            lines.append(f"- 🔍 {gap}")

    lines.extend(
        [
            "",
            "---",
            "",
            "## 1. Claim-vs-Reality Checks (With Source Attribution)",
            "",
            "| Seller Claim / Check | Assessment | Multi-Platform Evidence (<=200 chars) | Source Link |",
            "| :--- | :--- | :--- | :--- |",
        ]
    )
    for row in synthesis["claim_matrix"]:
        attr = row.get("source_attribution") or {}
        link_md = f"[{attr.get('label', 'Source')}]({attr.get('url', baseline.canonical_url)})"
        lines.append(
            f"| **{row['seller_claim']}** | `{row['verdict']}` | {row['Cross_platform_evidence']} | {link_md} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 2. Multi-Platform Review Attribution",
            "",
        ]
    )
    if enrichment.sources:
        lines.append("| Platform | Category | Provenance | Source |")
        lines.append("| :--- | :--- | :--- | :--- |")
        for src in enrichment.sources:
            lines.append(
                f"| **{src.platform}** | {src.platform_category} | `{src.provenance}` | [{src.title}]({src.url}) |"
            )
    elif enrichment.provenance == "unavailable_missing_key":
        lines.append("*External review discovery unavailable — no Web IQ API key was supplied in the portal (listing-only partial run).*")
    else:
        lines.append("*No external review sources were fetched for this custom URL in Sample Mode.*")

    browsed_sources = synthesis.get("browsed_sources") or []
    if browsed_sources:
        lines.extend(
            [
                "",
                "### Selected-Method Page Inspection Summary",
                "",
                "| Platform | Status | Summary (<=200 chars) |",
                "| :--- | :--- | :--- |",
            ]
        )
        for bsrc in browsed_sources:
            summary = (
                "Grounding used during the workflow; raw text is not retained."
                if bsrc.get("inspection_method") == "webiq_browse"
                else bsrc.get("extracted_summary", "")
            )
            lines.append(
                f"| **[{bsrc.get('platform', 'Source')}]({bsrc.get('url', '')})** | `{bsrc.get('access_status', 'unknown')}` | {summary} |"
            )
    if not plan.get("inspect_review_pages", True):
        lines.append("\nReview-page inspection was intentionally skipped; reviews use search grounding, not page visits.")
    if plan.get("community_exploration"):
        lines.extend([
            "", "### Optional Community Exploration",
            f"Configured domains: {', '.join(plan.get('exploration_domains') or [])}. General discovery was not restricted.",
            "Model-chosen actions and bounded browser observations appear in the portal timeline. "
            "Discussion blocks are unverified assertions, not authenticated owner feedback.",
        ])
        for source in browsed_sources:
            if source.get("agent_explored"):
                lines.append(
                    f"- [{source.get('title') or source.get('platform')}]({source.get('url')}): "
                    f"{source.get('discovery_origin')}; {source.get('discussion_blocks_observed', 0)} "
                    "discussion block(s) observed."
                )



    lines.extend(
        [
            "---",
            "",
            "## 3. Evidence Quality & Coverage Signals",
            f"*{synthesis['evidence_quality_disclaimer']}*",
            "",
            "| Signal | Status | Coverage Note |",
            "| :--- | :--- | :--- |",
        ]
    )
    for sig in synthesis["authenticity_signals"]:
        lines.append(f"| **{sig['signal']}** | `{sig['status']}` | {sig['detail']} |")

    lines.extend(["", "---", "", f"*{DISCLAIMER}*"])
    return "\n".join(lines)


async def _run_phase1_live_web_research(
    *,
    run_id: str,
    product_url: str,
    user_intent: str,
    scenario: str | None,
    webiq_key: str,
    webiq_query: str | None,
    sse: Any,
    use_client_browser: bool = False,
    bridge_target: str = "extension",
    is_webiq_browse: bool = False,
    inspect_review_pages: bool = True,
    community_exploration: bool = False,
    exploration_domains: list[str] | None = None,
) -> AsyncIterator[str]:
    """Execute site research without hidden inspector or sample fallbacks."""
    has_live_key = bool(webiq_key and webiq_key.strip())
    run_mode = "live_phase1_full" if has_live_key else "live_bat_listing_only"
    if not inspect_review_pages:
        run_mode = "live_search_only_reviews"
    if is_webiq_browse:
        browser_label = "WebIQ Browse (/v3/browse)"
        effective_exec_mode = "webiq_browse"
    elif use_client_browser:
        browser_label = (
            "Windows VM Native Chrome + Playwright Worker"
            if bridge_target == "vm_worker"
            else "Chrome Extension (Your Browser)"
        )
        effective_exec_mode = "vm_playwright" if bridge_target == "vm_worker" else "client_browser"
    else:
        browser_label = f"Foundry BAT ({PLAYWRIGHT_WORKSPACE_NAME})"
        effective_exec_mode = "live"

    yield sse({
        "type": "status",
        "stage": "start",
        "run_id": run_id,
        "scenario": scenario or "live",
        "run_mode": run_mode,
        "execution_mode": effective_exec_mode,
        "use_client_browser": use_client_browser,
        "bridge_target": "webiq_browse" if is_webiq_browse else (bridge_target if use_client_browser else "cloud_bat"),
        "capabilities": CAPABILITY_STATUS,
        "toolbox": FOUNDRY_TOOLBOX_NAME,
    })
    yield sse({
        "type": "agent_start",
        "agent": "pdd-shopping-agent",
        "role": "scenario",
        "label": (
            f"Product Due-Diligence Assistant ({browser_label} + Live Site Research)"
            if (use_client_browser or is_webiq_browse)
            else "Product Due-Diligence Assistant (Phase-1 Live Web Research)"
        ),
        "run_mode": run_mode,
    })

    plan = build_agent_plan(
        product_url,
        user_intent,
        execution_mode=effective_exec_mode,
        has_live_webiq_key=has_live_key,
        use_client_browser=use_client_browser,
        inspect_review_pages=inspect_review_pages,
        community_exploration=community_exploration,
        exploration_domains=exploration_domains,
    )
    yield sse({"type": "plan", "agent": "pdd-shopping-agent", "plan": plan})
    yield sse({
        "type": "activity",
        "agent": "pdd-shopping-agent",
        "kind": "plan",
        "provenance": run_mode,
        "label": f"Built {len(plan['steps'])}-step Live Research plan ({FOUNDRY_MODEL_DEPLOYMENT} + {browser_label})",
        "detail": " • ".join(plan["intent_focus_areas"]),
    })

    # Stage 1: Real BAT / Chrome Extension / VM Playwright / WebIQ /v3/browse Product Listing Inspection
    s1_md = load_skill_markdown("product-listing-digestion")
    yield sse({
        "type": "skill_loaded",
        "agent": "pdd-shopping-agent",
        "skill": "product-listing-digestion",
        "title": SKILLS_METADATA["product-listing-digestion"]["title"],
        "chars": len(s1_md),
    })
    yield sse({
        "type": "status",
        "stage": "digest_product_url",
        "run_id": run_id,
        "scenario": scenario or "live",
        "run_mode": run_mode,
    })

    bat_available = False
    foundry_blocker_msg: str | None = None
    try:
        if is_webiq_browse:
            if not has_live_key:
                raise FoundryResearchError(
                    "WebIQ Browse mode (/v3/browse) requires a WebIQ API key in the portal input field.",
                    code="webiq_browse_missing_key",
                    status_code=400,
                )
            baseline, _bat_meta = await inspect_product_listing_webiq_browse(
                run_id=run_id,
                product_url=product_url,
                user_intent=user_intent,
                webiq_key=webiq_key.strip(),
            )
        else:
            stage1_q: asyncio.Queue[str] = asyncio.Queue()

            async def _emit_stage1(payload: dict[str, Any]) -> None:
                await stage1_q.put(sse(payload))

            stage1_task = asyncio.create_task(
                inspect_product_listing_live(
                    run_id=run_id,
                    product_url=product_url,
                    user_intent=user_intent,
                    use_client_browser=use_client_browser,
                    bridge_target=bridge_target,
                    emit_sse=_emit_stage1,
                )
            )
            try:
                while not stage1_task.done():
                    get_t = asyncio.create_task(stage1_q.get())
                    done_set, _ = await asyncio.wait(
                        {stage1_task, get_t},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if get_t in done_set:
                        yield get_t.result()
                    else:
                        get_t.cancel()
                while not stage1_q.empty():
                    yield stage1_q.get_nowait()
                baseline, _bat_meta = await stage1_task
            finally:
                if not stage1_task.done():
                    stage1_task.cancel()

        bat_available = True
        for shot in baseline.screenshots:
            yield sse({"type": "screenshot", "agent": "pdd-shopping-agent", "screenshot": shot})
        yield sse({
            "type": "product_baseline",
            "agent": "pdd-shopping-agent",
            "baseline": baseline.to_dict(),
        })
        price_note = f"${baseline.price:.2f}" if baseline.price is not None else "price not visible"
        listing_activity_label = (
            f"Retailer site restriction / non-indexed page via {browser_label}: "
            f"{baseline.model} — continuing with GPT query planning & review discovery"
            if baseline.data_provenance in ("live_bat_site_restricted", "live_webiq_browse_restricted")
            else f"Inspected listing via {browser_label}: {baseline.brand} {baseline.model} ({price_note})"
        )
        yield sse({
            "type": "activity",
            "agent": "pdd-shopping-agent",
            "kind": "listing_baseline",
            "provenance": baseline.data_provenance,
            "label": listing_activity_label,
            "detail": baseline.limitation_notice or "",
        })
    except FoundryResearchError as exc:
        foundry_blocker_msg = str(exc)
        yield sse({
            "type": "activity",
            "agent": "pdd-shopping-agent",
            "kind": "foundry_access_notice",
            "provenance": exc.code,
            "label": f"{browser_label} listing inspection blocked ({exc.code})",
            "detail": foundry_blocker_msg,
        })
        if not has_live_key:
            yield sse({
                "type": "error",
                "agent": "pdd-shopping-agent",
                "code": exc.code,
                "status_code": exc.status_code,
                "message": foundry_blocker_msg,
            })
            yield sse({"type": "done", "outcome": "error", "run_mode": "live_error"})
            return

        run_mode = "live_webiq_only_foundry_blocked"
        baseline = build_unverified_seed_baseline(
            product_url,
            user_intent,
            limitation_notice=foundry_blocker_msg,
        )
        yield sse({
            "type": "product_baseline",
            "agent": "pdd-shopping-agent",
            "baseline": baseline.to_dict(),
        })

    # Stage 2: Model-guided review query planning
    s2_md = load_skill_markdown("multi-platform-review-discovery")
    yield sse({
        "type": "skill_loaded",
        "agent": "pdd-shopping-agent",
        "skill": "multi-platform-review-discovery",
        "title": SKILLS_METADATA["multi-platform-review-discovery"]["title"],
        "chars": len(s2_md),
    })
    yield sse({
        "type": "status",
        "stage": "webiq_search",
        "run_id": run_id,
        "scenario": scenario or "live",
        "run_mode": run_mode,
    })

    query_plan = await plan_review_queries_with_gpt(baseline=baseline, user_intent=user_intent)

    planned_primary_query = str(
        query_plan.get("primary_webiq_query")
        or query_plan.get("primary_web_query")
        or f"{baseline.brand} {baseline.model} review {user_intent[:80]}"
    )
    supp_list = query_plan.get("supplementary_queries") or []
    planned_secondary_query = str(
        query_plan.get("secondary_web_query")
        or (supp_list[0] if isinstance(supp_list, list) and supp_list else "")
    ).strip() or None

    yield sse({
        "type": "query_plan",
        "agent": "pdd-shopping-agent",
        "query_plan": query_plan,
    })
    yield sse({
        "type": "activity",
        "agent": "pdd-shopping-agent",
        "kind": "query_plan",
        "provenance": query_plan.get("provenance") or query_plan.get("planner", "gpt"),
        "label": f"Planned review search queries ({query_plan.get('model_deployment') or query_plan.get('planner', FOUNDRY_MODEL_DEPLOYMENT)})",
        "detail": f"Web query: {planned_primary_query}",
    })

    effective_web_query = (
        webiq_query.strip()
        if webiq_query and webiq_query.strip()
        else planned_primary_query
    )

    if has_live_key:
        try:
            clean_key, validated_query = validate_request(webiq_key, effective_web_query, user_intent)
            enrichment = await search_webiq_reviews(
                clean_key,
                validated_query,
                secondary_web_query=planned_secondary_query,
            )
            del clean_key
        except WebIQError as exc:
            safe_msg = str(exc)
            yield sse({
                "type": "enrichment",
                "provider": "webiq",
                "status": "failed",
                "provenance": "live_webiq_error",
                "message": safe_msg,
                "sources": [],
            })
            yield sse({"type": "error", "agent": "pdd-shopping-agent", "message": safe_msg})
            yield sse({"type": "done", "outcome": "error", "run_mode": run_mode})
            return

        has_sources = bool(enrichment.sources)
        yield sse({
            "type": "enrichment",
            "provider": "webiq",
            "status": "ready" if has_sources else "empty",
            "provenance": "live_webiq",
            "message": (
                f"Path 1 (WebIQ Search): Retrieved {len(enrichment.sources)} attributed review-page excerpt(s) (<=200 chars)."
                if has_sources
                else "Live Web IQ search returned 0 matching review pages for this query."
            ),
            "web_query": enrichment.query,
            "sources": [s.citation() for s in enrichment.sources],
        })
        yield sse({
            "type": "activity",
            "agent": "pdd-shopping-agent",
            "kind": "review_discovery",
            "provenance": "live_webiq",
            "label": f"Path 1 (WebIQ Search): Retrieved {len(enrichment.sources)} review-page excerpt(s)",
            "detail": (
                "Platforms: " + ", ".join(sorted({s.platform for s in enrichment.sources}))
                if enrichment.sources
                else "0 results returned from Web IQ."
            ),
        })
    else:
        toolbox_enrichment: Enrichment | None = None
        try:
            toolbox_enrichment = await search_toolbox_web_search(
                effective_web_query,
                 exclude_url=product_url,
            )
        except FoundryResearchError as exc:
            yield sse({
                "type": "activity", "kind": "review_discovery",
                "provenance": exc.code, "label": "Toolbox web_search discovery failed",
                "detail": str(exc),
            })

        if toolbox_enrichment is not None and (toolbox_enrichment.sources):
            enrichment = toolbox_enrichment
            yield sse({
                "type": "enrichment",
                "provider": "toolbox_web_search",
                "status": "ready",
                "provenance": "live_toolbox_web_search",
                "message": (
                    f"Path 2 (Foundry Toolbox web_search): No Web IQ key entered — retrieved {len(enrichment.sources)} "
                    "independent review excerpt(s) (<=200 chars) "
                    f"via {FOUNDRY_TOOLBOX_NAME}."
                ),
                "web_query": enrichment.query,
                "sources": [s.citation() for s in enrichment.sources],
            })
            yield sse({
                "type": "activity",
                "agent": "pdd-shopping-agent",
                "kind": "review_discovery",
                "provenance": "live_toolbox_web_search",
                "label": (
                    f"Path 2 (Foundry Toolbox web_search): Retrieved {len(enrichment.sources)} review-page excerpt(s) ({FOUNDRY_TOOLBOX_NAME})"
                ),
                "detail": (
                    "Platforms: " + ", ".join(sorted({s.platform for s in enrichment.sources}))
                    if enrichment.sources
                    else "0 results returned from Foundry Toolbox web_search."
                ),
            })
        else:
            enrichment = Enrichment(
                query=effective_web_query,
                retrieved_at="",
                sources=(),
                provenance="unavailable_missing_key",
            )
            yield sse({
                "type": "enrichment",
                "provider": "webiq",
                "status": "unavailable_missing_key",
                "provenance": "unavailable_missing_key",
                "message": (
                    "Web IQ API key not provided in the portal — external multi-platform review discovery is unavailable. "
                    "Showing listing-only partial outcome (no sample review fixtures are substituted in Live mode)."
                ),
                "web_query": effective_web_query,
                "sources": [],
            })
            yield sse({
                "type": "activity",
                "agent": "pdd-shopping-agent",
                "kind": "review_discovery",
                "provenance": "unavailable_missing_key",
                "label": "External review discovery unavailable (no Web IQ API key entered in portal)",
                "detail": "Proceeding with honest listing-only partial outcome; sample data is never substituted in Live mode.",
            })

    # Stage 3: Selected-method review inspection, or an explicit search-only choice.
    browsed_sources: list[BrowsedSourceEvidence] = []
    community_outcome = ExplorationOutcome()
    if not inspect_review_pages:
        yield sse({
            "type": "inspection_skipped", "stage": "permitted_source_browsing",
            "reason": "Search-only reviews selected; discovered pages were not opened.",
            "discovered": len(enrichment.sources),
        })
    elif (enrichment.provenance in ("live_webiq", "live_toolbox_web_search") and enrichment.sources) or community_exploration:
        yield sse({
            "type": "status",
            "stage": "permitted_source_browsing",
            "run_id": run_id,
            "scenario": scenario or "live",
            "run_mode": run_mode,
        })
        stage4_q: asyncio.Queue[str] = asyncio.Queue()
        stage4_screenshots: set[str] = set()

        async def _emit_stage4(payload: dict[str, Any]) -> None:
            if payload.get("type") == "screenshot":
                artifact_id = str((payload.get("screenshot") or {}).get("artifact_id") or "")
                if artifact_id in stage4_screenshots:
                    return
                stage4_screenshots.add(artifact_id)
            await stage4_q.put(sse(payload))

        async def _explore(driver: Any, rows: list[BrowsedSourceEvidence]) -> None:
            await explore_communities(
                driver=driver, rows=rows, baseline=baseline, user_intent=user_intent,
                enrichment=enrichment, domains=plan["exploration_domains"], execution_mode=effective_exec_mode,
                webiq_key=webiq_key, emit=_emit_stage4, outcome=community_outcome,
            )

        browse_kwargs: dict[str, Any] = {
            "run_id": run_id,
            "sources": enrichment.sources,
            "execution_mode": effective_exec_mode,
            "emit_sse": _emit_stage4,
        }
        if is_webiq_browse and has_live_key:
            browse_kwargs["webiq_browse_key"] = webiq_key.strip()
        if community_exploration:
            browse_kwargs["explore_communities"] = _explore

        stage4_task = asyncio.create_task(browse_permitted_sources_live(**browse_kwargs))
        try:
            while not stage4_task.done():
                get_t4 = asyncio.create_task(stage4_q.get())
                done_set4, _ = await asyncio.wait(
                    {stage4_task, get_t4},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if get_t4 in done_set4:
                    yield get_t4.result()
                else:
                    get_t4.cancel()
            while not stage4_q.empty():
                yield stage4_q.get_nowait()
            browsed_sources = await stage4_task
        finally:
            if not stage4_task.done():
                stage4_task.cancel()

        for bsrc in browsed_sources:
            if bsrc.screenshot_artifact_id and bsrc.screenshot_artifact_id not in stage4_screenshots:
                rec = SCREENSHOT_STORE.get(bsrc.screenshot_artifact_id)
                if rec is not None:
                    stage4_screenshots.add(bsrc.screenshot_artifact_id)
                    yield sse({"type": "screenshot", "agent": "pdd-shopping-agent", "screenshot": rec.metadata()})
        if community_exploration and community_outcome.status == "not_started":
            community_outcome.status = "failed"
            community_outcome.stop_reason = "The selected browser session could not start community exploration."
            community_outcome.gaps.append(community_outcome.stop_reason)
            yield sse({"type": "community_exploration", "phase": "error", **community_outcome.to_dict()})
        yield sse({
            "type": "browsed_sources",
            "agent": "pdd-shopping-agent",
            "inspection_method": effective_exec_mode,
            "sources": [b.to_dict() for b in browsed_sources],
        })
        yield sse({
            "type": "activity",
            "agent": "pdd-shopping-agent",
            "kind": "permitted_source_browsing",
            "provenance": f"live_{effective_exec_mode}_source_inspection",
            "label": (
                f"{browser_label}: {sum(b.status == 'inspected' for b in browsed_sources)} inspected "
                f"of {len(browsed_sources)} discovered / explored URLs"
            ),
            "detail": "; ".join(f"{b.platform}: {b.access_status}" for b in browsed_sources),
        })

    if use_client_browser:
        CLIENT_BROWSER_BROKER.cleanup_run(run_id)
        yield sse({
            "type": "client_browser_command",
            "target": bridge_target,
            "run_id": run_id,
            "command_id": f"close-{uuid.uuid4().hex[:8]}",
            "action": "close",
            "url": "",
        })


    # Stage 5: Model-guided claim-vs-reality synthesis
    s4_md = load_skill_markdown("claim-reality-matrix")
    yield sse({
        "type": "skill_loaded",
        "agent": "pdd-shopping-agent",
        "skill": "claim-reality-matrix",
        "title": SKILLS_METADATA["claim-reality-matrix"]["title"],
        "chars": len(s4_md),
    })
    yield sse({
        "type": "status",
        "stage": "synthesize_verdict",
        "run_id": run_id,
        "scenario": scenario or "live",
        "run_mode": run_mode,
    })

    synthesis = await synthesize_due_diligence_with_gpt(
        baseline=baseline, user_intent=user_intent, enrichment=enrichment,
        browsed_sources=browsed_sources,
        inspect_review_pages=inspect_review_pages,
        **({"community_exploration": community_outcome.to_dict()} if community_exploration else {}),
    )
    if community_exploration:
        synthesis["evidence_gaps"] = list(dict.fromkeys(
            synthesis.get("evidence_gaps", []) + community_outcome.gaps,
        ))
        synthesis["watch_outs"] += [f"Evidence Gap: {gap}" for gap in community_outcome.gaps[:2]]

    evidence_metrics = {
        "search_content_characters": sum(len(source.content[:3000]) for source in enrichment.sources),
        "inspected_context_characters": sum(len(source.page_content[:6000] or source.observed_excerpt)
                                           for source in browsed_sources if source.status == "inspected"),
        "discovered_sources": len(enrichment.sources),
        "inspected_sources": sum(source.status == "inspected" for source in browsed_sources),
        "inspect_review_pages": inspect_review_pages,
        "community_exploration": community_outcome.to_dict() if community_exploration else None,
    }
    yield sse({
        "type": "claim_matrix",
        "evidence_metrics": evidence_metrics,
        "community_exploration": community_outcome.to_dict() if community_exploration else None,
        "agent": "pdd-shopping-agent",
        "run_mode": synthesis["run_mode"],
        "inspection_method": effective_exec_mode,
        "model_deployment": synthesis.get("model_deployment"),
        "synthesis_status": synthesis.get("synthesis_status"),
        "is_custom_url": baseline.is_custom_url,
        "fit_score": synthesis["fit_score"],
        "fit_label": synthesis["fit_label"],
        "recommendation_badge": synthesis["recommendation_badge"],
        "executive_summary": synthesis["executive_summary"],
        "what_fits": synthesis["what_fits"],
        "watch_outs": synthesis["watch_outs"],
        "evidence_gaps": synthesis.get("evidence_gaps", []),
        "browsed_sources": synthesis.get("browsed_sources", []),
        "matrix": synthesis["claim_matrix"],
        "authenticity_signals": synthesis["authenticity_signals"],
        "evidence_quality_disclaimer": synthesis["evidence_quality_disclaimer"],
    })

    # Stage 6: Guarded Cart Simulation
    s5_md = load_skill_markdown("human-gated-cart-action")
    yield sse({
        "type": "skill_loaded",
        "agent": "pdd-shopping-agent",
        "skill": "human-gated-cart-action",
        "title": SKILLS_METADATA["human-gated-cart-action"]["title"],
        "chars": len(s5_md),
    })
    ticket = BROWSER_DRIVER.issue_cart_approval_ticket(
        run_id=run_id,
        baseline=baseline,
        recommendation_badge=synthesis["recommendation_badge"],
    )
    yield sse({
        "type": "cart_gate",
        "agent": "pdd-shopping-agent",
        "cart_gate": ticket.public_gate_view(),
    })

    report_md = _format_markdown_report(
        plan=plan,
        baseline=baseline,
        enrichment=enrichment,
        synthesis=synthesis,
    )
    slug = re.sub(r"[^a-z0-9]+", "_", f"{baseline.brand}_{baseline.model}".lower()).strip("_") or "product"
    artifact_meta = register_artifact(
        f"{slug}_due_diligence_summary.md",
        report_md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        contains_live_webiq=True,
    )
    yield sse({
        "type": "artifact",
        "agent": "pdd-shopping-agent",
        "id": artifact_meta["id"],
        "filename": artifact_meta["filename"],
        "url": artifact_meta["url"],
        "kind": "generated",
        "persisted": artifact_meta["persisted"],
        "retention_policy": artifact_meta["retention_policy"],
    })
    yield sse({"type": "final", "agent": "pdd-shopping-agent", "text": report_md})
    yield sse({"type": "agent_end", "agent": "pdd-shopping-agent"})
    outcome = (
        "complete"
        if (bat_available and enrichment.provenance in ("live_webiq", "live_toolbox_web_search") and bool(enrichment.sources)
            and synthesis.get("synthesis_status") != "failed" and not synthesis.get("evidence_gaps"))
        else "partial"
    )
    baseline.retrieved_response.clear()
    del enrichment, browsed_sources
    yield sse({"type": "done", "outcome": outcome, "run_mode": synthesis["run_mode"]})


async def run_due_diligence_pipeline(
    *,
    product_url: str,
    user_intent: str,
    scenario: str | None = None,
    webiq_key: str = "",
    webiq_query: str | None = None,
    execution_mode: str = "auto",
    use_client_browser: bool = False,
    inspect_review_pages: bool = True,
    community_exploration: bool = False,
    exploration_domains: list[str] | None = None,
) -> AsyncIterator[str]:
    """Execute the due-diligence pipeline and stream truthful, provenance-labeled SSE events."""
    run_id = uuid.uuid4().hex[:12]
    SCREENSHOT_STORE.start_run(run_id)
    has_live_key = bool(webiq_key and webiq_key.strip())
    normalized_mode = (execution_mode or "auto").strip().lower()
    is_webiq_browse = normalized_mode == "webiq_browse"
    effective_client_browser = bool(
        use_client_browser or normalized_mode in ("client_browser", "vm_playwright")
    )
    effective_bridge_target = "vm_worker" if normalized_mode == "vm_playwright" else "extension"
    run_mode = "mixed_live_search_sample_analysis" if has_live_key else "sample_demo"

    def sse(payload: dict[str, Any]) -> str:
        if payload.get("type") in ("status", "plan", "done"):
            payload.setdefault("inspect_review_pages", inspect_review_pages)
            payload.setdefault("comparison_method", normalized_mode if inspect_review_pages else "search_only")
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    try:
        if community_exploration:
            build_agent_plan(
                product_url, user_intent, execution_mode=normalized_mode, use_client_browser=effective_client_browser,
                inspect_review_pages=inspect_review_pages, community_exploration=True,
                exploration_domains=exploration_domains,
            )
        if normalized_mode in ("live", "client_browser", "vm_playwright", "webiq_browse") or effective_client_browser:
            max_run_seconds = float(
                os.environ.get("PDD_RUN_TIMEOUT_SECONDS", str(PDD_RUN_TIMEOUT_SECONDS))
            )
            deadline = time.monotonic() + max_run_seconds
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def _produce_live_events() -> None:
                try:
                    async for chunk in _run_phase1_live_web_research(
                        run_id=run_id,
                        product_url=product_url,
                        user_intent=user_intent,
                        scenario=scenario,
                        webiq_key=webiq_key,
                        webiq_query=webiq_query,
                        sse=sse,
                        use_client_browser=effective_client_browser,
                        bridge_target=effective_bridge_target,
                        is_webiq_browse=is_webiq_browse,
                        inspect_review_pages=inspect_review_pages,
                        community_exploration=community_exploration,
                        exploration_domains=exploration_domains,
                    ):
                        await queue.put(chunk)
                finally:
                    CLIENT_BROWSER_BROKER.cleanup_run(run_id)
                    await queue.put(None)

            producer_task = asyncio.create_task(_produce_live_events())
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError()
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=min(remaining, 15.0))
                    except asyncio.TimeoutError:
                        if time.monotonic() >= deadline:
                            raise
                        # SSE comments keep idle proxies active without inventing research events.
                        yield ": keep-alive\n\n"
                        continue
                    if item is None:
                        break
                    yield item
                await producer_task
            except asyncio.TimeoutError:
                producer_task.cancel()
                try:
                    await producer_task
                except (asyncio.CancelledError, Exception):
                    pass
                yield sse({
                    "type": "error",
                    "agent": "pdd-shopping-agent",
                    "code": "run_timeout_exceeded",
                    "message": (
                        f"Live Web Research run exceeded the {max_run_seconds:.0f}s total request budget. "
                        "Browser sessions were cleaned up."
                    ),
                })
                yield sse({"type": "done", "outcome": "error", "run_mode": "live_timeout"})
            finally:
                if not producer_task.done():
                    producer_task.cancel()
                    try:
                        await producer_task
                    except (asyncio.CancelledError, Exception):
                        pass
            return

        yield sse({
            "type": "status",
            "stage": "start",
            "run_id": run_id,
            "scenario": scenario or "custom",
            "run_mode": run_mode,
            "capabilities": CAPABILITY_STATUS,
            "toolbox": "tb-product-due-diligence",
        })
        yield sse({
            "type": "agent_start",
            "agent": "pdd-shopping-agent",
            "role": "scenario",
            "label": "Product Due-Diligence Assistant",
            "run_mode": run_mode,
        })

        # Stage 1: Plan & Skill Selection
        plan = build_agent_plan(product_url, user_intent, inspect_review_pages=inspect_review_pages)
        yield sse({"type": "plan", "agent": "pdd-shopping-agent", "plan": plan})
        yield sse({
            "type": "activity",
            "agent": "pdd-shopping-agent",
            "kind": "plan",
            "provenance": run_mode,
            "label": f"Built {len(plan['steps'])}-step check plan around {len(plan['intent_focus_areas'])} buyer priorities",
            "detail": " • ".join(plan["intent_focus_areas"]),
        })

        s1_md = load_skill_markdown("product-listing-digestion")
        yield sse({
            "type": "skill_loaded",
            "agent": "pdd-shopping-agent",
            "skill": "product-listing-digestion",
            "title": SKILLS_METADATA["product-listing-digestion"]["title"],
            "chars": len(s1_md),
        })

        # Stage 2: Product Baseline (Curated Sample Fixture or Unverified Custom URL Checklist)
        yield sse({"type": "status", "stage": "digest_product_url", "run_id": run_id, "scenario": scenario or "custom", "run_mode": run_mode})
        baseline = await BROWSER_DRIVER.digest_product_listing(
            run_id=run_id,
            product_url=product_url,
            user_intent=user_intent,
            resolve_dns=False,
        )
        if baseline.is_custom_url and not has_live_key:
            run_mode = "illustrative_custom_url"

        for shot in baseline.screenshots:
            yield sse({"type": "screenshot", "agent": "pdd-shopping-agent", "screenshot": shot})
        yield sse({
            "type": "product_baseline",
            "agent": "pdd-shopping-agent",
            "baseline": baseline.to_dict(),
        })
        yield sse({
            "type": "activity",
            "agent": "pdd-shopping-agent",
            "kind": "listing_baseline",
            "provenance": baseline.data_provenance,
            "label": (
                f"Loaded sample product baseline: {baseline.brand} {baseline.model} (${baseline.price:.2f})"
                if not baseline.is_custom_url and baseline.price is not None
                else f"Prepared unverified custom-URL checklist for {baseline.model} (live browser scraping not connected)"
            ),
            "detail": baseline.limitation_notice or "",
        })

        s2_md = load_skill_markdown("multi-platform-review-discovery")
        yield sse({
            "type": "skill_loaded",
            "agent": "pdd-shopping-agent",
            "skill": "multi-platform-review-discovery",
            "title": SKILLS_METADATA["multi-platform-review-discovery"]["title"],
            "chars": len(s2_md),
        })
        yield sse({"type": "status", "stage": "webiq_search", "run_id": run_id, "scenario": scenario or "custom", "run_mode": run_mode})

        effective_web_query = (
            webiq_query.strip()
            if webiq_query and webiq_query.strip()
            else f"{baseline.brand} {baseline.model} review {user_intent[:80]}"
        )

        if has_live_key:
            try:
                clean_key, validated_query = validate_request(webiq_key, effective_web_query, user_intent)
                enrichment = await search_webiq_reviews(
                    clean_key,
                    validated_query,
                )
                del clean_key
            except WebIQError as exc:
                safe_msg = str(exc)
                yield sse({
                    "type": "enrichment",
                    "provider": "webiq",
                    "status": "failed",
                    "provenance": "live_webiq_error",
                    "message": safe_msg,
                    "sources": [],
                })
                yield sse({"type": "error", "agent": "pdd-shopping-agent", "message": safe_msg})
                yield sse({"type": "done", "outcome": "error"})
                return
        else:
            enrichment = _build_local_enrichment(
                baseline=baseline,
                user_intent=user_intent,
                web_query=effective_web_query,
            )

        platform_badges = [f"{s.platform} ({s.provenance})" for s in enrichment.sources[:4]] or [
            "No external sources fetched (Custom URL in Sample Mode)"
        ]
        svg_discovery = render_evidence_svg(
            stage_badge="STAGE ILLUSTRATION • REVIEW SOURCES",
            title=f"Review Sources ({len(enrichment.sources)} Attributed Excerpts)",
            url=baseline.canonical_url,
            highlights=platform_badges,
            status_color="#d97706",
            footer_note="Generated SVG Stage Illustration • Excerpts Capped at <=200 Chars",
        )
        cap3 = SCREENSHOT_STORE.record_capture(
            run_id=run_id,
            sequence=3,
            stage="review_source_summary",
            title=f"Stage Illustration: {len(enrichment.sources)} Review Source(s)",
            url=baseline.canonical_url,
            caption="Generated SVG summary card of loaded review sources (not a live browser screenshot).",
            data=svg_discovery,
        )
        yield sse({"type": "screenshot", "agent": "pdd-shopping-agent", "screenshot": cap3.metadata()})

        has_sources = bool(enrichment.sources)
        yield sse({
            "type": "enrichment",
            "provider": "webiq",
            "status": "ready" if has_sources else "empty",
            "provenance": enrichment.provenance,
            "message": (
                f"Loaded {len(enrichment.sources)} sample review excerpt(s) across "
                f"{', '.join(sorted({s.platform for s in enrichment.sources}))}."
                if enrichment.provenance == "sample_fixture" and has_sources
                else (
                    f"Retrieved {len(enrichment.sources)} live Web IQ excerpt(s) (<=200 chars)."
                    if has_sources
                    else "Custom URL in Sample Mode: no live Web IQ search key was supplied, so no external review citations were fabricated."
                )
            ),
            "web_query": enrichment.query,
            "sources": [s.citation() for s in enrichment.sources],
        })
        yield sse({
            "type": "activity",
            "agent": "pdd-shopping-agent",
            "kind": "review_discovery",
            "provenance": enrichment.provenance,
            "label": (
                f"Loaded {len(enrichment.sources)} sample review excerpt(s)"
                if enrichment.provenance == "sample_fixture"
                else (
                    f"Retrieved {len(enrichment.sources)} live Web IQ excerpt(s)"
                    if has_sources
                    else "Skipped external review lookup (Custom URL in Sample Mode without live Web IQ key)"
                )
            ),
            "detail": (
                "Platforms: " + ", ".join(sorted({s.platform for s in enrichment.sources}))
                if enrichment.sources
                else "No external platforms queried in Sample Mode."
            ),
        })


        # Stage 5: Decision-First Claim-vs-Reality Synthesis
        s4_md = load_skill_markdown("claim-reality-matrix")
        yield sse({
            "type": "skill_loaded",
            "agent": "pdd-shopping-agent",
            "skill": "claim-reality-matrix",
            "title": SKILLS_METADATA["claim-reality-matrix"]["title"],
            "chars": len(s4_md),
        })
        yield sse({"type": "status", "stage": "synthesize_verdict", "run_id": run_id, "scenario": scenario or "custom", "run_mode": run_mode})
        synthesis = _synthesize_claim_matrix_and_signals(
            baseline=baseline,
            user_intent=user_intent,
            enrichment=enrichment,
            run_mode=run_mode,
        )
        yield sse({
            "type": "claim_matrix",
            "agent": "pdd-shopping-agent",
            "run_mode": run_mode,
            "is_custom_url": baseline.is_custom_url,
            "fit_score": synthesis["fit_score"],
            "fit_label": synthesis["fit_label"],
            "recommendation_badge": synthesis["recommendation_badge"],
            "executive_summary": synthesis["executive_summary"],
            "what_fits": synthesis["what_fits"],
            "watch_outs": synthesis["watch_outs"],
            "matrix": synthesis["claim_matrix"],
            "authenticity_signals": synthesis["authenticity_signals"],
            "evidence_quality_disclaimer": synthesis["evidence_quality_disclaimer"],
        })

        # Stage 6: Guarded Cart Simulation
        s5_md = load_skill_markdown("human-gated-cart-action")
        yield sse({
            "type": "skill_loaded",
            "agent": "pdd-shopping-agent",
            "skill": "human-gated-cart-action",
            "title": SKILLS_METADATA["human-gated-cart-action"]["title"],
            "chars": len(s5_md),
        })
        ticket = BROWSER_DRIVER.issue_cart_approval_ticket(
            run_id=run_id,
            baseline=baseline,
            recommendation_badge=synthesis["recommendation_badge"],
        )
        yield sse({
            "type": "cart_gate",
            "agent": "pdd-shopping-agent",
            "cart_gate": ticket.public_gate_view(),
        })

        report_md = _format_markdown_report(
            plan=plan,
            baseline=baseline,
            enrichment=enrichment,
            synthesis=synthesis,
        )
        slug = re.sub(r"[^a-z0-9]+", "_", f"{baseline.brand}_{baseline.model}".lower()).strip("_")
        artifact_meta = register_artifact(
            f"{slug}_due_diligence_summary.md",
            report_md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            contains_live_webiq=has_live_key,
        )
        yield sse({
            "type": "artifact",
            "agent": "pdd-shopping-agent",
            "id": artifact_meta["id"],
            "filename": artifact_meta["filename"],
            "url": artifact_meta["url"],
            "kind": "generated",
            "persisted": artifact_meta["persisted"],
            "retention_policy": artifact_meta["retention_policy"],
        })
        yield sse({"type": "final", "agent": "pdd-shopping-agent", "text": report_md})
        yield sse({"type": "agent_end", "agent": "pdd-shopping-agent"})
        yield sse({"type": "done", "outcome": "complete" if (has_sources or baseline.is_custom_url) else "partial", "run_mode": run_mode})
    except UrlValidationError as exc:
        yield sse({"type": "error", "agent": "pdd-shopping-agent", "message": str(exc)})
        yield sse({"type": "done", "outcome": "error"})
    except Exception as exc:  # noqa: BLE001
        yield sse({"type": "error", "agent": "pdd-shopping-agent", "message": f"Pipeline failed: {type(exc).__name__}"})
        yield sse({"type": "done", "outcome": "error"})
    finally:
        SCREENSHOT_STORE.finish_run(run_id)
