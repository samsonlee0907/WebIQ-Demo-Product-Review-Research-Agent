# Desktop browser bridge

This Manifest V3 extension connects the research portal to a desktop Chrome/Edge profile for read-only page observations, discussion snapshots and genuine PNG viewport captures. It is not a browser embedded in the API container and does not run inside an app's preview panel.

## Install

1. Use the complete `browser_extension` folder, or download `/api/chrome-extension.zip` while signed in to your portal and unzip it.
2. For a hosted portal, edit `manifest.json` before installation: add **your exact HTTPS portal host** to `content_scripts[0].matches`. The repository template trusts loopback only.
3. Open `chrome://extensions/` or `edge://extensions/`, enable **Developer mode**, select **Load unpacked**, and choose the folder containing `manifest.json`.
4. Keep that folder in place. Refresh the portal in the same desktop browser/profile, select the Local method, and confirm the connection badge.

Example **placeholder** hosted match:

```json
"matches": [
  "https://your-portal.example/*",
  "http://127.0.0.1/*",
  "http://localhost/*"
]
```

Replace the example host with the real one. Do not trust all HTTPS pages or every app on a shared cloud domain as command sources. `host_permissions` allows the bridge to visit research targets; `content_scripts.matches` controls which portal pages can send commands. These serve different purposes.

Use a dedicated browser profile. A normal profile can expose account-specific site state; installing this extension is not evidence that a source is public, trustworthy or permitted for collection.

## Update

Replace the complete loaded folder, including `community_snapshot.js`. Restore/review your exact hosted-origin match, click **Reload** on the extension card, then refresh the portal. Do not load duplicate copies.

Site denials, sign-in walls and read-only limits remain enforced. Dynamic comments may not load. The pipeline does not silently switch inspectors or claim that comments are authentic.

## VM distinction

The VM path uses `scripts\windows_playwright_worker.py` with an adjacent `community_snapshot.js`, not this extension. If you deliberately use the Local method from a VM desktop browser, install the extension there using the same steps.
