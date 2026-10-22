"""Simulated Product Baseline & Human-Gated Cart Guardrail Driver.

Truthful implementation contract:
  - Does NOT connect to a live browser or scrape live retail DOMs in this prototype.
  - For the 3 curated sample products (`sony-wh1000xm6`, `asus-pg32ucdm-oled`, `fellow-ode-gen2`),
    returns explicitly labeled `sample_fixture` product baselines.
  - For any custom URL entered by a user, does NOT invent fake prices, SKUs, or seller claims.
    Instead returns `data_provenance="illustrative_custom_url"` with `is_custom_url=True` and
    an explicit checklist of what a live integration would inspect.
  - `execute_approved_cart_action` is explicitly labeled a local cart gate simulation (`simulation_only`)
    that validates SKU/variant/seller/price-ceiling constraints and hard-blocks checkout/payment routes,
    never modifying any real retailer shopping cart.
"""
from __future__ import annotations

import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

try:
    from .config import SCENARIOS
    from .screenshot_store import SCREENSHOT_STORE, render_evidence_svg
    from .url_safety import safe_display_url, validate_public_https_url
except ImportError:
    from config import SCENARIOS
    from screenshot_store import SCREENSHOT_STORE, render_evidence_svg
    from url_safety import safe_display_url, validate_public_https_url

FORBIDDEN_CART_ACTIONS = frozenset({
    "proceed_to_checkout",
    "buy_now",
    "one_click_buy",
    "submit_order",
    "enter_payment",
    "enter_shipping",
    "place_order",
})

FORBIDDEN_URL_SEGMENTS = (
    "/checkout",
    "/payment",
    "/billing",
    "/buy-now",
    "/one-click",
    "/place-order",
    "/signin",
    "/login",
)


class CartPolicyViolationError(Exception):
    """Raised when a simulated cart action violates human-gate or checkout-block guardrails."""


@dataclass
class ProductBaseline:
    product_url: str
    canonical_url: str
    brand: str
    model: str
    sku: str
    variant: str
    price: float | None
    currency: str
    seller: str
    condition: str
    return_policy: str
    seller_claims: list[str]
    puffery_filtered: list[str]
    screenshots: list[dict[str, Any]] = field(default_factory=list)
    data_provenance: str = "sample_fixture"
    is_custom_url: bool = False
    limitation_notice: str | None = None
    retrieved_response: dict[str, Any] = field(default_factory=dict, repr=False)
    provider: str = ""
    retrieved_characters: int = 0
    content_display_limit: int = 0
    content_truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CartApprovalTicket:
    approval_token: str
    run_id: str
    product_url: str
    brand: str
    model: str
    sku: str
    variant: str
    seller: str
    price: float | None
    currency: str
    max_price_ceiling: float | None
    recommendation_badge: str
    created_at: str
    is_custom_url: bool = False
    simulation_only: bool = True
    consumed: bool = False

    def public_gate_view(self) -> dict[str, Any]:
        return {
            "approval_token": self.approval_token,
            "run_id": self.run_id,
            "product_url": self.product_url,
            "brand": self.brand,
            "model": self.model,
            "sku": self.sku,
            "variant": self.variant,
            "seller": self.seller,
            "price": self.price,
            "currency": self.currency,
            "max_price_ceiling": self.max_price_ceiling,
            "recommendation_badge": self.recommendation_badge,
            "simulation_only": True,
            "simulation_notice": (
                "Simulated Guardrail Check Only: Approving this step tests the SKU/price-ceiling "
                "lock in memory and generates an illustrative SVG card. No real retailer cart is modified."
            ),
            "is_custom_url": self.is_custom_url,
            "allowed_action": "add_to_cart",
            "blocked_actions": sorted(FORBIDDEN_CART_ACTIONS),
        }


_CATALOG_BASELINES: dict[str, dict[str, Any]] = {
    "xm6": {
        "brand": "Sony",
        "model": "WH-1000XM6 Wireless Noise-Canceling Headphones",
        "sku": "6580001-XM6-BLK",
        "variant": "Midnight Black (Over-Ear)",
        "price": 399.99,
        "currency": "USD",
        "seller": "Best Buy Direct (Sample Fixture)",
        "condition": "New — Factory Sealed (Sample Data)",
        "return_policy": "30-day return policy (Sample Data)",
        "seller_claims": [
            "Up to 30-hour battery life with Active Noise Cancellation enabled",
            "Dual-Processor V2 + QN3 HD Noise Canceling Processor",
            "All-day pressure-free comfort for eyewear and glasses users",
            "Seamless dual-device Bluetooth 5.4 Multipoint connection",
            "Reinforced folding hinge durability & 6-mic beamforming calls",
        ],
        "puffery_filtered": [
            "Unrivaled acoustic perfection that redefines silence forever",
            "The most breathtaking soundstage ever engineered",
        ],
    },
    "pg32ucdm": {
        "brand": "ASUS",
        "model": "ROG Swift OLED PG32UCDM 32\" 4K 240Hz Monitor",
        "sku": "ASUS-PG32UCDM-32QD",
        "variant": "32-inch 4K UHD (3840x2160) 240Hz QD-OLED",
        "price": 1199.00,
        "currency": "USD",
        "seller": "B&H Photo Video (Sample Fixture)",
        "condition": "New (Sample Data)",
        "return_policy": "30-day return + 3-year manufacturer OLED warranty (Sample Data)",
        "seller_claims": [
            "3rd-Gen 4K QD-OLED 140 PPI subpixel layout delivers sharp desktop text clarity",
            "Infinite contrast ratio with deepest true blacks in any room environment",
            "Custom fanless graphene heatsink + 3-year OLED burn-in warranty",
            "Built-in Smart KVM and 90W USB-C Power Delivery for single-cable macOS/PC switching",
        ],
        "puffery_filtered": [
            "Transcend reality with god-tier gaming immersion",
        ],
    },
    "ode": {
        "brand": "Fellow",
        "model": "Ode Brew Grinder Gen 2",
        "sku": "FELLOW-ODE-G2-MBK",
        "variant": "Matte Black — 64mm Gen 2 Brew Burrs",
        "price": 345.00,
        "currency": "USD",
        "seller": "Fellow Products Official (Sample Fixture)",
        "condition": "New (Sample Data)",
        "return_policy": "30-day trial + 2-year warranty (Sample Data)",
        "seller_claims": [
            "Anti-static ionizer technology for <0.1g single-dose retention",
            "Whisper-quiet cafe-grade grinding with automatic shutoff",
            "Full-range precision for every morning brewed coffee style",
        ],
        "puffery_filtered": [
            "Transform your kitchen counter into a world-champion barista sanctuary",
        ],
    },
}


def _match_preset_key(url: str) -> str | None:
    parsed = urlparse(url)
    norm_path = (parsed.path or "").rstrip("/")
    families = {
        "sony-wh1000xm6": "xm6",
        "asus-pg32ucdm-oled": "pg32ucdm",
        "fellow-ode-gen2": "ode",
    }
    for key, scenario in SCENARIOS.items():
        candidate_urls = [scenario["product_url"]] + [
            t["url"] for t in scenario.get("store_targets", []) if t.get("url")
        ]
        for candidate in candidate_urls:
            sample = urlparse(candidate)
            if (parsed.hostname, norm_path) == (sample.hostname, (sample.path or "").rstrip("/")):
                return families.get(key)
    return None


class CDPBrowserDriver:
    """Simulated product listing & cart guardrail driver (no live browser connection)."""

    def __init__(self) -> None:
        self._tickets: dict[str, CartApprovalTicket] = {}

    async def digest_product_listing(
        self,
        *,
        run_id: str,
        product_url: str,
        user_intent: str,
        resolve_dns: bool = False,
    ) -> ProductBaseline:
        clean_url = validate_public_https_url(product_url, resolve_dns=resolve_dns)
        preset_key = _match_preset_key(clean_url)

        if preset_key and preset_key in _CATALOG_BASELINES:
            item = _CATALOG_BASELINES[preset_key]
            baseline = ProductBaseline(
                product_url=clean_url,
                canonical_url=clean_url,
                brand=item["brand"],
                model=item["model"],
                sku=item["sku"],
                variant=item["variant"],
                price=item["price"],
                currency=item["currency"],
                seller=item["seller"],
                condition=item["condition"],
                return_policy=item["return_policy"],
                seller_claims=list(item["seller_claims"]),
                puffery_filtered=list(item["puffery_filtered"]),
                data_provenance="sample_fixture",
                is_custom_url=False,
                limitation_notice=(
                    "Curated Sample Fixture: Product details and claims below come from a pre-built "
                    "sample scenario, not a live browser scrape."
                ),
            )
            svg1 = render_evidence_svg(
                stage_badge="SAMPLE ILLUSTRATION • LISTING BASELINE",
                title=f"Sample Product Baseline: {baseline.brand} {baseline.model}",
                url=clean_url,
                highlights=[
                    f"SKU: {baseline.sku} • Variant: {baseline.variant}",
                    f"Sample Price: ${baseline.price:.2f} {baseline.currency} ({baseline.seller})",
                    f"Source: Curated Sample Fixture (Not a live browser capture)",
                ],
                status_color="#0284c7",
                footer_note="Generated SVG Stage Illustration • Live Browser Automation Not Connected",
            )
            cap1 = SCREENSHOT_STORE.record_capture(
                run_id=run_id,
                sequence=1,
                stage="sample_listing_baseline",
                title=f"Stage Illustration: {baseline.brand} {baseline.model} Listing Baseline",
                url=clean_url,
                caption="Generated SVG summary card for curated sample product (not a live browser screenshot).",
                data=svg1,
            )
            svg2 = render_evidence_svg(
                stage_badge="SAMPLE ILLUSTRATION • CLAIMS EXTRACTED",
                title=f"Testable Claims vs. Filtered Puffery ({len(baseline.seller_claims)} claims)",
                url=clean_url,
                highlights=baseline.seller_claims[:3] + [
                    f"Filtered Puffery: \"{baseline.puffery_filtered[0]}\""
                ],
                status_color="#059669",
                footer_note="Generated SVG Stage Illustration • Curated Sample Fixture",
            )
            cap2 = SCREENSHOT_STORE.record_capture(
                run_id=run_id,
                sequence=2,
                stage="sample_claim_extraction",
                title="Stage Illustration: Extracted Seller Claims vs. Puffery",
                url=clean_url,
                caption="Generated SVG summary card showing extracted testable claims vs. marketing puffery.",
                data=svg2,
            )
            baseline.screenshots = [cap1.metadata(), cap2.metadata()]
            return baseline

        # Custom URL path: DO NOT fabricate fake prices, SKUs, or guessed claims!
        parsed = urlparse(clean_url)
        host = (parsed.hostname or "retailer").replace("www.", "")
        slug_parts = [p for p in parsed.path.split("/") if p and not p.isdigit()]
        raw_slug = slug_parts[-1] if slug_parts else host
        readable_name = raw_slug.replace("-", " ").replace("_", " ").strip().title() or "Custom Product URL"

        baseline = ProductBaseline(
            product_url=clean_url,
            canonical_url=clean_url,
            brand=f"Unverified ({host})",
            model=readable_name,
            sku="UNVERIFIED-CUSTOM-URL",
            variant="Unverified (Live browser extraction not connected)",
            price=None,
            currency="USD",
            seller=f"{host} (Unverified in Sample Mode)",
            condition="Unverified",
            return_policy="Verify directly on retailer listing",
            seller_claims=[
                f"Seller specification claims on {host} related to: {user_intent[:90]}",
                "Advertised warranty, return window, and included accessories on listing page",
                "Exact model number, SKU, and variant pricing on retailer page",
            ],
            puffery_filtered=[
                "Unverified marketing superlatives (requires live page inspection)",
            ],
            data_provenance="illustrative_custom_url",
            is_custom_url=True,
            limitation_notice=(
                "Unverified Custom URL (Sample Mode): Live Browser Automation is not connected in this "
                "prototype, so the assistant did not fetch or scrape your URL. No price, SKU, or product "
                "claims have been guessed or fabricated."
            ),
        )
        svg1 = render_evidence_svg(
            stage_badge="UNVERIFIED CUSTOM URL • SAMPLE MODE",
            title=f"Custom URL Checklist: {readable_name[:55]}",
            url=clean_url,
            highlights=[
                "Live browser page scraping is not connected in Sample Mode.",
                "No price, SKU, or seller claims were guessed or fabricated.",
                f"User priorities to verify: {user_intent[:75]}",
            ],
            status_color="#d97706",
            footer_note="Generated SVG Stage Illustration • Unverified Custom URL",
        )
        cap1 = SCREENSHOT_STORE.record_capture(
            run_id=run_id,
            sequence=1,
            stage="custom_url_notice",
            title=f"Stage Illustration: Custom URL Checklist ({host})",
            url=clean_url,
            caption="Generated SVG notice explaining that custom URLs are not scraped in Sample Mode.",
            data=svg1,
        )
        baseline.screenshots = [cap1.metadata()]
        return baseline

    def issue_cart_approval_ticket(
        self,
        *,
        run_id: str,
        baseline: ProductBaseline,
        recommendation_badge: str,
    ) -> CartApprovalTicket:
        token = f"sim-gate-{secrets.token_hex(8)}"
        ticket = CartApprovalTicket(
            approval_token=token,
            run_id=run_id,
            product_url=baseline.canonical_url,
            brand=baseline.brand,
            model=baseline.model,
            sku=baseline.sku,
            variant=baseline.variant,
            seller=baseline.seller,
            price=baseline.price,
            currency=baseline.currency,
            max_price_ceiling=baseline.price,
            recommendation_badge=recommendation_badge,
            created_at=datetime.now(timezone.utc).isoformat(),
            is_custom_url=baseline.is_custom_url,
            simulation_only=True,
        )
        self._tickets[token] = ticket
        return ticket

    def execute_approved_cart_action(
        self,
        *,
        approval_token: str,
        approved: bool,
        sku: str,
        variant: str,
        seller: str,
        max_price: float | None,
        quantity: int = 1,
        requested_action: str = "add_to_cart",
        target_url: str | None = None,
    ) -> dict[str, Any]:
        if requested_action != "add_to_cart" or requested_action in FORBIDDEN_CART_ACTIONS:
            raise CartPolicyViolationError(
                f"Action '{requested_action}' is hard-blocked by shopping safety policy. "
                "Only simulated 'add_to_cart' is permitted; checkout, payment, and order placement are forbidden."
            )

        if target_url:
            lower_target = target_url.lower()
            for seg in FORBIDDEN_URL_SEGMENTS:
                if seg in lower_target:
                    raise CartPolicyViolationError(
                        f"Navigation to '{seg}' is hard-blocked by checkout/payment guardrail."
                    )

        ticket = self._tickets.get(approval_token)
        if not ticket:
            raise CartPolicyViolationError("Invalid or expired cart approval token.")
        if ticket.consumed:
            raise CartPolicyViolationError("This cart approval has already been used. Start a new check to simulate again.")

        if not approved:
            return {
                "status": "cancelled_by_user",
                "simulation_only": True,
                "checkout_blocked": True,
                "message": "Simulated Add-to-Cart action cancelled by user.",
            }

        if ticket.is_custom_url or ticket.price is None:
            raise CartPolicyViolationError(
                "Simulated Add-to-Cart is disabled for unverified custom URLs because exact SKU and live price "
                "were not observed."
            )

        if sku != ticket.sku or variant != ticket.variant or seller != ticket.seller:
            raise CartPolicyViolationError(
                "SKU, variant, or seller mismatch against the locked approval ticket."
            )

        if max_price is None or float(max_price) < float(ticket.price):
            raise CartPolicyViolationError(
                f"Observed price ${ticket.price:.2f} exceeds approved ceiling ${float(max_price or 0):.2f}."
            )

        if quantity != 1:
            raise CartPolicyViolationError("Quantity is locked to 1 for guarded cart simulation.")

        ticket.consumed = True
        svg = render_evidence_svg(
            stage_badge="SIMULATED CART ACTION • DEMO ONLY",
            title=f"Simulated Add-to-Cart: {ticket.brand} {ticket.model}",
            url=ticket.product_url,
            highlights=[
                f"Verified SKU: {ticket.sku} • Variant: {ticket.variant}",
                f"Locked Offer: ${ticket.price:.2f} {ticket.currency} via {ticket.seller}",
                "SIMULATION ONLY: No real retailer cart was modified; /checkout remains hard-blocked.",
            ],
            status_color="#10b981",
            footer_note="Generated SVG Simulation Card • In-Memory Guardrail Check Only",
        )
        cap = SCREENSHOT_STORE.record_capture(
            run_id=ticket.run_id,
            sequence=99,
            stage="simulated_cart_confirmation",
            title=f"Simulated Cart Gate Confirmation: {ticket.sku}",
            url=ticket.product_url,
            caption="Generated SVG card confirming the in-memory cart guardrail check (no real retailer cart modified).",
            data=svg,
        )
        return {
            "status": "simulated_add_to_cart",
            "simulation_only": True,
            "checkout_blocked": True,
            "sku": ticket.sku,
            "variant": ticket.variant,
            "seller": ticket.seller,
            "price": ticket.price,
            "currency": ticket.currency,
            "message": (
                f"Simulation complete: validated SKU {ticket.sku} (${ticket.price:.2f}) in memory. "
                "No real retailer shopping cart was modified, and checkout/payment routes remain hard-blocked."
            ),
            "screenshot": cap.metadata(),
        }


BROWSER_DRIVER = CDPBrowserDriver()
