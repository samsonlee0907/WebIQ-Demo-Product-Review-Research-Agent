---
name: human-gated-cart-action
description: Prepare a bounded, human-gated Add-to-Cart approval request with exact SKU, variant, seller, and price ceiling verification while strictly blocking checkout, payment, or automatic purchase.
---

# Human-Gated Cart Action Skill

## Purpose
Enforce a strict human-in-the-loop safety boundary between **Research Mode** (read-only due diligence) and **Cart Mode** (bounded `Add to Cart` execution).

## Mandatory Safety Rules

1. **Never Auto-Add or Auto-Purchase**:
   - The agent must **never** add an item to the cart during autonomous due-diligence execution, and must **never** initiate checkout, payment, login, address modification, warranty upsell, or order placement under any circumstances.
2. **Prepare an Explicit Approval Payload (`CartGatePayload`)**:
   - At the conclusion of due diligence, emit a `cart_gate` payload containing:
     - `approval_token`: Cryptographically random per-run token bound to the verified offer.
     - `product_url`: Validated public HTTPS product URL.
     - `sku` & `variant`: Exact canonical SKU and variant inspected.
     - `seller`: Verified seller/merchant name.
     - `unit_price` & `max_price`: Observed price and default price ceiling.
     - `quantity`: Default `1`.
     - `recommendation_badge`: `Recommended for Your Intent`, `Recommended with Caveats`, or `Hold / Consider Alternatives`.
3. **Post-Approval Bounded Verification**:
   - Only when the user explicitly submits `POST /api/cart/approve` with `approved: true` and matching `approval_token`, `sku`, `variant`, `seller`, and `unit_price <= max_price`:
     - Execute a single bounded `Add to Cart` action.
     - Immediately abort if the target URL or action touches `/checkout`, `/payment`, `/billing`, `/signin`, or `buy-now`.
     - Capture an immutable post-cart verification screenshot and return a structured cart receipt.
