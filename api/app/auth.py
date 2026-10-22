"""Passcode authentication, signed session cookies, CSRF validation, and rate/concurrency guards.

Designed for the temporary passcode-protected Azure Container Apps demo deployment:
  - Fail-closed startup/runtime when `PDD_HOSTED_MODE=true` or `PDD_REQUIRE_PASSCODE=true` and
    auth secrets (`PDD_PASSCODE_SHA256` / `PDD_DEMO_PASSCODE` and `PDD_SESSION_SIGNING_SECRET`) are missing.
  - Constant-time passcode verification via `hmac.compare_digest` over SHA-256 digests.
  - Signed `HttpOnly` session cookies (`pdd_session`) with expiration (`exp`), issued-at (`iat`),
    and per-session CSRF token (`csrf`).
  - State-changing endpoint CSRF protection (`X-CSRF-Token` + Origin/Referer/Sec-Fetch-Site checks).
  - Bounded login rate limiting keyed by verified client IP (using only the rightmost ACA-appended
    `X-Forwarded-For` entry when behind trusted Azure Container Apps ingress).
  - Bounded run concurrency guard so concurrent SSE runs cannot exhaust single-replica resources.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

try:
    from .config import (
        PDD_MAX_CONCURRENT_LIVE_RUNS,
        PDD_MAX_CONCURRENT_RUNS,
        get_allowed_hosts,
        get_allowed_origins,
        is_hosted_mode,
    )
except ImportError:
    from config import (
        PDD_MAX_CONCURRENT_LIVE_RUNS,
        PDD_MAX_CONCURRENT_LIVE_RUNS,
        PDD_MAX_CONCURRENT_RUNS,
        get_allowed_hosts,
        get_allowed_origins,
        is_hosted_mode,
    )

SESSION_COOKIE_NAME = "pdd_session"
CSRF_HEADER_NAME = "x-csrf-token"
DEFAULT_SESSION_TTL_SECONDS = 28800  # 8 hours
LOGIN_WINDOW_SECONDS = 300.0  # 5 minutes
MAX_FAILED_LOGIN_ATTEMPTS = 5

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost", "testclient", "testserver"})

# In-memory login attempt tracker: ip -> list of failure timestamps
_LOGIN_FAILURES: dict[str, list[float]] = {}

# Concurrency counters for expensive SSE runs
_ACTIVE_TOTAL_RUNS = 0
_ACTIVE_LIVE_RUNS = 0
_RUN_LOCK = asyncio.Lock()


def reset_security_state_for_testing() -> None:
    """Clear login rate-limit counters and run concurrency state between tests."""
    global _ACTIVE_TOTAL_RUNS, _ACTIVE_LIVE_RUNS
    _LOGIN_FAILURES.clear()
    _ACTIVE_TOTAL_RUNS = 0
    _ACTIVE_LIVE_RUNS = 0


def is_passcode_required() -> bool:
    """Return True if passcode authentication is required (always True in hosted mode)."""
    if is_hosted_mode():
        return True
    return os.environ.get("PDD_REQUIRE_PASSCODE", "").strip().lower() in {"1", "true", "yes"}


def _get_expected_passcode_sha256() -> str:
    """Return the expected lowercase 64-hex-char SHA-256 digest of the demo passcode, or empty string."""
    configured_hash = os.environ.get("PDD_PASSCODE_SHA256", "").strip().lower()
    if len(configured_hash) == 64 and all(c in "0123456789abcdef" for c in configured_hash):
        return configured_hash
    raw_passcode = os.environ.get("PDD_DEMO_PASSCODE", "").strip()
    if len(raw_passcode) >= 12:
        return hashlib.sha256(raw_passcode.encode("utf-8")).hexdigest().lower()
    return ""


def _get_signing_secret() -> bytes:
    """Return the HMAC session signing key bytes, or empty bytes if missing/too short."""
    raw = os.environ.get("PDD_SESSION_SIGNING_SECRET", "").strip()
    if len(raw) >= 24:
        return raw.encode("utf-8")
    return b""


def is_auth_secret_configured() -> bool:
    """Return True only when both a strong passcode digest and session signing secret are present."""
    return bool(_get_expected_passcode_sha256() and _get_signing_secret())


def validate_startup_auth_configuration() -> None:
    """Fail startup closed if passcode protection is required but secrets are missing."""
    if not is_passcode_required():
        return
    if not _get_expected_passcode_sha256():
        raise RuntimeError(
            "Passcode protection is enabled (PDD_HOSTED_MODE / PDD_REQUIRE_PASSCODE), "
            "but neither PDD_PASSCODE_SHA256 (64-hex SHA-256) nor PDD_DEMO_PASSCODE (>=12 chars) is configured."
        )
    if not _get_signing_secret():
        raise RuntimeError(
            "Passcode protection is enabled (PDD_HOSTED_MODE / PDD_REQUIRE_PASSCODE), "
            "but PDD_SESSION_SIGNING_SECRET (>=24 chars) is missing."
        )


def verify_passcode_constant_time(candidate: str) -> bool:
    """Verify a candidate passcode in constant time using `hmac.compare_digest` over SHA-256."""
    expected_hex = _get_expected_passcode_sha256()
    if not expected_hex or not candidate:
        return False
    candidate_hex = hashlib.sha256(candidate.encode("utf-8")).hexdigest().lower()
    return hmac.compare_digest(candidate_hex, expected_hex)


def issue_session_cookie_value(*, ttl_seconds: int | None = None) -> tuple[str, dict[str, Any]]:
    """Create a signed session token and return `(cookie_value, payload_dict)`."""
    secret = _get_signing_secret()
    if not secret:
        raise RuntimeError("Cannot issue session cookie without PDD_SESSION_SIGNING_SECRET.")
    ttl = ttl_seconds or int(os.environ.get("PDD_SESSION_TTL_SECONDS", str(DEFAULT_SESSION_TTL_SECONDS)))
    now = int(time.time())
    payload = {
        "iat": now,
        "exp": now + max(60, ttl),
        "csrf": secrets.token_urlsafe(24),
        "jti": secrets.token_hex(8),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    b64_payload = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    sig = hmac.new(secret, b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64_payload}.{sig}", payload


def verify_session_cookie_value(cookie_val: str | None) -> dict[str, Any] | None:
    """Verify a signed session cookie in constant time and return payload if valid and unexpired."""
    if not cookie_val or "." not in cookie_val:
        return None
    secret = _get_signing_secret()
    if not secret:
        return None
    parts = cookie_val.split(".")
    if len(parts) != 2:
        return None
    b64_payload, provided_sig = parts
    if not b64_payload or not provided_sig:
        return None
    expected_sig = hmac.new(secret, b64_payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(provided_sig.lower(), expected_sig.lower()):
        return None
    try:
        pad = "=" * (-len(b64_payload) % 4)
        raw_json = base64.urlsafe_b64decode((b64_payload + pad).encode("ascii"))
        data = json.loads(raw_json.decode("utf-8"))
        if not isinstance(data, dict):
            return None
        exp = int(data.get("exp", 0))
        if exp <= int(time.time()):
            return None
        if not data.get("csrf"):
            return None
        return data
    except Exception:
        return None


def get_authenticated_session(request: Request) -> dict[str, Any] | None:
    """Return the verified session dict from `request.cookies['pdd_session']`, or None."""
    cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
    return verify_session_cookie_value(cookie_val)


_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_private_or_loopback_peer(host: str) -> bool:
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or (ip.version == 4 and ip in _CGNAT_NET))
    except ValueError:
        return False


def _parse_host_header(raw_host: str) -> str:
    raw_host = (raw_host or "").strip().lower()
    if not raw_host:
        return ""
    if raw_host.startswith("["):
        return raw_host.split("]")[0] + "]"
    return raw_host.split(":")[0]


def is_trusted_aca_https_request(request: Request) -> bool:
    """Return True ONLY when running behind trusted Azure Container Apps HTTPS ingress.

    Official ACA ingress behavior (https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview):
      - Terminates TLS 1.2/1.3 at the environment ingress proxy and overwrites `X-Forwarded-Proto`.
      - Appends the true client IP as the RIGHTMOST entry in `X-Forwarded-For`.
    """
    proxy_mode = os.environ.get("PDD_TRUSTED_INGRESS_PROXY", "").strip().lower()
    if proxy_mode != "aca":
        return False

    allowed_hosts = get_allowed_hosts()
    if not allowed_hosts:
        return False

    client_host = (request.client.host if request.client else "").strip().lower()
    if not is_hosted_mode() and not _is_private_or_loopback_peer(client_host):
        return False

    forwarded_proto_raw = (request.headers.get("x-forwarded-proto") or "").strip().lower()
    forwarded_proto = forwarded_proto_raw.split(",")[0].strip() if forwarded_proto_raw else ""
    if forwarded_proto != "https":
        return False

    host_header = _parse_host_header(request.headers.get("host") or "")
    if not host_header or host_header not in allowed_hosts:
        return False

    for hdr in ("origin", "referer"):
        val = (request.headers.get(hdr) or "").strip()
        if val:
            parsed = urlparse(val)
            origin_host = (parsed.hostname or "").strip().lower()
            if parsed.scheme != "https" or origin_host not in allowed_hosts:
                return False

    return True


def get_client_rate_limit_key(request: Request) -> str:
    """Extract spoofing-resistant client IP key for login rate limiting.

    In Azure Container Apps, only the rightmost `X-Forwarded-For` value is appended by ACA ingress.
    """
    if is_trusted_aca_https_request(request):
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if xff:
            rightmost = xff.split(",")[-1].strip()
            if rightmost:
                return rightmost
    return (request.client.host if request.client else "unknown").strip() or "unknown"


def check_login_rate_limit(request: Request) -> tuple[bool, int]:
    """Return `(allowed, retry_after_seconds)` for login attempts from this client."""
    key = get_client_rate_limit_key(request)
    now = time.time()
    recent = [ts for ts in _LOGIN_FAILURES.get(key, []) if now - ts < LOGIN_WINDOW_SECONDS]
    if recent:
        _LOGIN_FAILURES[key] = recent
    else:
        _LOGIN_FAILURES.pop(key, None)
    if len(recent) >= MAX_FAILED_LOGIN_ATTEMPTS:
        retry_after = max(1, int(LOGIN_WINDOW_SECONDS - (now - recent[0])))
        return False, retry_after
    return True, 0


def record_login_failure(request: Request) -> None:
    key = get_client_rate_limit_key(request)
    now = time.time()
    recent = [ts for ts in _LOGIN_FAILURES.get(key, []) if now - ts < LOGIN_WINDOW_SECONDS]
    recent.append(now)
    _LOGIN_FAILURES[key] = recent


def clear_login_failures(request: Request) -> None:
    key = get_client_rate_limit_key(request)
    _LOGIN_FAILURES.pop(key, None)


LOGIN_MAX_ATTEMPTS = MAX_FAILED_LOGIN_ATTEMPTS


def reset_login_rate_limits() -> None:
    """Clear in-memory login failure counters (used by unit tests)."""
    _LOGIN_FAILURES.clear()


def verify_same_origin_and_csrf(request: Request, session: dict[str, Any] | None) -> tuple[bool, str]:
    """Validate cross-site request protections on state-changing POST endpoints."""
    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sec_fetch_site == "cross-site":
        return False, "Cross-site state-changing requests are rejected (Sec-Fetch-Site: cross-site)."

    allowed_hosts = get_allowed_hosts() | _LOOPBACK_HOSTS
    allowed_origins = get_allowed_origins()

    for hdr in ("origin", "referer"):
        val = (request.headers.get(hdr) or "").strip()
        if val:
            parsed = urlparse(val)
            origin_host = (parsed.hostname or "").strip().lower()
            origin_base = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
            if is_hosted_mode():
                if parsed.scheme.lower() != "https":
                    return False, f"Invalid {hdr} scheme; HTTPS origin required."
                if allowed_origins and origin_base not in allowed_origins and origin_host not in get_allowed_hosts():
                    return False, f"Untrusted {hdr} header."
            else:
                if origin_host not in allowed_hosts:
                    return False, f"Untrusted {hdr} header."

    if is_passcode_required():
        if not session:
            return False, "Authentication required."
        provided_csrf = (request.headers.get(CSRF_HEADER_NAME) or "").strip()
        expected_csrf = str(session.get("csrf") or "")
        if not provided_csrf or not expected_csrf or not hmac.compare_digest(provided_csrf, expected_csrf):
            return False, "Missing or invalid CSRF token (X-CSRF-Token)."

    return True, ""


class RunConcurrencySlot:
    """Context manager enforcing bounded concurrent SSE runs."""

    def __init__(self, *, is_live: bool = False, is_live_mode: bool | None = None) -> None:
        self.is_live = bool(is_live_mode if is_live_mode is not None else is_live)
        self.acquired = False

    def _try_acquire_sync(self) -> None:
        global _ACTIVE_TOTAL_RUNS, _ACTIVE_LIVE_RUNS
        max_total = int(os.environ.get("PDD_MAX_CONCURRENT_RUNS", str(PDD_MAX_CONCURRENT_RUNS)))
        max_live = int(os.environ.get("PDD_MAX_CONCURRENT_LIVE_RUNS", str(PDD_MAX_CONCURRENT_LIVE_RUNS)))
        if _ACTIVE_TOTAL_RUNS >= max_total:
            raise OverflowError(
                f"Server is currently running the maximum number of concurrent evaluations ({max_total}). "
                "Please wait for the active run to finish and try again."
            )
        if self.is_live and _ACTIVE_LIVE_RUNS >= max_live:
            raise OverflowError(
                f"Another Live Web Research session is currently active (limit: {max_live}). "
                "Please wait for it to finish and retry."
            )
        _ACTIVE_TOTAL_RUNS += 1
        if self.is_live:
            _ACTIVE_LIVE_RUNS += 1
        self.acquired = True

    def _release_sync(self) -> None:
        global _ACTIVE_TOTAL_RUNS, _ACTIVE_LIVE_RUNS
        if not self.acquired:
            return
        _ACTIVE_TOTAL_RUNS = max(0, _ACTIVE_TOTAL_RUNS - 1)
        if self.is_live:
            _ACTIVE_LIVE_RUNS = max(0, _ACTIVE_LIVE_RUNS - 1)
        self.acquired = False

    async def acquire_or_raise(self) -> None:
        async with _RUN_LOCK:
            self._try_acquire_sync()

    async def release(self) -> None:
        if not self.acquired:
            return
        async with _RUN_LOCK:
            self._release_sync()

    def __enter__(self) -> "RunConcurrencySlot":
        from fastapi import HTTPException

        try:
            self._try_acquire_sync()
        except OverflowError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._release_sync()


def render_passcode_login_html() -> str:
    """Render the self-contained, accessible Passcode Sign-In Gate HTML page."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Sign in — Before you buy</title>
  <style>
    :root {
      --bg: #f5f6f8;
      --surface: #ffffff;
      --border: #cbd5e1;
      --text: #0f172a;
      --muted: #475569;
      --primary: #294fc9;
      --primary-hover: #1e40af;
      --danger: #b91c1c;
      --danger-bg: #fef2f2;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 20px;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      line-height: 1.5;
    }
    main {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      max-width: 440px;
      width: 100%;
      padding: 36px 28px;
      box-shadow: 0 4px 16px rgba(15, 23, 42, 0.06);
    }
    h1 {
      margin: 0 0 6px 0;
      font-size: 1.7rem;
      letter-spacing: -0.035em;
    }
    p {
      margin: 0 0 18px 0;
      font-size: 0.9rem;
      color: var(--muted);
    }
    label {
      display: block;
      font-weight: 600;
      font-size: 0.88rem;
      margin-bottom: 6px;
    }
    input[type="password"] {
      width: 100%;
      padding: 11px 13px;
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 1rem;
      margin-bottom: 14px;
    }
    input[type="password"]:focus-visible, button:focus-visible {
      outline: 3px solid var(--primary);
      outline-offset: 2px;
    }
    button {
      width: 100%;
      padding: 11px 16px;
      border: none;
      border-radius: 8px;
      background: var(--primary);
      color: #ffffff;
      font-weight: 700;
      font-size: 0.95rem;
      cursor: pointer;
    }
    button:hover { background: var(--primary-hover); }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .error-box {
      display: none;
      margin-bottom: 14px;
      padding: 10px 12px;
      border-radius: 8px;
      background: var(--danger-bg);
      border: 1px solid #fecaca;
      color: var(--danger);
      font-size: 0.85rem;
      font-weight: 600;
    }
    .meta-note {
      margin-top: 16px;
      padding-top: 12px;
      border-top: 1px solid #e2e8f0;
      font-size: 0.78rem;
      color: var(--muted);
    }
  </style>
</head>
<body>
  <main aria-labelledby="login-heading">
    <p style="color:var(--primary); font-weight:600; margin-bottom:12px;">Before you buy.</p>
    <h1 id="login-heading">A clearer buying decision.</h1>
    <p>Sign in to your private product-research workspace.</p>
    <div id="login-error" class="error-box" role="alert"></div>
    <form id="login-form">
      <label for="passcode-input">Portal passcode</label>
      <input type="password" id="passcode-input" name="passcode" required autocomplete="current-password" aria-describedby="login-note" />
      <button type="submit" id="login-btn">Open workspace</button>
    </form>
    <div class="meta-note" id="login-note">
      Protected prototype. Your session expires automatically. Nothing is purchased and WebIQ keys are never saved.
    </div>
  </main>
  <script>
    document.getElementById('login-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const errEl = document.getElementById('login-error');
      const btn = document.getElementById('login-btn');
      const passcode = document.getElementById('passcode-input').value;
      errEl.style.display = 'none';
      btn.disabled = true;
      btn.textContent = 'Verifying...';
      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ passcode })
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          errEl.textContent = data.detail || 'Invalid passcode or too many attempts.';
          errEl.style.display = 'block';
          btn.disabled = false;
          btn.textContent = 'Open workspace';
          return;
        }
        window.location.replace(window.location.pathname === '/saved-research'
          ? window.location.pathname + window.location.search : '/');
      } catch (e) {
        errEl.textContent = 'Connection error. Please try again.';
        errEl.style.display = 'block';
        btn.disabled = false;
        btn.textContent = 'Open workspace';
      }
    });
  </script>
</body>
</html>"""
