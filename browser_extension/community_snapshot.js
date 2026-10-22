function captureCommunitySnapshot() {
  const clean = (value, limit = 1200) => String(value || "").replace(/\s+/g, " ").trim().slice(0, limit);
  const rendered = (element) => {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && element.getClientRects().length > 0;
  };
  const inViewport = (element) => {
    const rect = element.getBoundingClientRect();
    return rendered(element) && rect.bottom > 0 && rect.top < innerHeight && rect.right > 0 && rect.left < innerWidth;
  };
  const selectors = [
    "shreddit-comment", ".comment .md", "ytd-comment-thread-renderer #content-text",
    "ytd-comment-view-model #content-text", '[data-hook="review-body"]',
    ".message-body", ".messageContent", ".postbody", ".cooked", ".comment-body", ".commtext"
  ];
  const discussions = [];
  const seen = new Set();
  for (const element of document.querySelectorAll(selectors.join(","))) {
    if (!inViewport(element)) continue;
    const text = clean(element.innerText);
    if (text.length < 25 || seen.has(text)) continue;
    seen.add(text);
    discussions.push(text);
    if (discussions.length >= 12) break;
  }
  const viewportLines = [];
  const viewportSeen = new Set();
  for (const element of document.querySelectorAll("h1,h2,h3,p,li,td.comment,div[slot='text-body']")) {
    if (!inViewport(element)) continue;
    const text = clean(element.innerText, 600);
    if (text.length < 10 || viewportSeen.has(text)) continue;
    viewportSeen.add(text);
    viewportLines.push(text);
    if (viewportLines.join("\n").length >= 3200) break;
  }
  const links = [];
  const seenLinks = new Set();
  const anchors = Array.from(document.querySelectorAll("a[href]")).filter(rendered);
  anchors.sort((a, b) => Number(inViewport(b)) - Number(inViewport(a)));
  for (const anchor of anchors) {
    const url = anchor.href;
    const text = clean(anchor.innerText || anchor.getAttribute("aria-label"), 180);
    if (!url.startsWith("https://") || url.length > 2048 || !text || seenLinks.has(url)) continue;
    seenLinks.add(url);
    links.push({ url, text });
    if (links.length >= 60) break;
  }
  const root = document.scrollingElement || document.documentElement;
  return {
    community_capture_status: "ready",
    viewport_text: viewportLines.join("\n").slice(0, 3200),
    discussion_blocks: discussions,
    observed_links: links,
    scroll_y: Math.round(root.scrollTop),
    at_page_end: root.scrollTop + innerHeight >= root.scrollHeight - 10
  };
}
