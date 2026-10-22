/**
 * Product Due-Diligence Assistant — MV3 Background Service Worker.
 *
 * Executes read-only browser commands (`goto`, `scroll_down`, `observe`, `close`)
 * in a dedicated tab inside the user's open Chrome window, captures live PNG
 * viewport screenshots via `chrome.tabs.captureVisibleTab`, and extracts
 * visible DOM + JSON-LD Product/Offer metadata.
 */

importScripts("community_snapshot.js");

const runSessions = new Map(); // run_id -> { agentTabId, windowId, portalTabId, lastUrl }

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function waitForTabComplete(tabId, timeoutMs = 15000) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId === tabId && changeInfo.status === "complete") {
        finish();
      }
    };
    const timer = setTimeout(finish, timeoutMs);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError || (tab && tab.status === "complete")) {
        finish();
      }
    });
  });
}

function extractPageObservationInTab() {
  const headings = Array.from(document.querySelectorAll("h1, h2, h3"))
    .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, 12);

  const structuredLines = [];
  // Extract JSON-LD Product / Offer structured data when present on retailer pages
  try {
    const ldScripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
    for (const s of ldScripts) {
      const parsed = JSON.parse(s.textContent || "{}");
      const items = Array.isArray(parsed)
        ? parsed
        : parsed["@graph"] && Array.isArray(parsed["@graph"])
        ? parsed["@graph"]
        : [parsed];
      for (const item of items) {
        if (!item || typeof item !== "object") continue;
        const t = String(item["@type"] || "").toLowerCase();
        if (t.includes("product")) {
          if (item.name) structuredLines.push(`Product Name: ${item.name}`);
          if (item.brand) {
            const b = typeof item.brand === "object" ? item.brand.name : item.brand;
            if (b) structuredLines.push(`Brand: ${b}`);
          }
          if (item.sku) structuredLines.push(`SKU: ${item.sku}`);
          if (item.mpn) structuredLines.push(`Model: ${item.mpn}`);
          if (item.description) {
            structuredLines.push(`Description: ${String(item.description).replace(/\s+/g, " ").slice(0, 280)}`);
          }
          const offers = Array.isArray(item.offers)
            ? item.offers
            : item.offers
            ? [item.offers]
            : [];
          for (const off of offers.slice(0, 3)) {
            if (!off || typeof off !== "object") continue;
            const price = off.price || off.lowPrice || (off.priceSpecification && off.priceSpecification.price);
            const curr = off.priceCurrency || "USD";
            if (price !== undefined && price !== null && String(price).trim() !== "") {
              structuredLines.push(`Observed Offer Price: $${price} ${curr}`);
            }
            if (off.availability) {
              structuredLines.push(`Availability: ${off.availability}`);
            }
          }
        }
      }
    }
  } catch (_err) {
    // Ignore malformed JSON-LD blocks
  }

  // Extract OpenGraph / product meta tags
  try {
    const metaSelectors = [
      'meta[property="og:title"]',
      'meta[property="og:description"]',
      'meta[property="product:price:amount"]',
      'meta[name="twitter:data1"]',
      'meta[name="description"]',
    ];
    for (const sel of metaSelectors) {
      const el = document.querySelector(sel);
      if (el && el.content) {
        const label = el.getAttribute("property") || el.getAttribute("name") || "meta";
        structuredLines.push(`${label}: ${el.content.replace(/\s+/g, " ").trim().slice(0, 220)}`);
      }
    }
  } catch (_err) {
    // Ignore meta extraction issues
  }

  // Extract Amazon & Official Store DOM price / title / ASIN / seller / feature bullets
  try {
    const prodTitle = document.querySelector("#productTitle, #title, h1[data-testid*='title']");
    if (prodTitle && prodTitle.textContent) {
      structuredLines.push(`[DOM Title] ${prodTitle.textContent.replace(/\s+/g, " ").trim().slice(0, 220)}`);
    }
    const byline = document.querySelector("#bylineInfo, a#brand");
    if (byline && byline.textContent) {
      structuredLines.push(`[DOM Brand] ${byline.textContent.replace(/\s+/g, " ").trim().slice(0, 120)}`);
    }
    const asinInput = document.querySelector("input#ASIN, [data-asin]");
    if (asinInput) {
      const asin = asinInput.value || asinInput.getAttribute("data-asin") || "";
      if (asin) structuredLines.push(`[DOM SKU/ASIN] ${asin}`);
    }
    const sellerEl = document.querySelector("#merchant-info, #tabular-buybox, #fulfillerInfoFeature_feature_div");
    if (sellerEl && sellerEl.textContent) {
      structuredLines.push(`[DOM Seller] ${sellerEl.textContent.replace(/\s+/g, " ").trim().slice(0, 180)}`);
    }
    const priceEls = document.querySelectorAll(
      "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, #corePrice_feature_div .a-price .a-offscreen, .a-price .a-offscreen, [data-price], .price, [class*='price' i], [class*='Price']"
    );
    const seenPrices = new Set();
    for (const el of Array.from(priceEls).slice(0, 10)) {
      const txt = (el.textContent || "").replace(/\s+/g, " ").trim();
      if (txt && txt.includes("$") && txt.length <= 45 && !seenPrices.has(txt)) {
        seenPrices.add(txt);
        structuredLines.push(`[DOM Price] ${txt}`);
      }
    }
    const bullets = Array.from(document.querySelectorAll("#feature-bullets li, #productFactsDesktopExpander li"))
      .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
      .filter((t) => t.length >= 15 && t.length <= 240)
      .slice(0, 6);
    for (const b of bullets) {
      structuredLines.push(`[Feature Bullet] ${b}`);
    }
  } catch (_err) {
    // Ignore DOM selector errors
  }

  const candidateLines = [
    ...structuredLines.slice(0, 8),
    ...Array.from(document.querySelectorAll("li, p, tr, dd"))
      .map((el) => (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim())
      .filter((t) => t.length >= 25 && t.length <= 220)
      .slice(0, 14),
  ].slice(0, 16);

  const visibleBody = document.body
    ? (document.body.innerText || "").replace(/[ \t]+/g, " ").slice(0, 2600)
    : "";
  const combinedExcerpt = [structuredLines.join("\n"), visibleBody].filter(Boolean).join("\n\n").slice(0, 3200);

  return {
    url: location.href,
    title: document.title || "",
    headings,
    body_excerpt: combinedExcerpt,
    candidate_lines: candidateLines,
  };
}

async function handleBrowserCommand(cmd, sender) {
  const runId = cmd.run_id;
  const commandId = cmd.command_id;
  const action = cmd.action || "observe";
  const targetUrl = cmd.url || "";
  const portalTabId = sender && sender.tab ? sender.tab.id : null;
  const portalWindowId = sender && sender.tab ? sender.tab.windowId : undefined;

  if (action === "close") {
    const session = runSessions.get(runId);
    runSessions.delete(runId);
    if (session) {
      if (session.portalTabId) {
        try {
          await chrome.tabs.update(session.portalTabId, { active: true });
        } catch (_e) {}
      }
      if (session.agentTabId) {
        try {
          await chrome.tabs.remove(session.agentTabId);
        } catch (_e) {}
      }
    }
    return {
      run_id: runId,
      command_id: commandId,
      url: targetUrl,
      title: "Closed",
      status_code: 200,
      headings: [],
      body_excerpt: "",
      candidate_lines: [],
      screenshot_data_url: null,
      error: null,
    };
  }

  let session = runSessions.get(runId);
  if (session && session.agentTabId) {
    try {
      await chrome.tabs.get(session.agentTabId);
    } catch (_e) {
      session = null;
    }
  }

  try {
    if (!session) {
      const createdTab = await chrome.tabs.create({
        windowId: portalWindowId,
        url: targetUrl || "about:blank",
        active: true,
      });
      session = {
        agentTabId: createdTab.id,
        windowId: createdTab.windowId,
        portalTabId,
        lastUrl: targetUrl,
      };
      runSessions.set(runId, session);
      await waitForTabComplete(session.agentTabId, 16000);
      await sleep(1800);
    } else if (action === "goto" && targetUrl) {
      await chrome.tabs.update(session.agentTabId, { url: targetUrl, active: true });
      session.lastUrl = targetUrl;
      await waitForTabComplete(session.agentTabId, 16000);
      await sleep(1800);
    } else if (action === "scroll_down") {
      const pixels = Number(cmd.scroll_pixels || 750);
      await chrome.tabs.update(session.agentTabId, { active: true });
      await chrome.scripting.executeScript({
        target: { tabId: session.agentTabId },
        func: (px) => {
          window.scrollBy({ top: px, left: 0, behavior: "smooth" });
        },
        args: [pixels],
      });
      await sleep(1100);
    } else {
      await chrome.tabs.update(session.agentTabId, { active: true });
      await sleep(400);
    }

    // Ensure the agent tab is active so captureVisibleTab screenshots it cleanly
    const currentTab = await chrome.tabs.update(session.agentTabId, { active: true });
    await sleep(250);

    let screenshotDataUrl = null;
    if (cmd.capture_screenshot !== false) {
      try {
        screenshotDataUrl = await chrome.tabs.captureVisibleTab(currentTab.windowId, {
          format: "png",
        });
      } catch (_capErr) {
        screenshotDataUrl = null;
      }
    }

    let extracted = {
      url: currentTab.url || targetUrl,
      title: currentTab.title || "",
      headings: [],
      body_excerpt: "",
      candidate_lines: [],
    };
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: session.agentTabId },
        func: extractPageObservationInTab,
      });
      if (results && results[0] && results[0].result) {
        extracted = results[0].result;
      }
    } catch (_domErr) {
      // Fallback to tab title/url if scripting is blocked on a special page
    }

    return {
      run_id: runId,
      command_id: commandId,
      url: extracted.url || targetUrl,
      title: extracted.title || "",
      status_code: 200,
      headings: extracted.headings || [],
      body_excerpt: extracted.body_excerpt || "",
      candidate_lines: extracted.candidate_lines || [],
      ...(cmd.capture_community ? (await chrome.scripting.executeScript({
        target: { tabId: session.agentTabId }, func: captureCommunitySnapshot,
      }))[0].result : {}),
      screenshot_data_url: screenshotDataUrl,
      error: null,
    };
  } catch (err) {
    return {
      run_id: runId,
      command_id: commandId,
      url: targetUrl,
      title: "",
      status_code: 0,
      headings: [],
      body_excerpt: "",
      candidate_lines: [],
      screenshot_data_url: null,
      error: err && err.message ? err.message : String(err),
    };
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "PDD_EXEC_BROWSER_COMMAND" || !message.command) {
    return false;
  }
  handleBrowserCommand(message.command, sender)
    .then((observation) => {
      sendResponse({ observation });
    })
    .catch((err) => {
      sendResponse({
        observation: {
          run_id: message.command.run_id,
          command_id: message.command.command_id,
          url: message.command.url || "",
          title: "",
          status_code: 0,
          headings: [],
          body_excerpt: "",
          candidate_lines: [],
          screenshot_data_url: null,
          error: err && err.message ? err.message : String(err),
        },
      });
    });
  return true; // Keep message channel open for async sendResponse
});
