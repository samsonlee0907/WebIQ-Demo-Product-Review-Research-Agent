# Portal user guide

## Start a research run

1. Open the portal. If hosted, sign in with the access details supplied by its operator.
2. Paste a public HTTPS page for the product or offer you want to investigate.
3. Describe your priorities: intended use, constraints, budget, durability, comfort, compatibility, support or return-policy concerns.
4. Review the selected method above the main action. Use **Change method** for live settings; **Sample** is the safe default.
5. Start research and follow the four-stage timeline.

A neutral example priority statement is: "I need reliable daily use, comfortable long sessions, straightforward maintenance and a clear return policy within my budget." Be specific about the decision, not only the category.

Use a real reachable URL. Documentation placeholder domains are not test products. Do not submit private intranet pages, authentication links, URLs containing credentials or confidential customer information.

## Choose the right path

| Choose | When |
|---|---|
| Sample | Learn the interface without a cloud model. Keep the WebIQ key empty for fully offline behavior |
| WebIQ Browse | Use extracted listing/review content without native screenshots |
| Azure BAT | Use the operator's configured remote browser workspace |
| Local browser / Playwright | Use your connected desktop browser profile/network |
| Azure VM + Playwright | Use the operator's online VM worker |
| Search-only reviews | Skip opening discovered review pages; the listing still uses its selected inspector |

All live routes require the operator's model configuration. Local/VM methods are not local/offline AI inference. A request-scoped WebIQ key enables WebIQ discovery; otherwise the operator must provide Toolbox `web_search`.

An extension or VM cannot be substituted silently if a source blocks another inspector. Results can be partial, and a missing or restricted page remains an evidence gap.

### Connect the local browser

Use Chrome/Edge in a dedicated desktop profile and follow the [extension guide](../browser_extension/README.md). For a hosted portal, configure that portal's exact host in the extension manifest before loading it. Refresh the portal and confirm the connection badge.

The app's embedded browser preview is not an extension host. Keep the real portal tab open while its browser commands run. Do not enter passwords or interact with shopping/checkout flows to help a run.

### Explore community discussions

For BAT, Local or VM with review inspection enabled, turn on **agent-guided community exploration** and edit the domain list to relevant discussion/review sites. Enter domains, not arbitrary URL paths. The list limits the extra exploration phase, not general discovery.

The model can choose scoped searches, observed links, page visits, scrolling or completion within fixed budgets. The timeline exposes short goals/reasons and captured observations. These are action explanations, not private chain-of-thought.

Visible comments are candidate evidence, not verified owners or automatically authentic feedback. Some sites load discussions through requests blocked by read-only policies; the report should disclose that limitation rather than claim complete coverage.

## Read the result

The buying brief prioritizes the apparent fit, watch-outs and evidence gaps. Expand claim checks and follow the citations. Treat the recommendation as an attributed research aid, not a guarantee that specifications, stock, prices, reviewer identity or seller claims have been independently certified.

The timeline distinguishes:

| Item | What it establishes |
|---|---|
| Search result | A discovered URL with provider-returned grounding; not proof that the page was visited |
| Inspected page | Bounded content actually returned by the selected inspector, with status and restrictions |
| Genuine screenshot | Captured browser viewport bytes with identity/time/hash, not a generated illustration |
| Sample illustration | Labelled visual aid; not evidence of a real page visit |
| Community observation | Visible discussion candidates and bounded browser actions, not exhaustive comments |
| Partial/failed stage | Work that did not complete; missing information must not be filled in as verified fact |

Expand each searched/inspected item for the available response-shaped text and related screenshot. These are bounded views, not complete HTML dumps. Raw WebIQ panels are cleared when the workflow ends and are not restored from saved history.

Automatic challenge monitoring allows two same-session rechecks. Interactive verification may remain unresolved and be skipped. No CAPTCHA bypass, login automation or alternate-proxy retry is provided.

## Reopen evidence later

Open **Saved research** from the header or the follow-up link shown for the run. You can:

- Reopen the generated assessment and full generated Markdown when retained.
- Expand independent browser observations and agent exploration decisions.
- Inspect genuine screenshots with their timestamps and SHA-256 digests.
- Download a ZIP containing `research.json`, `report.md` and original screenshot files.
- Copy a follow-up link or explicitly delete the archived record.

New evidence is checkpointed during a run and retained until explicitly deleted. A storage failure is shown as a failure/incomplete retention state, not a successful save. Hosted records require the portal's private storage and access gate; a copied link does not bypass sign-in.

**Raw WebIQ Search and Browse responses are not archived.** Generated attributed internal reports and independent browser captures are different artifacts. Search-only/WebIQ paths may have no independent browser screenshots. Historical imported evidence can lack an expired generated brief; it is not recreated.

This is a shared internal-demo library: **all portal passcode holders can view and delete all saved records**. Do not use it for confidential multi-user research.

Deletion removes that archive's manifest/capture objects. Downloaded ZIPs, separate benchmark copies and external copies are not removed. Deletion is unavailable while the same process is actively recording that run.

## Compare methods

**Compare methods** opens the recorded benchmark report without initiating a new search or inference. It is a historical snapshot, not a fresh evaluation for your inputs. Existing product-specific examples are intentionally preserved.

The current tab can also show a small, short-lived set of live comparison summaries. Those are separate from durable saved history. Different products, priorities, source sets, access states or queries prevent controlled answer-quality comparisons. More retrieved characters or visible discussion blocks do not prove a better recommendation.

## Cart approval is only a simulation

When available, the approval ticket locks product identity, variant, seller, quantity and price ceiling. Approval produces a simulated receipt. It never changes a real retailer cart, submits an order or reaches payment/checkout. A failed or missing approval gate should not be worked around.

## Screenshot walkthrough

Start with the [solution concept diagrams](Product-Review-Agent-Solution-Concept.pptx) for a five-slide explanation of the architecture, agent-planned flow, request sequence, independent routing choices and evidence lifecycle. Its boxes, labels and connectors remain editable; official Microsoft service icons identify Container Apps, Foundry Models, Storage, managed identity and the Azure VM where applicable. Original SVGs, proportional PNGs and usage terms are retained under `docs\assets\azure-icons`. The deck documents the current prototype, not the unrelated services or multi-user authorization in the visual-reference deck.

The [README portal tour](../README.md#a-tour-of-the-portal) shows recorded project-owned UI captures for input, research progress and saved export. The example is dated, not current product advice. The deployment-specific presentation and original publisher-page captures remain private rather than being redistributed in the public source snapshot.

Optional authoring dependencies are listed in `requirements-presentations.txt`. `scripts\capture_portal_walkthrough.py --help` describes the explicit single-live-run capture command; it requires an operator-supplied private access JSON, desktop Edge and working live integrations. A new live capture refuses an existing output directory to prevent an accidental repeat. If capture is interrupted after research completes, `--resume-archive-id` recovers the remaining screenshots through the real saved-research UI without another research call. The walkthrough distinguishes that retained inventory from the transient live timeline.

`scripts\build_portal_walkthrough_deck.py --help` rebuilds a new presentation from the capture receipt without another research call. Existing presentations and receipts are never overwritten. Windows PowerPoint is required only for its optional rendering audit; it closes only its own read-only output, never another open presentation.

`scripts\build_solution_concept_deck.py --help` builds a new concept deck without cloud calls or access to the visual-reference file. Use `--render-dir` with an empty folder to export diagram PNGs and perform the same PowerPoint text-bounds audit.
