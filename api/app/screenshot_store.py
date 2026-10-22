"""In-memory store for generated SVG stage summary cards (`RuntimeArtifactStore`).

Explicitly labels every generated card with:
  - `capture_kind = "generated_svg_illustration"`
  - `is_live_browser_capture = False`
so neither the UI nor API consumers ever mistake generated SVG cards for live
browser viewport screenshots.
"""
from __future__ import annotations

import hashlib
import html
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MAX_RUNTIME_SCREENSHOTS = 100


@dataclass(frozen=True)
class ScreenshotRecord:
    artifact_id: str
    run_id: str
    sequence: int
    stage: str
    title: str
    url: str
    caption: str
    media_type: str
    data: bytes
    sha256: str
    captured_at: str
    capture_kind: str = "generated_svg_illustration"
    is_live_browser_capture: bool = False
    custom_provenance_label: str | None = None

    def metadata(self) -> dict[str, Any]:
        provenance_label = self.custom_provenance_label or (
            "Live BAT Browser Viewport Capture (Playwright Workspace CDP)"
            if self.is_live_browser_capture
            else "Generated SVG Illustration (Not a live browser capture)"
        )
        return {
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "stage": self.stage,
            "title": self.title,
            "url": self.url,
            "caption": self.caption,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "captured_at": self.captured_at,
            "content_url": f"/api/screenshots/{self.artifact_id}",
            "capture_kind": self.capture_kind,
            "is_live_browser_capture": self.is_live_browser_capture,
            "provenance_label": provenance_label,
        }


class RuntimeArtifactStore:
    def __init__(self, max_items: int = MAX_RUNTIME_SCREENSHOTS) -> None:
        self._max_items = max_items
        self._records: OrderedDict[str, ScreenshotRecord] = OrderedDict()
        self._active_runs: set[str] = set()

    def start_run(self, run_id: str) -> None:
        self._active_runs.add(run_id)

    def finish_run(self, run_id: str) -> None:
        self._active_runs.discard(run_id)

    def record_capture(
        self,
        *,
        run_id: str,
        sequence: int,
        stage: str,
        title: str,
        url: str,
        caption: str,
        data: bytes,
        media_type: str = "image/svg+xml",
        capture_kind: str = "generated_svg_illustration",
        is_live_browser_capture: bool = False,
        provenance_label: str | None = None,
    ) -> ScreenshotRecord:
        artifact_id = f"cap-{uuid.uuid4().hex[:12]}"
        digest = hashlib.sha256(data).hexdigest()
        record = ScreenshotRecord(
            artifact_id=artifact_id,
            run_id=run_id,
            sequence=sequence,
            stage=stage,
            title=title,
            url=url,
            caption=caption,
            media_type=media_type,
            data=data,
            sha256=digest,
            captured_at=datetime.now(timezone.utc).isoformat(),
            capture_kind=capture_kind,
            is_live_browser_capture=is_live_browser_capture,
            custom_provenance_label=provenance_label,
        )
        self._records[artifact_id] = record
        while len(self._records) > self._max_items:
            self._records.popitem(last=False)
        return record

    def get(self, artifact_id: str) -> ScreenshotRecord | None:
        return self._records.get(artifact_id)

    def list_for_run(self, run_id: str) -> list[ScreenshotRecord]:
        return [rec for rec in self._records.values() if rec.run_id == run_id]


def render_evidence_svg(
    *,
    stage_badge: str,
    title: str,
    url: str,
    highlights: list[str],
    status_color: str = "#0284c7",
    footer_note: str = "Generated SVG Stage Illustration (Not a live browser screenshot)",
) -> bytes:
    esc_badge = html.escape(stage_badge[:65])
    esc_title = html.escape(title[:85])
    esc_url = html.escape(url[:95])
    esc_footer = html.escape(footer_note[:105])

    bullet_lines: list[str] = []
    y = 168
    for line in highlights[:4]:
        esc_line = html.escape(str(line)[:98])
        bullet_lines.append(
            f'<circle cx="44" cy="{y - 5}" r="4.5" fill="{status_color}" />'
            f'<text x="60" y="{y}" font-family="Segoe UI, Inter, sans-serif" font-size="14" fill="#1e293b">{esc_line}</text>'
        )
        y += 34

    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 820 340" width="100%" height="100%">
  <rect width="820" height="340" rx="14" fill="#f8fafc" stroke="#cbd5e1" stroke-width="2"/>
  <rect x="0" y="0" width="820" height="46" rx="14" fill="#f1f5f9"/>
  <text x="24" y="29" font-family="Segoe UI, Inter, monospace" font-size="12" font-weight="700" fill="{status_color}">{esc_badge}</text>
  <text x="540" y="29" font-family="Segoe UI, Inter, sans-serif" font-size="11" font-weight="600" fill="#64748b">SVG Illustration • Not Live Browser Capture</text>
  <text x="28" y="86" font-family="Segoe UI, Inter, sans-serif" font-size="19" font-weight="700" fill="#0f172a">{esc_title}</text>
  <text x="28" y="114" font-family="Consolas, monospace" font-size="12" fill="#475569">{esc_url}</text>
  <line x1="28" y1="130" x2="792" y2="130" stroke="#e2e8f0" stroke-width="1.5"/>
  {''.join(bullet_lines)}
  <rect x="24" y="292" width="772" height="32" rx="6" fill="#eff6ff" stroke="#bfdbfe"/>
  <text x="38" y="313" font-family="Segoe UI, Inter, sans-serif" font-size="12" font-weight="600" fill="#1e40af">{esc_footer}</text>
</svg>"""
    return svg.encode("utf-8")


SCREENSHOT_STORE = RuntimeArtifactStore()
