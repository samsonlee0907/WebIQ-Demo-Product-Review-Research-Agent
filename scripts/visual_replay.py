"""Capture permitted browser evidence privately; never write WebIQ-grounded text to disk."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

PRIVATE_ROOT = Path(__file__).resolve().parents[1] / "api" / "private_replay"


class VisualRecorder:
    def __init__(self, method: str, secret: str) -> None:
        self.method, self.secret = method, secret
        self.saved: dict[str, Any] = {"method": method, "sources": [], "screenshots": []}
        self.temporary: dict[str, Any] = {}

    def text(self, value: Any, limit: int = 200) -> str:
        text = str(value or "")
        return (text.replace(self.secret, "[REDACTED]") if self.secret else text)[:limit]

    def capture(self, event: dict[str, Any], client: Any) -> None:
        from scripts.refresh_benchmark_report import public_url
        kind = event.get("type")
        if kind == "product_baseline":
            baseline = event.get("baseline") or {}
            if baseline.get("provider") == "webiq" or self.method in ("webiq_browse", "search_only"):
                return
            fields = {key: baseline.get(key) for key in ("brand", "model", "sku", "variant", "price", "currency", "seller", "data_provenance")}
            fields = {key: self.text(value, 200) if isinstance(value, str) else value for key, value in fields.items()}
            fields["claims"] = [self.text(claim, 160) for claim in (baseline.get("seller_claims") or [])[:6]]
            fields["limitation"] = self.text(baseline.get("limitation_notice"), 400)
            self.saved["baseline"] = fields
        elif kind == "browsed_sources":
            rows = [{
                "url": public_url(source.get("requested_url") or source.get("url")),
                "final_url": public_url(source.get("url")),
                "status": source.get("access_status"), "fetched_characters": source.get("fetched_characters", 0),
                "excerpt": self.text(source.get("observed_excerpt")),
                "reason": self.text(source.get("restriction_reason"), 300),
                "inspection_method": source.get("inspection_method") or self.method,
                "screenshot_artifact_id": source.get("screenshot_artifact_id"),
                "verification_attempts": source.get("verification_attempts", 0),
                "verification_outcome": source.get("verification_outcome", "not_needed"),
                "agent_explored": bool(source.get("agent_explored")),
                "discovery_origin": self.text(source.get("discovery_origin"), 60),
                "parent_url": public_url(source["parent_url"]) if source.get("parent_url") else "",
                "discussion_blocks_observed": int(source.get("discussion_blocks_observed") or 0),
                "exploration_steps": [{
                    "step": step.get("step"), "action": self.text(step.get("action"), 30),
                    "goal": self.text(step.get("goal"), 200), "reason": self.text(step.get("reason"), 300),
                    "status": self.text(step.get("status"), 60),
                    "screenshot_artifact_id": step.get("screenshot_artifact_id"),
                    "viewport_text": self.text(step.get("viewport_text"), 3200),
                    "visible_discussion_blocks": [self.text(text, 1200) for text in
                                                  (step.get("visible_discussion_blocks") or [])[:12]],
                } for step in (source.get("exploration_steps") or [])[:12]],
                "retrieved_response": {
                    "url": public_url(source.get("url")), "title": self.text(source.get("title"), 180),
                    "content": self.text((source.get("retrieved_response") or {}).get("content"), 3200),
                    "status": source.get("access_status"),
                    "inspection_method": source.get("inspection_method") or self.method,
                    "verification_attempts": source.get("verification_attempts", 0),
                    "verification_outcome": source.get("verification_outcome", "not_needed"),
                },
            } for source in event.get("sources") or []
                if source.get("inspection_method", self.method) != "webiq_browse"
                and source.get("provider") != "webiq" and self.method not in ("webiq_browse", "search_only")]
            self.saved["sources"] = rows
        elif kind == "screenshot":
            shot = event.get("screenshot") or {}
            if not shot.get("is_live_browser_capture"):
                return
            path = shot.get("content_url") or ""
            if not re.fullmatch(r"/api/screenshots/cap-[a-f0-9]{12}", path):
                raise ValueError("Invalid screenshot artifact path.")
            response = client.get(path)
            response.raise_for_status()
            data = response.content
            digest = hashlib.sha256(data).hexdigest()
            if not data.startswith(b"\x89PNG\r\n\x1a\n") or digest != shot.get("sha256"):
                raise ValueError("Screenshot bytes do not match the genuine PNG capture hash.")
            folder = PRIVATE_ROOT / "assets"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{digest}.png").write_bytes(data)
            self.saved["screenshots"].append({
                "url": public_url(shot.get("url")), "stage": shot.get("stage"),
                "title": self.text(shot.get("title"), 160), "captured_at": shot.get("captured_at"),
                "sha256": digest, "asset": f"/api/replay/assets/{digest}.png",
                "artifact_id": shot.get("artifact_id"),
            })
        elif kind == "community_exploration" and event.get("phase") == "decision":
            self.saved.setdefault("community_actions", []).append({
                "step": event.get("step"), "action": self.text(event.get("action"), 30),
                "goal": self.text(event.get("goal"), 200), "reason": self.text(event.get("reason"), 300),
                "status": self.text(event.get("status"), 60),
                "url": public_url(event.get("url")), "domain": self.text(event.get("domain"), 253),
            })
        elif kind == "claim_matrix":
            if event.get("synthesis_status") != "complete":
                return
            self.temporary["synthesis"] = {
                "summary": self.text(event.get("executive_summary"), 1800),
                "recommendation": self.text(event.get("recommendation_badge"), 160),
                "gaps": [self.text(gap, 250) for gap in (event.get("evidence_gaps") or [])[:15]],
            }

    def finish(self, run: dict[str, Any], client: Any) -> dict[str, Any]:
        run_id = run["run_id"]
        if not re.fullmatch(r"[a-f0-9]{12}", run_id):
            raise ValueError("Invalid run identifier.")
        self.saved["run_id"] = run_id
        PRIVATE_ROOT.mkdir(parents=True, exist_ok=True)
        # Only generated briefs may outlive the workflow in RAM; never raw WebIQ grounding.
        (PRIVATE_ROOT / f"run-{run_id}.json").write_text(json.dumps(self.saved, ensure_ascii=True, indent=2), encoding="utf-8")
        run["visual_evidence_id"] = run_id
        payload = {"run_id": run_id, "expires_at": time.time() + 900, "evidence": self.temporary}
        response = client.post("/api/replay/transient", json=payload)
        response.raise_for_status()
        return payload
