"""Standalone Windows Native Chrome + Playwright Remote Worker for Product Due-Diligence Assistant.

Runs on a Windows VM (or any Windows desktop/server with Google Chrome installed), attaches to or
launches real Google Chrome (`channel="chrome"`), polls the online/local portal for pending
`client_browser_command` tasks (`GET /api/client-browser/next-command`), executes Playwright
navigation, scrolling, PNG screenshot capture (`page.screenshot(type="png")`), and DOM/JSON-LD
Product/Offer extraction, and posts the observation back (`POST /api/client-browser/observation`).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

EXTRACTION_JS = """
() => {
  const clean = (s, maxLen = 300) => String(s || '').replace(/\\s+/g, ' ').trim().slice(0, maxLen);
  const title = clean(document.title, 220);

  const headings = [];
  document.querySelectorAll('h1, h2, h3, [data-testid*="title"], [class*="product-title"], [class*="sku-title"]').forEach((el) => {
    const t = clean(el.innerText || el.textContent, 200);
    if (t && t.length > 2 && !headings.includes(t) && headings.length < 12) {
      headings.push(t);
    }
  });

  const jsonLdItems = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach((script) => {
    try {
      const parsed = JSON.parse(script.textContent || '{}');
      const queue = Array.isArray(parsed) ? [...parsed] : [parsed];
      while (queue.length > 0 && jsonLdItems.length < 8) {
        const node = queue.shift();
        if (!node || typeof node !== 'object') continue;
        if (Array.isArray(node['@graph'])) queue.push(...node['@graph']);
        const t = String(node['@type'] || '').toLowerCase();
        if (t.includes('product') || t.includes('offer')) {
          const name = clean(node.name, 180);
          const sku = clean(node.sku || node.mpn || node.productID, 80);
          const brand = clean(typeof node.brand === 'object' ? node.brand?.name : node.brand, 80);
          const desc = clean(node.description, 320);
          let priceStr = '';
          const offers = Array.isArray(node.offers) ? node.offers[0] : node.offers;
          if (offers && typeof offers === 'object') {
            const p = offers.price ?? offers.lowPrice ?? offers.highPrice;
            const c = offers.priceCurrency || 'USD';
            if (p !== undefined && p !== null && String(p).trim() !== '') {
              priceStr = `$${p} ${c}`;
            }
          }
          jsonLdItems.push(
            `[JSON-LD Product] name=${name} | brand=${brand} | sku=${sku} | price=${priceStr} | desc=${desc}`
          );
        }
      }
    } catch (_) {}
  });

  const metaItems = [];
  const metaSelectors = [
    'meta[property="og:title"]',
    'meta[property="og:description"]',
    'meta[property="product:price:amount"]',
    'meta[property="product:price:currency"]',
    'meta[name="description"]',
    'meta[name="twitter:title"]',
    'meta[name="twitter:description"]'
  ];
  metaSelectors.forEach((sel) => {
    const el = document.querySelector(sel);
    if (el && el.content) {
      metaItems.push(`[META ${el.getAttribute('property') || el.getAttribute('name')}] ${clean(el.content, 240)}`);
    }
  });

  const priceTexts = [];
  const priceSelectors = [
    '#corePriceDisplay_desktop_feature_div .a-offscreen',
    '#corePrice_feature_div .a-offscreen',
    '.a-price .a-offscreen',
    '[data-testid*="price"]',
    '[class*="price"]',
    '[aria-label*="$"]',
    '[itemprop="price"]'
  ];
  priceSelectors.forEach((sel) => {
    document.querySelectorAll(sel).forEach((el) => {
      const t = clean(el.innerText || el.textContent || el.getAttribute('content'), 100);
      if (t && /\\$\\s?\\d/.test(t) && !priceTexts.includes(`[DOM Price] ${t}`) && priceTexts.length < 10) {
        priceTexts.push(`[DOM Price] ${t}`);
      }
    });
  });

  const rawVisibleText = clean(document.body ? document.body.innerText : '', 2800);
  if (priceTexts.length === 0 && rawVisibleText) {
    const bodyPrices = rawVisibleText.match(/\\$\\s?\\d{1,4}(?:,\\d{3})*(?:\\.\\d{2})?/g) || [];
    bodyPrices.slice(0, 4).forEach((p) => {
      const entry = `[DOM Price] ${p.trim()}`;
      if (!priceTexts.includes(entry)) priceTexts.push(entry);
    });
  }

  const storefrontMeta = [];
  const prodTitleEl = document.querySelector('#productTitle');
  if (prodTitleEl) storefrontMeta.push(`[Product Title] ${clean(prodTitleEl.innerText, 200)}`);
  const bylineEl = document.querySelector('#bylineInfo');
  if (bylineEl) storefrontMeta.push(`[Brand / Store] ${clean(bylineEl.innerText, 120)}`);
  const asinInput = document.querySelector('input#ASIN') || document.querySelector('[data-asin]');
  if (asinInput) {
    const asinVal = clean(asinInput.value || asinInput.getAttribute('data-asin'), 40);
    if (asinVal) storefrontMeta.push(`[SKU / ASIN] ${asinVal}`);
  }
  const merchantEl = document.querySelector('#merchant-info, #tabular-buybox');
  if (merchantEl) storefrontMeta.push(`[Seller / Fulfillment] ${clean(merchantEl.innerText, 180)}`);

  const candidateLines = [...jsonLdItems, ...storefrontMeta, ...priceTexts, ...metaItems];
  document.querySelectorAll('#feature-bullets li, li, p, [class*="feature"], [class*="benefit"], [class*="spec"]').forEach((el) => {
    if (candidateLines.length >= 18) return;
    const t = clean(el.innerText || el.textContent, 220);
    if (t.length >= 25 && t.length <= 220 && !candidateLines.includes(t)) {
      candidateLines.push(t);
    }
  });

  const combinedExcerpt = [
    ...jsonLdItems,
    ...storefrontMeta,
    ...priceTexts,
    ...metaItems,
    rawVisibleText
  ].join('\\n').slice(0, 3400);

  return {
    url: window.location.href,
    title,
    headings,
    body_excerpt: combinedExcerpt,
    candidate_lines: candidateLines.slice(0, 14)
  };
}
"""


def find_chrome_executable() -> str | None:
    candidates = [
        os.environ.get("CHROME_EXECUTABLE_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def community_extraction_script() -> str:
    for path in (Path(__file__).with_name("community_snapshot.js"),
                 Path(__file__).resolve().parents[1] / "browser_extension" / "community_snapshot.js"):
        if path.is_file():
            return f"({path.read_text(encoding='utf-8')})()"
    raise RuntimeError("Community capture script is missing; update the VM worker bundle.")


def run_worker_loop(
    *,
    portal_url: str,
    passcode: str,
    worker_id: str,
    headed: bool = False,
    cdp_url: str | None = None,
    once: bool = False,
) -> None:
    from playwright.sync_api import sync_playwright

    base_url = portal_url.rstrip("/").strip()
    hostname = socket.gethostname()
    headers = {"Content-Type": "application/json"}
    if passcode and passcode.strip():
        headers["X-PDD-Passcode"] = passcode.strip()

    chrome_exe = find_chrome_executable()
    print(
        f"[WORKER] Starting Windows Native Chrome + Playwright worker id={worker_id} host={hostname} "
        f"portal={base_url} chrome_exe={chrome_exe or 'channel=chrome'} headed={headed}"
    )

    with sync_playwright() as p:
        if cdp_url:
            browser = p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context(
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
        else:
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--window-size=1366,900",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            if not headed:
                # Chrome's modern native headless mode uses the full desktop Chrome binary and TLS/HTTP2 stack
                launch_args.append("--headless=new")
            launch_kwargs: dict[str, Any] = {
                "headless": False if not headed else False,
                "args": launch_args,
                "viewport": {"width": 1366, "height": 900},
                "locale": "en-US",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
            }
            if chrome_exe:
                launch_kwargs["executable_path"] = chrome_exe
            else:
                launch_kwargs["channel"] = "chrome"

            user_data_dir = Path(os.environ.get("TEMP", r"C:\Temp")) / f"pdd-chrome-profile-{worker_id}"
            user_data_dir.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(str(user_data_dir), **launch_kwargs)

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.pages[0] if context.pages else context.new_page()
        browser_version = f"Chrome ({platform.system()} {platform.release()})"
        last_status_code: int | None = None

        def record_navigation_response(response: Any) -> None:
            nonlocal last_status_code
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                last_status_code = response.status

        page.on("response", record_navigation_response)

        with httpx.Client(timeout=35.0, headers=headers) as client:
            last_hb = 0.0
            while True:
                now = time.time()
                if now - last_hb >= 10.0:
                    try:
                        hb_res = client.post(
                            f"{base_url}/api/client-browser/heartbeat",
                            json={
                                "worker_id": worker_id,
                                "hostname": hostname,
                                "browser_version": browser_version,
                                "mode": "windows_vm_playwright_chrome",
                            },
                        )
                        if hb_res.status_code == 200:
                            last_hb = now
                    except Exception as exc:
                        print(f"[WORKER] Heartbeat warning: {exc}")

                try:
                    poll_res = client.get(
                        f"{base_url}/api/client-browser/next-command",
                        params={"worker_id": worker_id, "timeout": "10"},
                    )
                    if poll_res.status_code != 200:
                        time.sleep(2.0)
                        continue
                    data = poll_res.json()
                except Exception as exc:
                    print(f"[WORKER] Poll warning: {exc}")
                    time.sleep(2.0)
                    continue

                if not data.get("has_command") or not data.get("command"):
                    if once:
                        break
                    continue

                cmd = data["command"]
                run_id = str(cmd.get("run_id", ""))
                command_id = str(cmd.get("command_id", ""))
                action = str(cmd.get("action") or "observe")
                target_url = str(cmd.get("url") or "")
                scroll_px = int(cmd.get("scroll_pixels") or 0)
                capture_screenshot = bool(cmd.get("capture_screenshot", True))

                print(f"[WORKER] Executing command={command_id} action={action} url={target_url}")
                error_msg: str | None = None
                try:
                    if action == "goto" and target_url:
                        last_status_code = None
                        resp = page.goto(target_url, wait_until="domcontentloaded", timeout=26000)
                        if last_status_code is None:
                            last_status_code = resp.status if resp is not None else None
                        page.wait_for_timeout(2000)
                        # Handle non-CAPTCHA storefront splash button (e.g., Amazon "Continue shopping")
                        cont_btn = page.locator("button:has-text('Continue shopping'), input[value*='Continue shopping']")
                        if not cmd.get("capture_community") and cont_btn.count() > 0:
                            try:
                                cont_btn.first.click(timeout=3000)
                                page.wait_for_timeout(2000)
                            except Exception:
                                pass
                    elif action == "scroll_down":
                        px = scroll_px if scroll_px > 0 else 700
                        page.evaluate(f"window.scrollBy({{ top: {px}, left: 0, behavior: 'smooth' }});")
                        page.wait_for_timeout(1000)
                    else:
                        if target_url and (not page.url or page.url == "about:blank"):
                            last_status_code = None
                            resp = page.goto(target_url, wait_until="domcontentloaded", timeout=26000)
                            if last_status_code is None:
                                last_status_code = resp.status if resp is not None else None
                            page.wait_for_timeout(2000)
                except Exception as nav_exc:
                    error_msg = str(nav_exc)[:300]

                extracted: dict[str, Any] = {}
                community: dict[str, Any] = {}
                try:
                    extracted = page.evaluate(EXTRACTION_JS) or {}
                    if cmd.get("capture_community"):
                        community = page.evaluate(community_extraction_script()) or {}
                except Exception as eval_exc:
                    if not error_msg:
                        error_msg = str(eval_exc)[:240]

                screenshot_data_url: str | None = None
                if capture_screenshot:
                    try:
                        png_bytes = page.screenshot(type="png", full_page=False)
                        b64_png = base64.b64encode(png_bytes).decode("ascii")
                        screenshot_data_url = f"data:image/png;base64,{b64_png}"
                    except Exception as shot_exc:
                        print(f"[WORKER] Screenshot warning: {shot_exc}")

                obs_payload = {
                    "run_id": run_id,
                    "command_id": command_id,
                    "url": str(extracted.get("url") or page.url or target_url),
                    "title": str(extracted.get("title") or ""),
                    "status_code": last_status_code,
                    "headings": list(extracted.get("headings") or []),
                    "body_excerpt": str(extracted.get("body_excerpt") or ""),
                    "candidate_lines": list(extracted.get("candidate_lines") or []),
                    "screenshot_data_url": screenshot_data_url,
                    "error": error_msg,
                    "worker_label": f"Live Windows VM Native Chrome + Playwright ({hostname})",
                    **community,
                }
                try:
                    obs_res = client.post(f"{base_url}/api/client-browser/observation", json=obs_payload)
                    print(
                        f"[WORKER] Posted observation command={command_id} status={obs_res.status_code} "
                        f"title={obs_payload['title'][:60]!r} shot={bool(screenshot_data_url)}"
                    )
                except Exception as post_exc:
                    print(f"[WORKER] Failed to post observation: {post_exc}")

                if once:
                    break


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Windows Native Chrome + Playwright Worker for PDD Portal.")
    parser.add_argument(
        "--portal-url",
        default=os.environ.get(
            "PDD_PORTAL_URL",
            "",
        ),
    )
    parser.add_argument("--passcode", default=os.environ.get("PDD_DEMO_PASSCODE", ""))
    parser.add_argument("--worker-id", default=f"win-vm-{socket.gethostname().lower()}")
    parser.add_argument("--headed", action="store_true", help="Launch visible desktop Chrome window (for RDP session)")
    parser.add_argument("--cdp-url", default=None, help="Optional existing Chrome CDP endpoint (e.g. http://127.0.0.1:9222)")
    parser.add_argument("--once", action="store_true", help="Process one command poll cycle and exit")
    args = parser.parse_args()
    if not args.portal_url.strip():
        parser.error("Set PDD_PORTAL_URL or --portal-url to your portal's HTTPS origin.")
    target = urlsplit(args.portal_url.strip().rstrip("/"))
    loopback = target.scheme == "http" and target.hostname in {"127.0.0.1", "localhost", "::1"}
    if (not target.hostname or (target.scheme != "https" and not loopback) or target.username or target.password
            or target.path or target.query or target.fragment):
        parser.error("Use an HTTPS portal origin, or literal loopback HTTP for local development.")

    run_worker_loop(
        portal_url=args.portal_url,
        passcode=args.passcode,
        worker_id=args.worker_id,
        headed=args.headed,
        cdp_url=args.cdp_url,
        once=args.once,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
