"""Loopback-only archive usability and escaped-evidence checks; no paid research."""
from __future__ import annotations

import io
import json
import sys
from urllib.parse import urlparse
import zipfile

import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    origin = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
    if urlparse(origin).hostname != "127.0.0.1":
        raise ValueError("This check only targets the loopback preview.")
    with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=60) as client:
        result = client.post("/api/run", json={"product_url": "https://electronics.sony.com/audio/headphones/headband/p/wh1000xm6-b",
                                              "user_intent": "Illustrative archive browser check", "execution_mode": "sample"})
        result.raise_for_status()
        events = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith("data: ")]
        saved = next(event for event in events if event.get("type") == "evidence_archive" and event.get("status") == "saved")
        identifier = saved["archive_id"]
        api = "/api/saved-research/" + identifier
        manifest = client.get(api).raise_for_status().json()
        assert manifest["retention"] == "until_explicit_deletion" and not manifest["screenshots"]
        response = client.get(api + "/export").raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
            assert {"research.json", "report.md"} <= set(bundle.namelist())
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page()
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append((request.method, request.url)))
            page.goto(origin + saved["url"], wait_until="networkidle")
            expect(page.locator("#research")).to_be_visible()
            expect(page.locator("#research")).to_contain_text("Genuine browser screenshots (0)")
            expect(page.locator("#research")).to_contain_text("generated illustrations are not substituted")
            expect(page.get_by_text("anyone with the portal passcode", exact=False)).to_be_visible()
            expect(page.get_by_role("link", name="Download evidence + screenshots")).to_have_attribute("href", api + "/export")
            assert not any("/api/run" in url for _, url in requests)
            for width in (320, 375, 768, 1280):
                page.set_viewport_size({"width": width, "height": 900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), width
            fixture = dict(manifest)
            fixture["user_intent"] = "<img src=x onerror=window.archiveInjected=true>"
            fixture["report_markdown"] = "<script>window.archiveInjected=true</script>"
            fixture["observations"] = [{"url": "javascript:alert(1)", "status": "synthetic fixture",
                                        "retrieved_response": {"content": "<img src=x onerror=window.archiveInjected=true>"}}]
            page.route("**" + api, lambda route: route.fulfill(status=200, json=fixture) if route.request.method == "GET" else route.continue_())
            page.reload(wait_until="networkidle")
            expect(page.locator("#research h2").first).to_have_text(fixture["user_intent"])
            assert page.locator("#research script, #research img").count() == 0
            assert page.locator('#research a[href^="javascript:"]').count() == 0
            assert page.evaluate("window.archiveInjected === undefined")
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Delete saved research").click()
            expect(page.locator("#status")).to_have_text("Saved research and its archived screenshot objects were deleted.")
            assert client.get(api).status_code == 404
            assert not errors, errors
            browser.close()
        print(json.dumps({"archive_browser": "passed", "responsive_widths": [320, 375, 768, 1280],
                          "xss": "escaped", "export_and_explicit_delete": "verified", "paid_research": False}))


if __name__ == "__main__":
    main()
