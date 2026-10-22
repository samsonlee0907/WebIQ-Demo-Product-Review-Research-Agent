---
name: product-listing-digestion
description: Digest the initial product URL with the selected inspector to extract observed identity, offer and testable seller claims, without guessing missing facts.
---

# Product Listing Digestion Skill

## Purpose
When the user provides an initial `product_url` and `user_intent`, establish the observed **Product Baseline** before searching external review sites.

## Execution Methodology

1. **Validate & Observe the Initial Product URL**:
   - Enforce public HTTPS validation (`validate_public_https_url`). Reject non-HTTPS, localhost, private IPs, or credential-bearing URLs.
   - Use exactly the selected inspector: WebIQ Browse, Azure BAT Tool, Local Playwright or Azure VM + Playwright. Browser methods capture genuine viewport screenshots where available; WebIQ Browse returns extracted content, not screenshots.
   - Report restrictions, pending crawls, missing fields and failed fetches explicitly. Do not substitute another inspector or infer identity, price or claims as if observed.
   - For a CAPTCHA or browser-verification interstitial, allow two automatic rechecks after three-second waits in the same session. Resume only on observed clearance. Keep runs unattended; unresolved challenges and hard denials remain explicit gaps, not invented listing facts. Login requirements, rate limits and legal restrictions are not retried. Do not weaken read-only controls or claim interactive puzzle-solving capability.
2. **Extract Canonical Product Identity**:
   - **Brand & Exact Model Number**: Disambiguate generational names (e.g., `WH-1000XM6` vs. `WH-1000XM5`, `PG32UCDM` vs. `PG32UCDP`, `Ode Gen 2` vs. `Ode Gen 1`).
   - **SKU / Variant**: Record color, capacity/size, sub-model, and regional variant.
   - **Commercial Offer**: Record displayed price, currency, seller/merchant name (first-party vs. third-party marketplace seller), stock status, and return policy window.
3. **Separate Testable Seller Claims from Subjective Puffery**:
   - **Testable Claims** (include in Claim-vs-Reality Matrix): Battery life hours, acoustic attenuation (dB), display brightness/text clarity, retention weight (grams), thermal/fan noise (dBA), multipoint connectivity, hinge/chassis materials, warranty coverage.
   - **Subjective Puffery** (exclude or flag as non-testable): "World's best sound", "Revolutionary design", "Unmatched luxury".
4. **Map Claims to User Intent**:
   - Prioritize claims that intersect with `user_intent` so review-site discovery and evidence synthesis focus on the buyer's actual questions.
