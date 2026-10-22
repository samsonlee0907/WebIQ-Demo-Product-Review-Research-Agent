"""FastAPI backend for the Product Due-Diligence & Assisted Shopping Portal.

Truthfully exposes sample/demo vs mixed-mode capability states in `/api/health`,
streams provenance-tagged SSE updates from `/api/run`, and enforces simulation-only
human-gated cart guardrails on `/api/cart/approve`.
"""
from __future__ import annotations

import asyncio
from contextlib import aclosing, asynccontextmanager
import io
import json
import logging
import os
from pathlib import Path
from urllib.parse import urlparse
import zipfile

import httpx
import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

try:
    from .evidence_archive import (
        ACTIVE_ARCHIVES, EvidenceRecorder, EvidenceStorageError, get_store,
        router as archive_router,
    )
    from .community_exploration import CommunityOptions, DEFAULT_DOMAINS, LIMITS
    from .replay_evidence import router as replay_router
except ImportError:
    from evidence_archive import (
        ACTIVE_ARCHIVES, EvidenceRecorder, EvidenceStorageError, get_store,
        router as archive_router,
    )
    from community_exploration import CommunityOptions, DEFAULT_DOMAINS, LIMITS
    from replay_evidence import router as replay_router

try:
    from .auth import (
        SESSION_COOKIE_NAME,
        RunConcurrencySlot,
        check_login_rate_limit,
        clear_login_failures,
        get_authenticated_session,
        is_auth_secret_configured,
        is_passcode_required,
        is_trusted_aca_https_request,
        issue_session_cookie_value,
        record_login_failure,
        render_passcode_login_html,
        validate_startup_auth_configuration,
        verify_passcode_constant_time,
        verify_same_origin_and_csrf,
    )
    from .bat_research import CLIENT_BROWSER_BROKER, ClientBrowserObservationPayload, redact_secrets
    from .browser_driver import BROWSER_DRIVER, CartPolicyViolationError
    from .config import (
        CAPABILITY_STATUS,
        DISCLAIMER,
        ENVIRONMENT_NAME,
        FOUNDRY_ACCOUNT_UPN,
        FOUNDRY_AUTHORIZED_OBJECT_ID,
        FOUNDRY_MANAGED_IDENTITY_CLIENT_ID,
        FOUNDRY_MODEL_DEPLOYMENT,
        FOUNDRY_PROJECT_ENDPOINT,
        FOUNDRY_TENANT_ID,
        FOUNDRY_TOOLBOX_NAME,
        MAX_DISPLAY_EXCERPT_CHARS,
        MODEL_DEPLOYMENT_NAME,
        PDD_RUN_TIMEOUT_SECONDS,
        PLAYWRIGHT_CONNECTION_NAME,
        PLAYWRIGHT_WORKSPACE_NAME,
        PROJECT_ENDPOINT,
        SCENARIOS,
        SITE_COMPATIBILITY_MATRIX,
        SKILLS_METADATA,
        TOOLBOX_META,
        get_allowed_hosts,
        get_allowed_origins,
        is_hosted_mode,
        load_skill_markdown,
    )
    from .pipeline import (
        ArtifactStorageUnavailable,
        build_agent_plan,
        resolve_artifact,
        run_due_diligence_pipeline,
    )
    from .screenshot_store import SCREENSHOT_STORE
    from .telemetry import configure as configure_telemetry
    from .url_safety import UrlValidationError
except ImportError:
    from auth import (
        SESSION_COOKIE_NAME,
        RunConcurrencySlot,
        check_login_rate_limit,
        clear_login_failures,
        get_authenticated_session,
        is_auth_secret_configured,
        is_passcode_required,
        is_trusted_aca_https_request,
        issue_session_cookie_value,
        record_login_failure,
        render_passcode_login_html,
        validate_startup_auth_configuration,
        verify_passcode_constant_time,
        verify_same_origin_and_csrf,
    )
    from bat_research import CLIENT_BROWSER_BROKER, ClientBrowserObservationPayload, redact_secrets
    from browser_driver import BROWSER_DRIVER, CartPolicyViolationError
    from config import (
        CAPABILITY_STATUS,
        DISCLAIMER,
        ENVIRONMENT_NAME,
        FOUNDRY_ACCOUNT_UPN,
        FOUNDRY_AUTHORIZED_OBJECT_ID,
        FOUNDRY_MANAGED_IDENTITY_CLIENT_ID,
        FOUNDRY_MODEL_DEPLOYMENT,
        FOUNDRY_PROJECT_ENDPOINT,
        FOUNDRY_TENANT_ID,
        FOUNDRY_TOOLBOX_NAME,
        MAX_DISPLAY_EXCERPT_CHARS,
        MODEL_DEPLOYMENT_NAME,
        PDD_RUN_TIMEOUT_SECONDS,
        PLAYWRIGHT_CONNECTION_NAME,
        PLAYWRIGHT_WORKSPACE_NAME,
        PROJECT_ENDPOINT,
        SCENARIOS,
        SITE_COMPATIBILITY_MATRIX,
        SKILLS_METADATA,
        TOOLBOX_META,
        get_allowed_hosts,
        get_allowed_origins,
        is_hosted_mode,
        load_skill_markdown,
    )
    from pipeline import (
        ArtifactStorageUnavailable,
        build_agent_plan,
        resolve_artifact,
        run_due_diligence_pipeline,
    )
    from screenshot_store import SCREENSHOT_STORE
    from telemetry import configure as configure_telemetry
    from url_safety import UrlValidationError


_TELEMETRY_ON = configure_telemetry()

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost", "testclient", "testserver"})


def is_secure_or_loopback_request(request: Request) -> bool:
    """Allow request-scoped X-WebIQ-Key over direct HTTPS, trusted ACA HTTPS ingress, or verified local loopback HTTP.

    Security rules:
      - Direct `https://` requests are permitted when no non-https forwarded hop is present (and in hosted
        mode when `Host`/`Origin` match `PDD_ALLOWED_HOSTS`).
      - Behind Azure Container Apps ingress (`PDD_TRUSTED_INGRESS_PROXY=aca`), `X-Forwarded-Proto: https`
        is accepted ONLY when `is_trusted_aca_https_request(request)` succeeds (private/loopback ingress peer,
        `Host` in explicit `PDD_ALLOWED_HOSTS`, and any `Origin`/`Referer` matching `https://<allowed_host>`).
      - Otherwise, plain `http://` requests reject ANY `X-Forwarded-Proto` (including spoofed
        `X-Forwarded-Proto: https`), `X-Forwarded-For`, `X-Forwarded-Host`, `X-Real-IP`, or `Forwarded`,
        and require loopback `request.client.host`, `request.url.hostname`, `Host`, and `Origin`/`Referer`.
    """
    scheme = (request.url.scheme or "").strip().lower()

    if scheme == "https":
        forwarded_proto = (request.headers.get("x-forwarded-proto") or "").strip().lower()
        if forwarded_proto and forwarded_proto != "https":
            return False
        if is_hosted_mode():
            allowed_hosts = get_allowed_hosts()
            if not allowed_hosts:
                return False
            raw_host = (request.headers.get("host") or "").strip().lower().split(":")[0]
            if raw_host not in allowed_hosts:
                return False
            for hdr in ("origin", "referer"):
                val = (request.headers.get(hdr) or "").strip()
                if val:
                    parsed = urlparse(val)
                    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() not in allowed_hosts:
                        return False
        return True

    if scheme != "http":
        return False

    if is_trusted_aca_https_request(request):
        return True

    if is_hosted_mode():
        return False

    # Never accept X-Forwarded-Proto (including spoofed `X-Forwarded-Proto: https`),
    # X-Forwarded-For, X-Forwarded-Host, X-Real-IP, or Forwarded on plain HTTP requests.
    for proxy_header in (
        "x-forwarded-proto",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-real-ip",
        "forwarded",
    ):
        if (request.headers.get(proxy_header) or "").strip():
            return False

    client_host = (request.client.host if request.client else "").strip().lower()
    if not client_host or client_host not in _LOOPBACK_HOSTS:
        return False

    url_host = (request.url.hostname or "").strip().lower()
    if url_host not in _LOOPBACK_HOSTS:
        return False

    raw_host_header = (request.headers.get("host") or "").strip().lower()
    if not raw_host_header:
        return False
    if raw_host_header.startswith("["):
        parsed_host = raw_host_header.split("]")[0] + "]"
    else:
        parsed_host = raw_host_header.split(":")[0]
    if parsed_host not in _LOOPBACK_HOSTS:
        return False

    for hdr in ("origin", "referer"):
        val = (request.headers.get(hdr) or "").strip()
        if val:
            parsed = urlparse(val)
            origin_host = (parsed.hostname or "").strip().lower()
            if parsed.scheme not in {"http", "https"} or origin_host not in _LOOPBACK_HOSTS:
                return False

    return True


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    validate_startup_auth_configuration()
    yield


app = FastAPI(
    title="Product Due-Diligence Assistant API",
    version="2.2.0",
    lifespan=_app_lifespan,
)
app.include_router(replay_router)
app.include_router(archive_router)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(get_allowed_origins()),
    allow_origin_regex=(
        None
        if is_hosted_mode()
        else r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$"
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-WebIQ-Key", "X-CSRF-Token", "Accept"],
)


@app.middleware("http")
async def security_and_passcode_gate_middleware(request: Request, call_next):
    path = request.url.path or "/"
    method = (request.method or "GET").upper()

    if method == "OPTIONS":
        return await call_next(request)

    # 1. Fail-closed check when passcode auth is enabled (`PDD_HOSTED_MODE` or `PDD_REQUIRE_PASSCODE`)
    if is_passcode_required() and not is_auth_secret_configured():
        return JSONResponse(
            status_code=503,
            content={
                "detail": "Service is fail-closed: required passcode or session signing secrets are not configured."
            },
            headers={"Cache-Control": "no-store"},
        )

    # 2. Enforce HTTPS and allowed Host header in hosted or explicit allowed-host mode (except local container readiness probe on /api/health)
    allowed_hosts = get_allowed_hosts()
    if is_hosted_mode() or (is_passcode_required() and allowed_hosts):
        is_local_probe = (
            path == "/api/health"
            and method == "GET"
            and not request.headers.get("x-forwarded-proto")
            and (request.client.host if request.client else "") in {"127.0.0.1", "::1"}
        )
        if not is_local_probe:
            raw_host = (request.headers.get("host") or "").strip().lower().split(":")[0]
            if allowed_hosts and raw_host not in allowed_hosts:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Host header."},
                    headers={"Cache-Control": "no-store"},
                )
            if is_hosted_mode() and request.url.scheme.lower() != "https" and not is_trusted_aca_https_request(request):
                return JSONResponse(
                    status_code=400,
                    content={"detail": "HTTPS connection required."},
                    headers={"Cache-Control": "no-store"},
                )

    # 3. Evaluate session authentication (or X-PDD-Passcode header for remote browser workers)
    session = get_authenticated_session(request)
    worker_passcode_hdr = (request.headers.get("x-pdd-passcode") or "").strip()
    has_valid_worker_passcode = bool(
        path.startswith("/api/client-browser/")
        and worker_passcode_hdr
        and verify_passcode_constant_time(worker_passcode_hdr)
    )
    if session is None and has_valid_worker_passcode:
        session = {"sub": "vm-playwright-worker", "worker_header": True}
    request.state.auth_session = session

    if is_passcode_required():
        # Allow minimal health check and login/session endpoints to reach their handlers
        if path in ("/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/session"):
            pass
        elif path in ("/", "/portal", "/login", "/saved-research") and method == "GET":
            if session is None:
                return HTMLResponse(
                    render_passcode_login_html(),
                    status_code=200,
                    headers={"Cache-Control": "no-store"},
                )
        else:
            # Protect all research APIs, reports, screenshots, and OpenAPI/docs endpoints
            if session is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Passcode authentication required."},
                    headers={"Cache-Control": "no-store"},
                )

    # 4. Enforce same-origin & CSRF checks on state-changing POST endpoints
    if (method == "POST" and path in (
        "/api/plan",
        "/api/run",
        "/api/cart/approve",
        "/api/client-browser/observation",
        "/api/client-browser/heartbeat",
        "/api/replay/transient",
        "/api/auth/logout",
    )) or (method == "DELETE" and path.startswith("/api/saved-research/")):
        if not has_valid_worker_passcode and (method == "DELETE" or is_passcode_required() or not request.headers.get("x-webiq-key")):
            ok_csrf, csrf_reason = verify_same_origin_and_csrf(request, session)
            if not ok_csrf:
                return JSONResponse(
                    status_code=403,
                    content={"detail": csrf_reason},
                    headers={"Cache-Control": "no-store"},
                )

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.headers.get("x-webiq-key"):
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid workflow request. Check the input fields."},
        )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(EvidenceStorageError)
async def evidence_storage_error(_request: Request, exc: EvidenceStorageError):
    return JSONResponse(status_code=503, content={"detail": str(exc)}, headers={"Cache-Control": "no-store"})


class PlanRequest(CommunityOptions):
    product_url: str = Field(..., description="Initial HTTPS URL containing the product of interest")
    user_intent: str = Field(..., description="Brief user input on what the user is looking for")
    execution_mode: str = Field(
        default="auto",
        description=(
            "Execution mode: 'vm_playwright' (Azure VM + Playwright), 'client_browser' (Local Playwright), "
            "'live' (Azure BAT Tool), 'webiq_browse' (WebIQ /v3/browse), or 'sample' (Sample Walkthrough)"
        ),
    )
    use_client_browser: bool = False
    inspect_review_pages: bool = True


class RunRequest(CommunityOptions):
    product_url: str | None = Field(default=None, description="Initial HTTPS URL containing the product")
    user_intent: str | None = Field(default=None, description="What the user cares about regarding the product")
    scenario: str | None = Field(default=None, description="Optional preset scenario key")
    webiq_query: str | None = None
    execution_mode: str = Field(
        default="auto",
        description=(
            "Execution mode: 'vm_playwright' (Azure VM + Playwright), 'client_browser' (Local Playwright), "
            "'live' (Azure BAT Tool), 'webiq_browse' (WebIQ /v3/browse), or 'sample' (Sample Walkthrough)"
        ),
    )
    use_client_browser: bool = False
    inspect_review_pages: bool = True


class CartApprovalRequest(BaseModel):
    approval_token: str
    approved: bool
    sku: str
    variant: str
    seller: str
    max_price: float | None = None
    quantity: int = 1
    requested_action: str = "add_to_cart"
    target_url: str | None = None


class PasscodeLoginRequest(BaseModel):
    passcode: str = Field(..., min_length=1, max_length=256)


@app.post("/api/auth/login")
def login_with_passcode(req: PasscodeLoginRequest, request: Request):
    if not is_passcode_required():
        return {"authenticated": True, "auth_required": False, "csrf_token": None}

    # Enforce same-origin check on login POST
    sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    if sec_fetch_site == "cross-site":
        raise HTTPException(status_code=403, detail="Cross-site login requests are rejected.")
    allowed_hosts = get_allowed_hosts() | _LOOPBACK_HOSTS
    for hdr in ("origin", "referer"):
        val = (request.headers.get(hdr) or "").strip()
        if val:
            parsed = urlparse(val)
            if (parsed.hostname or "").strip().lower() not in allowed_hosts:
                raise HTTPException(status_code=403, detail=f"Untrusted {hdr} header.")

    allowed, retry_after = check_login_rate_limit(request)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"detail": f"Too many failed passcode attempts. Try again in {retry_after}s."},
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )

    if not verify_passcode_constant_time(req.passcode):
        record_login_failure(request)
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid demo passcode."},
            headers={"Cache-Control": "no-store"},
        )

    clear_login_failures(request)
    cookie_val, payload = issue_session_cookie_value()
    secure_cookie = is_hosted_mode() or request.url.scheme.lower() == "https" or is_trusted_aca_https_request(request)
    resp = JSONResponse(
        status_code=200,
        content={
            "authenticated": True,
            "auth_required": True,
            "csrf_token": payload["csrf"],
            "expires_at": payload["exp"],
        },
        headers={"Cache-Control": "no-store"},
    )
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_val,
        max_age=max(60, int(payload["exp"] - payload["iat"])),
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/api/auth/logout")
def logout_session(request: Request):
    secure_cookie = is_hosted_mode() or request.url.scheme.lower() == "https" or is_trusted_aca_https_request(request)
    resp = JSONResponse(
        status_code=200,
        content={"authenticated": False, "auth_required": is_passcode_required()},
        headers={"Cache-Control": "no-store"},
    )
    resp.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=secure_cookie,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.get("/api/auth/session")
def auth_session_status(request: Request):
    session = getattr(request.state, "auth_session", None) or get_authenticated_session(request)
    req_passcode = is_passcode_required()
    return JSONResponse(
        content={
            "auth_required": req_passcode,
            "authenticated": True if not req_passcode else bool(session),
            "csrf_token": session.get("csrf") if session else None,
            "expires_at": session.get("exp") if session else None,
            "hosted_mode": is_hosted_mode(),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/login", response_class=HTMLResponse)
def serve_login_page() -> HTMLResponse:
    return HTMLResponse(render_passcode_login_html(), headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
@app.get("/portal", response_class=HTMLResponse)
def serve_portal() -> HTMLResponse:
    candidates = [
        Path(__file__).resolve().parent / "portal.html",
        Path(__file__).resolve().parents[2] / "portal" / "index.html",
    ]
    for path in candidates:
        if path.is_file():
            return HTMLResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>Product Due-Diligence Assistant</h1><p>portal.html not found.</p>", status_code=200)


@app.get("/benchmark-report", response_class=HTMLResponse)
@app.get("/api/benchmark-report.html", response_class=HTMLResponse)
def serve_benchmark_report() -> HTMLResponse:
    candidates = [
        Path(__file__).resolve().parent / "benchmark_report.html",
        Path(__file__).resolve().parents[2] / "docs" / "browser-execution-methods-benchmark.html",
    ]
    for path in candidates:
        if path.is_file():
            return HTMLResponse(path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>Benchmark Report Not Found</h1>", status_code=404)


@app.get("/api/health")
def health(request: Request):
    session = getattr(request.state, "auth_session", None) or get_authenticated_session(request)
    req_passcode = is_passcode_required()
    hosted = is_hosted_mode()
    if req_passcode and session is None:
        return JSONResponse(
            content={
                "status": "ok",
                "auth_required": True,
                "authenticated": False,
                "hosted_mode": hosted,
                "mode": "passcode_protected_demo",
                "mode_label": "Passcode-Protected Online Demo (Sign in required)",
            },
            headers={"Cache-Control": "no-store"},
        )

    cred_policy = (
        f"Hosted cloud mode: Azure ManagedIdentityCredential (client_id={FOUNDRY_MANAGED_IDENTITY_CLIENT_ID or 'system-assigned'}) "
        "with no local .azure/MSAL user-cache access and no interactive prompts"
        if hosted
        else (
            f"Strictly non-interactive MSAL silent cache pinned to tenant {FOUNDRY_TENANT_ID} "
            f"and account oid={FOUNDRY_AUTHORIZED_OBJECT_ID} "
            "(no raw env token bypass, no silent fallback to other cached users, no browser popups)"
        )
    )
    return {
        "status": "ok",
        "auth_required": req_passcode,
        "authenticated": True,
        "csrf_token": session.get("csrf") if session else None,
        "hosted_mode": hosted,
        "mode": "sample_demo",
        "mode_label": (
            "Live configuration supplied (connections not probed); offline walkthroughs available"
            if FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL_DEPLOYMENT
            else "Offline walkthroughs available; configure Foundry for live research"
        ),
        "disclaimer": DISCLAIMER,
        "excerpt_char_limit": MAX_DISPLAY_EXCERPT_CHARS,
        "capabilities": CAPABILITY_STATUS,
        "phase1_live_config": {
            "hosted_mode": hosted,
            "foundry_tenant_id": FOUNDRY_TENANT_ID,
            "foundry_authorized_object_id": None if hosted else FOUNDRY_AUTHORIZED_OBJECT_ID,
            "foundry_managed_identity_client_id": FOUNDRY_MANAGED_IDENTITY_CLIENT_ID or None if hosted else None,
            "foundry_account_upn": None if hosted else (FOUNDRY_ACCOUNT_UPN or None),
            "foundry_project_endpoint": FOUNDRY_PROJECT_ENDPOINT,
            "foundry_model_deployment": FOUNDRY_MODEL_DEPLOYMENT,
            "foundry_toolbox_name": FOUNDRY_TOOLBOX_NAME,
            "playwright_connection_name": PLAYWRIGHT_CONNECTION_NAME,
            "playwright_workspace_name": PLAYWRIGHT_WORKSPACE_NAME,
            "credential_policy": cred_policy,
            "webiq_key_transport": (
                "Request-scoped X-WebIQ-Key header allowed over direct HTTPS, verified Azure Container Apps HTTPS ingress, "
                "or verified local loopback HTTP (127.0.0.1 / localhost / ::1 with no X-Forwarded-* headers); "
                "never stored on disk, localStorage, logs, or reports"
            ),
        },
        "provisioning_script_status": "scaffold_not_deployment_ready",
        "webiq_browse_path_supported": True,
        "project_endpoint": PROJECT_ENDPOINT or None,
        "telemetry": _TELEMETRY_ON,
        "environment_name": ENVIRONMENT_NAME,
        "model_deployment_name": MODEL_DEPLOYMENT_NAME,
    }


@app.get("/api/research-contract")
def research_contract():
    return {
        "contract": "one-inspector-four-research-stages",
        "research_stages": 4,
        "review_inspection_limit": None,
        "run_timeout_seconds": PDD_RUN_TIMEOUT_SECONDS,
        "model_deployment": MODEL_DEPLOYMENT_NAME,
        "revision": os.environ.get("CONTAINER_APP_REVISION", ""),
        "review_strategies": ["inspect_discovered_pages", "search_only"],
        "community_exploration": {
            "default_enabled": False, "default_domains": DEFAULT_DOMAINS, "limits": LIMITS,
            "supported_inspectors": ["live", "client_browser", "vm_playwright"],
            "actions": ["search", "visit_observed_url", "scroll", "finish"],
            "scope": "Additional community exploration only; general discovery stays open.",
            "comments": "Visible candidate assertions, not verified owners or authenticity proof.",
            "dynamic_loading": "Read-only browser network policies can prevent dynamic comments loading.",
        },
        "search_grounding": {"contentFormat": "passage", "maxLength": 3000,
                             "display_limit": 200, "raw_retention": "active_workflow_only"},
        "saved_research": {"retention": "until_explicit_deletion", "raw_webiq_content": False,
                           "shared_demo_access": True, "includes_genuine_screenshots": True},
    }


@app.get("/api/scenarios")
@app.get("/api/presets")
def scenarios():
    out = []
    for key, s in SCENARIOS.items():
        out.append({
            "key": key,
            "title": s["title"],
            "short_label": s.get("short_label", s["title"]),
            "tagline": s["tagline"],
            "category": s["category"],
            "sample_badge": s.get("sample_badge", "Curated Sample Walkthrough"),
            "toolbox": s["toolbox"],
            "agent": s["agent"],
            "skills": s["skills"],
            "product_url": s["product_url"],
            "store_targets": s.get("store_targets", []),
            "user_intent": s["user_intent"],
            "default_webiq_query": s["default_webiq_query"],
        })
    return {"scenarios": out, "site_compatibility_matrix": SITE_COMPATIBILITY_MATRIX}


@app.get("/api/skills")
def list_skills():
    items = []
    for name, meta in SKILLS_METADATA.items():
        md = load_skill_markdown(name)
        items.append({**meta, "markdown": md})
    return {"skills": items}


@app.get("/api/toolboxes")
def toolboxes():
    return {"toolboxes": list(TOOLBOX_META.values())}


@app.post("/api/plan")
def preview_plan(req: PlanRequest, request: Request):
    has_key = bool((request.headers.get("x-webiq-key") or "").strip())
    try:
        plan = build_agent_plan(
            req.product_url,
            req.user_intent,
            execution_mode=req.execution_mode,
            has_live_webiq_key=has_key,
            use_client_browser=req.use_client_browser,
            inspect_review_pages=req.inspect_review_pages,
            community_exploration=req.community_exploration,
            exploration_domains=req.exploration_domains,
        )
        return {"plan": plan}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.post("/api/run")
async def run(req: RunRequest, request: Request):
    key = request.headers.get("x-webiq-key", "")
    if key.strip() and not is_secure_or_loopback_request(request):
        raise HTTPException(
            status_code=400,
            detail="WebIQ requires an HTTPS connection (or a local loopback connection on 127.0.0.1 / localhost with matching Host/Origin).",
        )

    scenario_meta = SCENARIOS.get(req.scenario or "") if req.scenario else None
    if req.scenario and not scenario_meta and not req.product_url:
        raise HTTPException(status_code=400, detail="Unknown scenario.")

    product_url = (req.product_url or (scenario_meta["product_url"] if scenario_meta else "")).strip()
    user_intent = (req.user_intent or (scenario_meta["user_intent"] if scenario_meta else "")).strip()
    if not product_url:
        raise HTTPException(status_code=400, detail="product_url is required.")
    if not user_intent:
        raise HTTPException(status_code=400, detail="user_intent is required.")
    if req.community_exploration:
        try:
            build_agent_plan(
                product_url, user_intent, execution_mode=req.execution_mode, use_client_browser=req.use_client_browser,
                inspect_review_pages=req.inspect_review_pages, community_exploration=True,
                exploration_domains=req.exploration_domains,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    norm_mode = (req.execution_mode or "").strip().lower()
    is_live_run = (
        norm_mode in ("live", "client_browser", "vm_playwright", "vm_worker", "webiq_browse")
        or bool(req.use_client_browser)
    )
    slot = RunConcurrencySlot(is_live=is_live_run)
    try:
        await slot.acquire_or_raise()
    except OverflowError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from None
    try:
        archive = EvidenceRecorder(
            get_store(), product_url=product_url, user_intent=user_intent, execution_mode=req.execution_mode,
        )
        await archive.start()
    except BaseException:
        await slot.release()
        raise

    async def _bounded_stream():
        archive_failed = False
        try:
            yield f"data: {json.dumps(archive.public_status())}\n\n"
            async with aclosing(run_due_diligence_pipeline(
                product_url=product_url,
                user_intent=user_intent,
                scenario=req.scenario,
                webiq_key=key,
                webiq_query=req.webiq_query,
                execution_mode=req.execution_mode,
                use_client_browser=req.use_client_browser,
                inspect_review_pages=req.inspect_review_pages,
                community_exploration=req.community_exploration,
                exploration_domains=req.exploration_domains,
            )) as pipeline_stream:
                async for chunk in pipeline_stream:
                    if not archive_failed and chunk.startswith("data:"):
                        event = json.loads(chunk.removeprefix("data:").strip())
                        try:
                            if event.get("type") == "done":
                                await archive.finish(str(event.get("outcome") or "error"))
                                yield f"data: {json.dumps(archive.public_status())}\n\n"
                            else:
                                await archive.observe(event)
                        except EvidenceStorageError as exc:
                            archive_failed = True
                            yield f"data: {json.dumps({'type': 'evidence_archive', 'status': 'failed', 'message': str(exc), 'url': archive.public_status()['url']})}\n\n"
                    yield chunk
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    if not archive.finished:
                        await archive.finish("retention_incomplete" if archive_failed else "interrupted")
                except EvidenceStorageError:
                    logging.getLogger(__name__).error("Final evidence checkpoint failed; previously saved objects remain available.")
                finally:
                    ACTIVE_ARCHIVES.discard(archive.archive_id)
                    await slot.release()

    return StreamingResponse(
        _bounded_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class WorkerHeartbeatRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=80)
    hostname: str = Field(default="", max_length=120)
    browser_version: str = Field(default="", max_length=120)
    mode: str = Field(default="windows_vm_playwright_chrome", max_length=80)


@app.get("/api/client-browser/status")
async def client_browser_worker_status():
    return CLIENT_BROWSER_BROKER.get_worker_status()


@app.post("/api/client-browser/heartbeat")
async def submit_client_browser_heartbeat(req: WorkerHeartbeatRequest):
    return CLIENT_BROWSER_BROKER.record_worker_heartbeat(
        worker_id=req.worker_id,
        hostname=req.hostname,
        browser_version=req.browser_version,
        mode=req.mode,
    )


@app.get("/api/client-browser/next-command")
async def poll_next_client_browser_command(worker_id: str = "vm-worker", timeout: float = 12.0):
    bounded_timeout = max(0.0, min(float(timeout), 25.0))
    deadline = asyncio.get_running_loop().time() + bounded_timeout
    while True:
        cmd = CLIENT_BROWSER_BROKER.pop_next_command(worker_id=worker_id)
        if cmd is not None:
            return {"has_command": True, "command": cmd}
        if asyncio.get_running_loop().time() >= deadline:
            return {"has_command": False, "command": None}
        await asyncio.sleep(0.35)


@app.post("/api/client-browser/observation")
async def submit_client_browser_observation(payload: ClientBrowserObservationPayload):
    accepted = CLIENT_BROWSER_BROKER.resolve_observation(payload)
    if not accepted:
        raise HTTPException(
            status_code=404,
            detail="No pending client browser command found for this run_id/command_id (or it already completed).",
        )
    return {"accepted": True, "run_id": payload.run_id, "command_id": payload.command_id}


@app.get("/api/chrome-extension.zip")
def download_chrome_extension():
    candidates = [
        Path(__file__).resolve().parent / "browser_extension",
        Path(__file__).resolve().parents[2] / "browser_extension",
    ]
    ext_dir = next((p for p in candidates if p.is_dir()), None)
    if ext_dir is None:
        raise HTTPException(status_code=404, detail="Chrome extension directory not found.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(ext_dir.rglob("*")):
            if file_path.is_file():
                zf.write(file_path, arcname=f"pdd-chrome-extension/{file_path.relative_to(ext_dir).as_posix()}")
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="pdd-chrome-extension.zip"',
        },
    )


@app.post("/api/cart/approve")
def approve_cart(req: CartApprovalRequest):
    try:
        receipt = BROWSER_DRIVER.execute_approved_cart_action(
            approval_token=req.approval_token,
            approved=req.approved,
            sku=req.sku,
            variant=req.variant,
            seller=req.seller,
            max_price=req.max_price,
            quantity=req.quantity,
            requested_action=req.requested_action,
            target_url=req.target_url,
        )
        return receipt
    except CartPolicyViolationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@app.get("/api/screenshots/{artifact_id}")
def screenshot(artifact_id: str):
    record = SCREENSHOT_STORE.get(artifact_id)
    if not record:
        raise HTTPException(status_code=404, detail="Screenshot capture not found or expired.")
    return Response(
        content=record.data,
        media_type=record.media_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "X-Screenshot-Sha256": record.sha256,
            "X-Capture-Kind": record.capture_kind,
        },
    )


@app.get("/api/artifacts/{artifact_id}")
def artifact(artifact_id: str):
    try:
        meta = resolve_artifact(artifact_id)
    except ArtifactStorageUnavailable:
        logging.getLogger(__name__).warning("Artifact download storage unavailable")
        raise HTTPException(status_code=503, detail="Artifact storage is temporarily unavailable.") from None
    if not meta:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if not meta["persisted"]:
        return Response(
            content=meta["content"],
            media_type=meta["media_type"],
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{meta["filename"]}"',
            },
        )
    return FileResponse(meta["path"], media_type=meta["media_type"], filename=meta["filename"])
