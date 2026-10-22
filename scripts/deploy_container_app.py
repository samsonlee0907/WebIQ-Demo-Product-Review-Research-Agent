"""Opt-in bootstrap for a new passcode-protected Azure Container App.

Read configuration from process environment, preview the plan by default, and
require --apply for Azure changes. Existing applications are not overwritten.
Foundry resources and accessible identity-authorized evidence storage must exist.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import sys
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "api" / "app"))
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from bat_research import NonInteractiveTenantCredential  # noqa: E402
import config as app_config  # noqa: E402

SUBSCRIPTION_ID = app_config.FOUNDRY_SUBSCRIPTION_ID
TENANT_ID = app_config.FOUNDRY_TENANT_ID
AUTHORIZED_OID = app_config.FOUNDRY_AUTHORIZED_OBJECT_ID
RESOURCE_GROUP = os.environ.get("PDD_RESOURCE_GROUP", "").strip() or app_config.FOUNDRY_RESOURCE_GROUP
LOCATION = os.environ.get("PDD_AZURE_LOCATION", "").strip()
ACA_LOCATION = LOCATION

FOUNDRY_ACCOUNT_NAME = app_config.FOUNDRY_ACCOUNT_NAME
FOUNDRY_PROJECT_NAME = app_config.FOUNDRY_PROJECT_NAME
FOUNDRY_PROJECT_ENDPOINT = app_config.FOUNDRY_PROJECT_ENDPOINT
FOUNDRY_MODEL_DEPLOYMENT = app_config.FOUNDRY_MODEL_DEPLOYMENT
FOUNDRY_TOOLBOX_NAME = app_config.FOUNDRY_TOOLBOX_NAME
PLAYWRIGHT_WORKSPACE_NAME = app_config.PLAYWRIGHT_WORKSPACE_NAME
PLAYWRIGHT_CONNECTION_NAME = app_config.PLAYWRIGHT_CONNECTION_NAME

UAMI_NAME = os.environ.get("PDD_IDENTITY_NAME", "").strip() or "pdd-portal-identity"
ACR_NAME = os.environ.get("PDD_ACR_NAME", "").strip()
CAE_NAME = os.environ.get("PDD_CONTAINER_ENVIRONMENT_NAME", "").strip() or "pdd-environment"
CA_NAME = os.environ.get("PDD_CONTAINER_APP_NAME", "").strip() or "pdd-portal"

ROLE_ACR_PULL = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
ROLE_FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"

DEFAULT_PASSCODE_FILE = Path(os.environ.get("PDD_PASSCODE_FILE") or (
    Path.home() / ".config" / "product-due-diligence" / "portal-access.txt"
))


def require_configuration(values: dict[str, str]) -> None:
    missing = [name for name, value in values.items() if not value.strip()]
    if missing:
        raise ValueError("Configure required environment values: " + ", ".join(missing))


class ArmClient:
    def __init__(self) -> None:
        require_configuration({
            "FOUNDRY_TENANT_ID": TENANT_ID, "FOUNDRY_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
            "PDD_RESOURCE_GROUP (or FOUNDRY_RESOURCE_GROUP)": RESOURCE_GROUP,
            "FOUNDRY_AUTHORIZED_OBJECT_ID or FOUNDRY_ACCOUNT_UPN": AUTHORIZED_OID or app_config.FOUNDRY_ACCOUNT_UPN,
        })
        self._cred = NonInteractiveTenantCredential(
            tenant_id=TENANT_ID,
            account_oid=AUTHORIZED_OID,
        )
        self._http = httpx.Client(timeout=60.0)

    def _headers(self) -> dict[str, str]:
        tok = self._cred.get_token("https://management.azure.com/.default")
        return {
            "Authorization": f"Bearer {tok.token}",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        expected_statuses: tuple[int, ...] = (200, 201, 202),
    ) -> httpx.Response:
        full_url = url if url.startswith("https://") else f"https://management.azure.com{url}"
        res = self._http.request(method, full_url, headers=self._headers(), json=json_body)
        if res.status_code not in expected_statuses:
            raise RuntimeError(
                f"ARM {method} {full_url} failed with HTTP {res.status_code}: {res.text[:600]}"
            )
        return res

    def wait_for_provisioning(self, resource_path: str, api_version: str, timeout_sec: int = 420) -> dict[str, Any]:
        deadline = time.time() + timeout_sec
        url = f"{resource_path}?api-version={api_version}"
        while time.time() < deadline:
            res = self.request("GET", url, expected_statuses=(200,))
            data = res.json()
            state = (data.get("properties") or {}).get("provisioningState")
            if state == "Succeeded":
                return data
            if state in ("Failed", "Canceled"):
                raise RuntimeError(f"Provisioning of {resource_path} ended in state={state}: {json.dumps(data)[:600]}")
            time.sleep(5)
        raise TimeoutError(f"Timed out waiting for {resource_path} to reach Succeeded.")


def load_or_create_secrets(passcode_file: Path, *, rotate: bool = False) -> dict[str, str]:
    passcode_file.parent.mkdir(parents=True, exist_ok=True)
    state_json_path = passcode_file.with_suffix(".json")
    if not rotate and state_json_path.is_file():
        try:
            existing = json.loads(state_json_path.read_text(encoding="utf-8"))
            if (
                isinstance(existing, dict)
                and isinstance(existing.get("passcode"), str)
                and len(existing["passcode"]) >= 16
                and isinstance(existing.get("session_signing_secret"), str)
                and len(existing["session_signing_secret"]) >= 64
            ):
                passcode = existing["passcode"]
                signing_secret = existing["session_signing_secret"]
                passcode_sha256 = hashlib.sha256(passcode.encode("utf-8")).hexdigest()
                return {
                    "passcode": passcode,
                    "passcode_sha256": passcode_sha256,
                    "session_signing_secret": signing_secret,
                }
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("The private access file is unreadable. Restore it or explicitly request --rotate-secrets.") from exc
        raise ValueError("The private access file is invalid. Restore it or explicitly request --rotate-secrets.")

    # Generate strong random temporary passcode (24 URL-safe chars) and 64-hex signing secret
    passcode = secrets.token_urlsafe(18)
    passcode_sha256 = hashlib.sha256(passcode.encode("utf-8")).hexdigest()
    signing_secret = secrets.token_hex(32)
    state_json_path.write_text(
        json.dumps(
            {
                "passcode": passcode,
                "passcode_sha256": passcode_sha256,
                "session_signing_secret": signing_secret,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "passcode": passcode,
        "passcode_sha256": passcode_sha256,
        "session_signing_secret": signing_secret,
    }


def write_passcode_delivery_file(
    passcode_file: Path,
    *,
    portal_url: str,
    passcode: str,
    passcode_sha256: str,
) -> None:
    content = (
        "# Product Due-Diligence Assistant — Temporary Online Demo Access\n\n"
        f"Portal HTTPS URL: {portal_url}\n"
        f"Temporary Demo Passcode: {passcode}\n"
        f"Passcode SHA-256 Fingerprint (first 12 hex): {passcode_sha256[:12]}\n\n"
        "Notes:\n"
        "- Enter the temporary demo passcode above on the sign-in screen to unlock an 8-hour HttpOnly session cookie.\n"
        "- All research APIs (/api/plan, /api/run, /api/scenarios, /api/skills, /api/cart/approve), reports, screenshots, and OpenAPI docs require this passcode session.\n"
        "- Your Web IQ API key is entered directly inside the portal UI after sign-in and remains request-scoped in memory only (never stored on disk or in logs).\n"
    )
    passcode_file.write_text(content, encoding="utf-8")


def build_filtered_source_tarball(repo_root: Path, *, include_private_replay: bool = False) -> bytes:
    """Create an in-memory .tar.gz containing only the Dockerfile and runtime source files."""
    allowed_files: list[Path] = [
        repo_root / "Dockerfile",
        repo_root / ".dockerignore",
        repo_root / "requirements.txt",
        repo_root / "api" / "__init__.py",
    ]
    for p in sorted((repo_root / "api" / "app").glob("*")):
        if p.is_file() and p.suffix in {".py", ".html"}:
            allowed_files.append(p)
    for p in sorted((repo_root / "agents").rglob("*")):
        if p.is_file() and p.suffix in {".py", ".md"} and "__pycache__" not in p.parts:
            allowed_files.append(p)
    for p in sorted((repo_root / "skills").rglob("*")):
        if p.is_file() and p.suffix in {".md", ".json"} and "__pycache__" not in p.parts:
            allowed_files.append(p)
    for p in sorted((repo_root / "browser_extension").rglob("*")):
        if p.is_file() and p.suffix in {".json", ".js", ".md"} and "__pycache__" not in p.parts:
            allowed_files.append(p)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for file_path in allowed_files:
            rel = file_path.relative_to(repo_root).as_posix()
            # Double-check no forbidden path is ever included
            lower_rel = rel.lower()
            if any(
                bad in lower_rel
                for bad in (".env", ".azure", "passcode", "secret", "test_", "check_portal_browser", "__pycache__")
            ):
                continue
            if rel == ".dockerignore" and include_private_replay:
                content = file_path.read_bytes() + b"\n!api/private_replay/\n!api/private_replay/**\n"
                entry = tarfile.TarInfo(rel)
                entry.size = len(content)
                entry.mode = 0o644
                tar.addfile(entry, io.BytesIO(content))
            else:
                tar.add(str(file_path), arcname=rel, recursive=False)
        report_path = repo_root / "docs" / "browser-benchmark-runs.json"
        if include_private_replay and report_path.is_file():
            recordings = json.loads(report_path.read_text(encoding="utf-8")).get("runs", [])
            ids = {row.get("visual_evidence_id", "") for row in recordings}
            assets: set[str] = set()
            for run_id in sorted(ids):
                if not re.fullmatch(r"[a-f0-9]{12}", run_id):
                    continue
                evidence_path = repo_root / "api" / "private_replay" / f"run-{run_id}.json"
                if not evidence_path.is_file():
                    continue
                saved = json.loads(evidence_path.read_text(encoding="utf-8"))
                saved = {key: saved[key] for key in ("method", "sources", "screenshots", "baseline", "run_id", "community_actions")
                         if key in saved}
                content = json.dumps(saved, ensure_ascii=True).encode("utf-8")
                entry = tarfile.TarInfo(f"api/private_replay/run-{run_id}.json")
                entry.size = len(content)
                entry.mode = 0o644
                tar.addfile(entry, io.BytesIO(content))
                for capture in saved.get("screenshots", []):
                    digest = capture.get("sha256", "")
                    if re.fullmatch(r"[a-f0-9]{64}", digest):
                        assets.add(digest)
            for digest in sorted(assets):
                asset = repo_root / "api" / "private_replay" / "assets" / f"{digest}.png"
                if not asset.is_file():
                    continue
                content = asset.read_bytes()
                if not content.startswith(b"\x89PNG\r\n\x1a\n") or hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError("Private replay capture failed PNG/hash verification.")
                tar.add(str(asset), arcname=f"api/private_replay/assets/{digest}.png", recursive=False)
    return buf.getvalue()


def ensure_role_assignment(
    arm: ArmClient,
    *,
    scope: str,
    role_definition_guid: str,
    principal_id: str,
) -> str:
    role_def_id = f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/{role_definition_guid}"
    deterministic_name = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{role_def_id}|{principal_id}"))
    url = f"{scope}/providers/Microsoft.Authorization/roleAssignments/{deterministic_name}?api-version=2022-04-01"
    body = {
        "properties": {
            "roleDefinitionId": role_def_id,
            "principalId": principal_id,
            "principalType": "ServicePrincipal",
        }
    }
    res = arm.request("PUT", url, json_body=body, expected_statuses=(200, 201, 409))
    if res.status_code == 409 and "RoleAssignmentExists" not in res.text:
        raise RuntimeError(f"Unexpected 409 on role assignment {url}: {res.text[:400]}")
    return deterministic_name


def build_image_in_acr(arm: ArmClient, rg_path: str, image_tag: str, *, include_private_replay: bool = False) -> str:
    acr_path = f"{rg_path}/providers/Microsoft.ContainerRegistry/registries/{ACR_NAME}"
    upload_res = arm.request(
        "POST",
        f"{acr_path}/listBuildSourceUploadUrl?api-version=2019-06-01-preview",
        expected_statuses=(200,),
    ).json()
    upload_url = upload_res["uploadUrl"]
    relative_path = upload_res["relativePath"]

    tar_bytes = build_filtered_source_tarball(REPO_ROOT, include_private_replay=include_private_replay)
    print(f"[ACR] Uploading filtered build context ({len(tar_bytes)} bytes)...")
    blob_res = httpx.put(
        upload_url,
        content=tar_bytes,
        headers={
            "x-ms-blob-type": "BlockBlob",
            "Content-Length": str(len(tar_bytes)),
        },
        timeout=120.0,
    )
    if blob_res.status_code not in (200, 201):
        raise RuntimeError(f"ACR source blob upload failed with HTTP {blob_res.status_code}: {blob_res.text[:400]}")

    schedule_body = {
        "type": "DockerBuildRequest",
        "imageNames": [f"pdd-portal:{image_tag}", "pdd-portal:latest"],
        "isPushEnabled": True,
        "noCache": False,
        "dockerFilePath": "Dockerfile",
        "sourceLocation": relative_path,
        "platform": {"os": "Linux", "architecture": "amd64"},
    }
    run_res = arm.request(
        "POST",
        f"{acr_path}/scheduleRun?api-version=2019-06-01-preview",
        json_body=schedule_body,
        expected_statuses=(200, 201),
    ).json()
    run_id = run_res["name"]
    print(f"[ACR] Scheduled ACR build runId={run_id} for pdd-portal:{image_tag}...")

    deadline = time.time() + 600
    while time.time() < deadline:
        status_data = arm.request(
            "GET",
            f"{acr_path}/runs/{run_id}?api-version=2019-06-01-preview",
            expected_statuses=(200,),
        ).json()
        run_status = (status_data.get("properties") or {}).get("status")
        if run_status == "Succeeded":
            print(f"[ACR] Build {run_id} Succeeded.")
            return f"{ACR_NAME}.azurecr.io/pdd-portal:{image_tag}"
        if run_status in ("Failed", "Canceled", "Error", "Timeout"):
            log_url_res = arm.request(
                "POST",
                f"{acr_path}/runs/{run_id}/listLogSasUrl?api-version=2019-06-01-preview",
                expected_statuses=(200,),
            ).json()
            log_link = log_url_res.get("logLink")
            log_tail = ""
            if log_link:
                log_tail = httpx.get(log_link, timeout=30.0).text[-2000:]
            raise RuntimeError(f"ACR build {run_id} ended with status={run_status}.\nLog tail:\n{log_tail}")
        time.sleep(6)

    raise TimeoutError(f"Timed out waiting for ACR build {run_id}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy Product Due-Diligence Assistant to Azure Container Apps.")
    parser.add_argument("--passcode-file", type=Path, default=DEFAULT_PASSCODE_FILE)
    parser.add_argument("--rotate-secrets", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--image-tag", type=str, default=f"v{int(time.time())}")
    parser.add_argument("--apply", action="store_true", help="Explicitly create the planned Azure resources.")
    parser.add_argument("--include-private-replay", action="store_true", help="Opt in to packaging permitted private benchmark evidence; never use for a public image.")
    args = parser.parse_args()

    evidence_url = os.environ.get("PDD_EVIDENCE_BLOB_ACCOUNT_URL", "").rstrip("/")
    try:
        require_configuration({
            "FOUNDRY_TENANT_ID": TENANT_ID, "FOUNDRY_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
            "FOUNDRY_RESOURCE_GROUP": app_config.FOUNDRY_RESOURCE_GROUP,
            "FOUNDRY_ACCOUNT_NAME": FOUNDRY_ACCOUNT_NAME, "FOUNDRY_PROJECT_NAME": FOUNDRY_PROJECT_NAME,
            "FOUNDRY_PROJECT_ENDPOINT": FOUNDRY_PROJECT_ENDPOINT,
            "FOUNDRY_MODEL_DEPLOYMENT": FOUNDRY_MODEL_DEPLOYMENT, "FOUNDRY_TOOLBOX_NAME": FOUNDRY_TOOLBOX_NAME,
            "PDD_AZURE_LOCATION": LOCATION, "PDD_ACR_NAME": ACR_NAME,
            "PDD_EVIDENCE_BLOB_ACCOUNT_URL": evidence_url,
        })
        if not re.fullmatch(r"https://[a-z0-9]{3,24}\.blob\.core\.windows\.net", evidence_url):
            raise ValueError("PDD_EVIDENCE_BLOB_ACCOUNT_URL must be an Azure Blob account HTTPS URL.")
    except ValueError as exc:
        parser.error(str(exc))
    if not args.apply:
        print(json.dumps({
            "status": "plan_only", "subscription": SUBSCRIPTION_ID, "resource_group": RESOURCE_GROUP,
            "location": LOCATION, "container_app": CA_NAME, "registry": ACR_NAME,
            "evidence_account_url": evidence_url, "include_private_replay": args.include_private_replay,
            "next_step": "Review prerequisites in docs/DEPLOYMENT.md; use --apply only for a new application.",
        }))
        return 0

    arm = ArmClient()
    rg_path = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    ca_path = f"{rg_path}/providers/Microsoft.App/containerApps/{CA_NAME}"
    existing_app = arm.request("GET", ca_path + "?api-version=2024-03-01", expected_statuses=(200, 404))
    if existing_app.status_code == 200:
        raise RuntimeError("Bootstrap will not overwrite an existing application. Use an image-only update that preserves its identity, secrets and environment.")

    # 1. Load or generate temporary passcode + session signing secret (never printed)
    sec = load_or_create_secrets(args.passcode_file, rotate=args.rotate_secrets)
    print(
        f"[AUTH] Passcode secret ready (sha256 prefix={sec['passcode_sha256'][:12]}, file={args.passcode_file})."
    )

    # 2. Ensure the configured user-assigned managed identity.
    uami_path = f"{rg_path}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{UAMI_NAME}"
    print(f"[ARM] Ensuring User-Assigned Managed Identity {UAMI_NAME}...")
    uami_data = arm.request(
        "PUT",
        f"{uami_path}?api-version=2023-01-31",
        json_body={"location": LOCATION},
        expected_statuses=(200, 201),
    ).json()
    uami_id = uami_data["id"]
    uami_principal_id = uami_data["properties"]["principalId"]
    uami_client_id = uami_data["properties"]["clientId"]
    print(f"[ARM] UAMI ready: principalId={uami_principal_id}, clientId={uami_client_id}")

    # 3. Ensure the configured private registry.
    acr_path = f"{rg_path}/providers/Microsoft.ContainerRegistry/registries/{ACR_NAME}"
    print(f"[ARM] Ensuring Azure Container Registry {ACR_NAME} (Basic SKU, adminUserEnabled=false)...")
    arm.request(
        "PUT",
        f"{acr_path}?api-version=2023-07-01",
        json_body={
            "location": LOCATION,
            "sku": {"name": "Basic"},
            "properties": {"adminUserEnabled": False},
        },
        expected_statuses=(200, 201, 202),
    )
    arm.wait_for_provisioning(acr_path, "2023-07-01", timeout_sec=180)
    print(f"[ARM] ACR {ACR_NAME} ready.")

    # 4. Ensure RBAC role assignments for `uami-pdd-portal`
    foundry_rg = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{app_config.FOUNDRY_RESOURCE_GROUP}"
    foundry_account_scope = f"{foundry_rg}/providers/Microsoft.CognitiveServices/accounts/{FOUNDRY_ACCOUNT_NAME}"
    foundry_project_scope = f"{foundry_account_scope}/projects/{FOUNDRY_PROJECT_NAME}"
    print(f"[RBAC] Ensuring AcrPull on {ACR_NAME} and Foundry User on {FOUNDRY_ACCOUNT_NAME} for {UAMI_NAME}...")
    ensure_role_assignment(arm, scope=acr_path, role_definition_guid=ROLE_ACR_PULL, principal_id=uami_principal_id)
    ensure_role_assignment(
        arm,
        scope=foundry_account_scope,
        role_definition_guid=ROLE_FOUNDRY_USER,
        principal_id=uami_principal_id,
    )
    ensure_role_assignment(
        arm,
        scope=foundry_project_scope,
        role_definition_guid=ROLE_FOUNDRY_USER,
        principal_id=uami_principal_id,
    )
    print("[RBAC] Managed Identity role assignments verified.")

    # 5. Build and push image in ACR
    if args.skip_build:
        image_ref = f"{ACR_NAME}.azurecr.io/pdd-portal:{args.image_tag}"
    else:
        image_ref = build_image_in_acr(arm, rg_path, args.image_tag, include_private_replay=args.include_private_replay)

    # 6. Ensure the configured Container Apps environment.
    cae_path = f"{rg_path}/providers/Microsoft.App/managedEnvironments/{CAE_NAME}"
    existing_cae = arm.request("GET", f"{cae_path}?api-version=2024-03-01", expected_statuses=(200, 404))
    if existing_cae.status_code == 200:
        existing_state = (existing_cae.json().get("properties") or {}).get("provisioningState")
        if existing_state == "Failed" or existing_cae.json()["location"].lower() != ACA_LOCATION.lower():
            raise RuntimeError("The existing environment failed or uses a different region. Choose another name or resolve it explicitly; nothing will be deleted.")

    print(f"[ARM] Ensuring Container Apps Environment {CAE_NAME} in {RESOURCE_GROUP} ({ACA_LOCATION}, Consumption-only stamp)...")
    arm.request(
        "PUT",
        f"{cae_path}?api-version=2024-03-01",
        json_body={
            "location": ACA_LOCATION,
            "properties": {
                "appLogsConfiguration": {"destination": "azure-monitor"},
            },
        },
        expected_statuses=(200, 201, 202),
    )
    cae_data = arm.wait_for_provisioning(cae_path, "2024-03-01", timeout_sec=420)
    default_domain = cae_data["properties"]["defaultDomain"]
    expected_fqdn = f"{CA_NAME}.{default_domain}"
    portal_url = f"https://{expected_fqdn}"
    print(f"[ARM] Container Apps Environment ready: defaultDomain={default_domain}, expected FQDN={expected_fqdn}")

    # 7. Deploy Container App (`ca-pdd-portal`) with passcode secrets and trusted ACA ingress
    ca_path = f"{rg_path}/providers/Microsoft.App/containerApps/{CA_NAME}"
    print(f"[ARM] Deploying Container App {CA_NAME} in {RESOURCE_GROUP} ({ACA_LOCATION}) with 1 replica and passcode protection...")
    ca_body = {
        "location": ACA_LOCATION,
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {uami_id: {}},
        },
        "properties": {
            "managedEnvironmentId": cae_data["id"],
            "configuration": {
                "activeRevisionsMode": "Single",
                "secrets": [
                    {"name": "pdd-demo-passcode", "value": sec["passcode"]},
                    {"name": "pdd-passcode-sha256", "value": sec["passcode_sha256"]},
                    {"name": "pdd-session-signing-secret", "value": sec["session_signing_secret"]},
                ],
                "registries": [
                    {
                        "server": f"{ACR_NAME}.azurecr.io",
                        "identity": uami_id,
                    }
                ],
                "ingress": {
                    "external": True,
                    "targetPort": 8000,
                    "transport": "auto",
                    "allowInsecure": False,
                },
            },
            "template": {
                "containers": [
                    {
                        "name": "pdd-portal",
                        "image": image_ref,
                        "resources": {"cpu": 0.5, "memory": "1Gi"},
                        "env": [
                            {"name": "PDD_HOSTED_MODE", "value": "true"},
                            {"name": "PDD_REQUIRE_PASSCODE", "value": "true"},
                            {"name": "PDD_TRUSTED_INGRESS_PROXY", "value": "aca"},
                            {"name": "PDD_ALLOWED_HOSTS", "value": expected_fqdn},
                            {"name": "PDD_ALLOWED_ORIGINS", "value": portal_url},
                            {"name": "FOUNDRY_MANAGED_IDENTITY_CLIENT_ID", "value": uami_client_id},
                            {"name": "FOUNDRY_TENANT_ID", "value": TENANT_ID},
                            {"name": "FOUNDRY_PROJECT_ENDPOINT", "value": FOUNDRY_PROJECT_ENDPOINT},
                            {"name": "FOUNDRY_MODEL_DEPLOYMENT", "value": FOUNDRY_MODEL_DEPLOYMENT},
                            {"name": "FOUNDRY_TOOLBOX_NAME", "value": FOUNDRY_TOOLBOX_NAME},
                            {"name": "PLAYWRIGHT_WORKSPACE_NAME", "value": PLAYWRIGHT_WORKSPACE_NAME},
                            {"name": "PLAYWRIGHT_CONNECTION_NAME", "value": PLAYWRIGHT_CONNECTION_NAME},
                            {"name": "PLAYWRIGHT_WORKSPACE_RESOURCE_ID", "value": app_config.PLAYWRIGHT_WORKSPACE_RESOURCE_ID},
                            {"name": "PDD_EVIDENCE_BLOB_ACCOUNT_URL", "value": evidence_url},
                            {"name": "PDD_EVIDENCE_BLOB_CONTAINER", "value": os.environ.get("PDD_EVIDENCE_BLOB_CONTAINER") or "research-evidence"},
                            {"name": "PDD_RUN_TIMEOUT_SECONDS", "value": str(app_config.PDD_RUN_TIMEOUT_SECONDS)},
                            {"name": "PDD_MAX_CONCURRENT_RUNS", "value": str(app_config.PDD_MAX_CONCURRENT_RUNS)},
                            {"name": "PDD_MAX_CONCURRENT_LIVE_RUNS", "value": str(app_config.PDD_MAX_CONCURRENT_LIVE_RUNS)},
                            {"name": "PDD_DEMO_PASSCODE", "secretRef": "pdd-demo-passcode"},
                            {"name": "PDD_PASSCODE_SHA256", "secretRef": "pdd-passcode-sha256"},
                            {"name": "PDD_SESSION_SIGNING_SECRET", "secretRef": "pdd-session-signing-secret"},
                        ],
                    }
                ],
                "scale": {"minReplicas": 1, "maxReplicas": 1},
            },
        },
    }
    arm.request(
        "PUT",
        f"{ca_path}?api-version=2024-03-01",
        json_body=ca_body,
        expected_statuses=(200, 201, 202),
    )
    ca_data = arm.wait_for_provisioning(ca_path, "2024-03-01", timeout_sec=420)
    actual_fqdn = ((ca_data.get("properties") or {}).get("configuration") or {}).get("ingress", {}).get(
        "fqdn", expected_fqdn
    )
    actual_url = f"https://{actual_fqdn}"

    # Write final delivery file with actual HTTPS URL (never printing passcode to stdout)
    write_passcode_delivery_file(
        args.passcode_file,
        portal_url=actual_url,
        passcode=sec["passcode"],
        passcode_sha256=sec["passcode_sha256"],
    )

    summary = {
        "status": "succeeded",
        "portal_url": actual_url,
        "resource_group": RESOURCE_GROUP,
        "location": LOCATION,
        "managed_identity": {
            "name": UAMI_NAME,
            "id": uami_id,
            "principal_id": uami_principal_id,
            "client_id": uami_client_id,
        },
        "acr": f"{ACR_NAME}.azurecr.io",
        "image": image_ref,
        "container_apps_environment": CAE_NAME,
        "container_app": CA_NAME,
        "passcode_delivery_file": str(args.passcode_file),
        "passcode_sha256_prefix": sec["passcode_sha256"][:12],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
