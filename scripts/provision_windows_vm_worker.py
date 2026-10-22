"""Opt-in bootstrap for a new Windows VM browser worker, using operator-supplied configuration.

Preview by default; --apply creates billable resources. Existing VMs are not
overwritten, deleted or resized. The worker uses outbound HTTPS, not public RDP.
"""
from __future__ import annotations

import base64
import argparse
import json
import os
import re
import secrets
import string
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

import deploy_container_app as d  # noqa: E402

VM_NAME = os.environ.get("PDD_VM_NAME", "").strip() or "pdd-browser-worker"
VM_LOCATION = os.environ.get("PDD_VM_LOCATION", "").strip() or d.ACA_LOCATION
CANDIDATE_VM_SIZES = [os.environ.get("PDD_VM_SIZE", "").strip() or "Standard_D2s_v5"]
VM_SIZE = CANDIDATE_VM_SIZES[0]
VNET_NAME = f"{VM_NAME}-vnet"
SUBNET_NAME = "snet-worker"
NSG_NAME = f"{VM_NAME}-nsg"
PIP_NAME = f"{VM_NAME}-ip"
NIC_NAME = f"{VM_NAME}-nic"
ADMIN_USERNAME = "pddadmin"

DEFAULT_VM_CREDS_FILE = d.DEFAULT_PASSCODE_FILE.parent / "vm-credentials.json"


def load_or_create_vm_password(creds_file: Path) -> str:
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    if creds_file.is_file():
        try:
            data = json.loads(creds_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("admin_password"), str) and data["admin_password"]:
                return data["admin_password"]
        except (OSError, ValueError, TypeError) as exc:
            raise ValueError("The private VM credentials file cannot be read; refusing to replace its password.") from exc
        raise ValueError("The private VM credentials file has no admin_password; refusing to replace it.")
    # Must satisfy Windows complexity requirements (upper, lower, digit, special, >=12 chars)
    alphabet = string.ascii_letters + string.digits
    pwd = (
        "Pdd!"
        + secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + "#"
        + "".join(secrets.choice(alphabet) for _ in range(14))
    )
    creds_file.write_text(
        json.dumps(
            {
                "vm_name": VM_NAME,
                "resource_group": d.RESOURCE_GROUP,
                "location": VM_LOCATION,
                "vm_size": VM_SIZE,
                "admin_username": ADMIN_USERNAME,
                "admin_password": pwd,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return pwd


def wait_for_arm_operation_or_resource(
    arm: d.ArmClient,
    resource_path: str,
    api_version: str,
    *,
    timeout_sec: int = 600,
) -> dict[str, Any]:
    return arm.wait_for_provisioning(resource_path, api_version, timeout_sec=timeout_sec)


def run_vm_powershell(
    arm: d.ArmClient,
    vm_path: str,
    script_lines: list[str],
    *,
    timeout_sec: int = 900,
    protected_parameters: dict[str, str] | None = None,
) -> dict[str, Any]:
    rc_path = f"{vm_path}/runCommands/BootstrapWorker"
    script_text = "\r\n".join(script_lines)
    arm.request(
        "PUT",
        f"{rc_path}?api-version=2024-03-01",
        json_body={
            "location": VM_LOCATION,
            "properties": {
                "source": {"script": script_text},
                "timeoutInSeconds": min(timeout_sec, 900),
                "asyncExecution": False,
                "treatFailureAsDeploymentFailure": True,
                "protectedParameters": [
                    {"name": name, "value": value} for name, value in (protected_parameters or {}).items()
                ],
            },
        },
        expected_statuses=(200, 201, 202),
    )

    expand_query = f"{rc_path}?{chr(36)}expand=instanceView&api-version=2024-03-01"
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        iv_res = arm.request("GET", expand_query, expected_statuses=(200,))
        body = iv_res.json()
        iv = (body.get("properties") or {}).get("instanceView") or {}
        state = iv.get("executionState", "")
        if state == "Succeeded":
            if iv.get("exitCode") not in (None, 0):
                raise RuntimeError("VM bootstrap exited unsuccessfully; inspect the protected worker setup and instance view.")
            return iv
        if state in ("Failed", "TimedOut", "Canceled"):
            raise RuntimeError(f"VM Managed RunCommand ended with executionState={state}: {json.dumps(iv)}")
        time.sleep(5)
    raise TimeoutError(f"Timed out waiting for VM Managed RunCommand on {vm_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Explicitly create and bootstrap the planned VM.")
    parser.add_argument("--passcode-file", type=Path, default=d.DEFAULT_PASSCODE_FILE)
    parser.add_argument("--credentials-file", type=Path, default=DEFAULT_VM_CREDS_FILE)
    args = parser.parse_args()
    portal_url = os.environ.get("PDD_PORTAL_URL", "").strip().rstrip("/")
    parsed = urlsplit(portal_url)
    if (not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", portal_url)
            or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        parser.error("Set PDD_PORTAL_URL to the actual portal HTTPS origin without credentials, a path or a query.")
    d.require_configuration({"PDD_VM_LOCATION or PDD_AZURE_LOCATION": VM_LOCATION,
                             "FOUNDRY_SUBSCRIPTION_ID": d.SUBSCRIPTION_ID,
                             "PDD_RESOURCE_GROUP": d.RESOURCE_GROUP})
    if not args.apply:
        print(json.dumps({"status": "plan_only", "vm": VM_NAME, "location": VM_LOCATION,
                          "size": VM_SIZE, "portal_url": portal_url, "public_inbound_ports": [],
                          "next_step": "Review docs/DEPLOYMENT.md and use --apply only for a new VM."}))
        return 0
    arm = d.ArmClient()
    rg_path = f"/subscriptions/{d.SUBSCRIPTION_ID}/resourceGroups/{d.RESOURCE_GROUP}"
    vm_path = f"{rg_path}/providers/Microsoft.Compute/virtualMachines/{VM_NAME}"
    if arm.request("GET", vm_path + "?api-version=2024-03-01", expected_statuses=(200, 404)).status_code == 200:
        raise RuntimeError("Bootstrap will not overwrite an existing VM. Use the manual worker installation/update instructions.")
    if not args.passcode_file.with_suffix(".json").is_file():
        raise ValueError("The deployed portal's private access JSON is required; VM setup never generates a different portal passcode.")
    sec = d.load_or_create_secrets(args.passcode_file, rotate=False)
    vm_password = load_or_create_vm_password(args.credentials_file)

    nsg_path = f"{rg_path}/providers/Microsoft.Network/networkSecurityGroups/{NSG_NAME}"
    vnet_path = f"{rg_path}/providers/Microsoft.Network/virtualNetworks/{VNET_NAME}"
    pip_path = f"{rg_path}/providers/Microsoft.Network/publicIPAddresses/{PIP_NAME}"
    nic_path = f"{rg_path}/providers/Microsoft.Network/networkInterfaces/{NIC_NAME}"

    # 1. Network Security Group (zero inbound ports exposed to internet; outbound HTTPS open)
    print(f"[ARM] Ensuring NSG {NSG_NAME} in {VM_LOCATION} (inbound internet blocked, outbound HTTPS allowed)...")
    arm.request(
        "PUT",
        f"{nsg_path}?api-version=2023-11-01",
        json_body={
            "location": VM_LOCATION,
            "properties": {
                "securityRules": [
                    {
                        "name": "DenyAllInboundInternet",
                        "properties": {
                            "protocol": "*",
                            "sourcePortRange": "*",
                            "destinationPortRange": "*",
                            "sourceAddressPrefix": "Internet",
                            "destinationAddressPrefix": "*",
                            "access": "Deny",
                            "priority": 4000,
                            "direction": "Inbound",
                        },
                    }
                ]
            },
        },
        expected_statuses=(200, 201),
    )
    nsg_data = wait_for_arm_operation_or_resource(arm, nsg_path, "2023-11-01", timeout_sec=180)

    # 2. Virtual Network + Subnet
    print(f"[ARM] Ensuring VNet {VNET_NAME} ({SUBNET_NAME}) in {VM_LOCATION}...")
    arm.request(
        "PUT",
        f"{vnet_path}?api-version=2023-11-01",
        json_body={
            "location": VM_LOCATION,
            "properties": {
                "addressSpace": {"addressPrefixes": ["10.42.0.0/24"]},
                "subnets": [
                    {
                        "name": SUBNET_NAME,
                        "properties": {
                            "addressPrefix": "10.42.0.0/24",
                            "networkSecurityGroup": {"id": nsg_data["id"]},
                        },
                    }
                ],
            },
        },
        expected_statuses=(200, 201),
    )
    vnet_data = wait_for_arm_operation_or_resource(arm, vnet_path, "2023-11-01", timeout_sec=180)
    subnet_id = vnet_data["properties"]["subnets"][0]["id"]

    # 3. Standard Static Public IP (for deterministic outbound IP + inspection)
    print(f"[ARM] Ensuring Public IP {PIP_NAME} in {VM_LOCATION}...")
    arm.request(
        "PUT",
        f"{pip_path}?api-version=2023-11-01",
        json_body={
            "location": VM_LOCATION,
            "sku": {"name": "Standard", "tier": "Regional"},
            "properties": {
                "publicIPAllocationMethod": "Static",
                "publicIPAddressVersion": "IPv4",
            },
        },
        expected_statuses=(200, 201),
    )
    pip_data = wait_for_arm_operation_or_resource(arm, pip_path, "2023-11-01", timeout_sec=180)
    public_ip = pip_data["properties"].get("ipAddress", "")
    print(f"[ARM] Public IP ready: {public_ip}")

    # 4. Network Interface
    print(f"[ARM] Ensuring NIC {NIC_NAME} in {VM_LOCATION}...")
    arm.request(
        "PUT",
        f"{nic_path}?api-version=2023-11-01",
        json_body={
            "location": VM_LOCATION,
            "properties": {
                "ipConfigurations": [
                    {
                        "name": "ipconfig1",
                        "properties": {
                            "privateIPAllocationMethod": "Dynamic",
                            "subnet": {"id": subnet_id},
                            "publicIPAddress": {"id": pip_data["id"]},
                        },
                    }
                ]
            },
        },
        expected_statuses=(200, 201),
    )
    nic_data = wait_for_arm_operation_or_resource(arm, nic_path, "2023-11-01", timeout_sec=180)

    # 5. Windows Server 2022 VM (minimum available 2-vCPU Gen2 size, 30 GB SmallDisk Gen2)
    vm_path = f"{rg_path}/providers/Microsoft.Compute/virtualMachines/{VM_NAME}"
    existing_vm = arm.request("GET", f"{vm_path}?api-version=2024-03-01", expected_statuses=(200, 404))
    selected_vm_size = VM_SIZE
    if existing_vm.status_code == 404:
        created_ok = False
        for candidate_size in CANDIDATE_VM_SIZES:
            selected_vm_size = candidate_size
            print(f"[ARM] Provisioning Windows Server 2022 VM {VM_NAME} ({candidate_size}) in {VM_LOCATION}...")
            try:
                arm.request(
                    "PUT",
                    f"{vm_path}?api-version=2024-03-01",
                    json_body={
                        "location": VM_LOCATION,
                        "properties": {
                            "hardwareProfile": {"vmSize": candidate_size},
                            "storageProfile": {
                                "imageReference": {
                                    "publisher": "MicrosoftWindowsServer",
                                    "offer": "WindowsServer",
                                    "sku": "2022-datacenter-smalldisk-g2",
                                    "version": "latest",
                                },
                                "osDisk": {
                                    "createOption": "FromImage",
                                    "managedDisk": {"storageAccountType": "StandardSSD_LRS"},
                                    "deleteOption": "Delete",
                                },
                            },
                            "osProfile": {
                                "computerName": "vmpddchrome",
                                "adminUsername": ADMIN_USERNAME,
                                "adminPassword": vm_password,
                                "windowsConfiguration": {
                                    "provisionVMAgent": True,
                                    "enableAutomaticUpdates": True,
                                },
                            },
                            "networkProfile": {
                                "networkInterfaces": [
                                    {
                                        "id": nic_data["id"],
                                        "properties": {"deleteOption": "Delete"},
                                    }
                                ]
                            },
                        },
                    },
                    expected_statuses=(200, 201),
                )
                created_ok = True
                break
            except RuntimeError as exc:
                if "SkuNotAvailable" in str(exc) or "QuotaExceeded" in str(exc):
                    raise RuntimeError(f"Configured VM size {candidate_size} is unavailable in {VM_LOCATION}; no alternative will be provisioned.") from exc
                raise
        if not created_ok:
            raise RuntimeError(f"None of the candidate VM sizes {CANDIDATE_VM_SIZES} could be created in {VM_LOCATION}.")
    else:
        raise RuntimeError("A VM with this name appeared during setup; refusing to bootstrap an existing VM.")

    if existing_vm.status_code == 404:
        wait_for_arm_operation_or_resource(arm, vm_path, "2024-03-01", timeout_sec=600)
    print(f"[ARM] Windows VM {VM_NAME} ({selected_vm_size}, IP={public_ip}) is Ready!")

    # 6. Bootstrap Native Google Chrome + Python 3.12 + Playwright + Worker via Azure VM runCommand
    worker_py_bytes = (REPO_ROOT / "scripts" / "windows_playwright_worker.py").read_bytes()
    worker_b64 = base64.b64encode(worker_py_bytes).decode("ascii")
    community_b64 = base64.b64encode((REPO_ROOT / "browser_extension" / "community_snapshot.js").read_bytes()).decode("ascii")
    bootstrap_ps = [
        "param([Parameter(Mandatory=$true)][string]$PortalPasscode)",
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12",
        "New-Item -ItemType Directory -Force -Path 'C:\\pdd-worker' | Out-Null",
        "& icacls.exe 'C:\\pdd-worker' /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null",
        "if ($LASTEXITCODE -ne 0) { throw 'Could not restrict worker-directory access.' }",
        # 1. Install Google Chrome Enterprise (64-bit MSI) if not present
        "$chromeExe = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'",
        "if (-not (Test-Path $chromeExe)) {",
        "    Write-Output '[BOOTSTRAP] Downloading Google Chrome Enterprise 64-bit MSI...'",
        "    $chromeMsi = 'C:\\pdd-worker\\googlechromestandaloneenterprise64.msi'",
        "    Invoke-WebRequest -Uri 'https://dl.google.com/dl/chrome/install/googlechromestandaloneenterprise64.msi' -OutFile $chromeMsi -UseBasicParsing",
        "    Start-Process msiexec.exe -ArgumentList '/i', $chromeMsi, '/qn', '/norestart' -Wait",
        "}",
        # 2. Install Python 3.12 (64-bit) if not present
        "$pyExe = 'C:\\Program Files\\Python312\\python.exe'",
        "if (-not (Test-Path $pyExe)) {",
        "    Write-Output '[BOOTSTRAP] Downloading Python 3.12.8 amd64 installer...'",
        "    $pyInstaller = 'C:\\pdd-worker\\python-3.12.8-amd64.exe'",
        "    Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe' -OutFile $pyInstaller -UseBasicParsing",
        "    Start-Process $pyInstaller -ArgumentList '/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0' -Wait",
        "}",
        # 3. Install httpx and playwright
        "& $pyExe -m pip install --quiet --disable-pip-version-check httpx playwright",
        "if ($LASTEXITCODE -ne 0) { throw 'Worker dependency installation failed.' }",
        # 4. Write windows_playwright_worker.py and runner script
        f"$workerBytes = [Convert]::FromBase64String('{worker_b64}')",
        "[IO.File]::WriteAllBytes('C:\\pdd-worker\\windows_playwright_worker.py', $workerBytes)",
        f"[IO.File]::WriteAllBytes('C:\\pdd-worker\\community_snapshot.js', [Convert]::FromBase64String('{community_b64}'))",
        f"$access = @{{portal_url='{portal_url}';passcode=$PortalPasscode}}",
        "[IO.File]::WriteAllText('C:\\pdd-worker\\worker-access.json', ($access | ConvertTo-Json))",
        "$runnerContent = @(",
        "  '$config = Get-Content -LiteralPath ''C:\\pdd-worker\\worker-access.json'' -Raw | ConvertFrom-Json',",
        "  '$env:PDD_PORTAL_URL = $config.portal_url',",
        "  '$env:PDD_DEMO_PASSCODE = $config.passcode',",
        "  '& \"C:\\Program Files\\Python312\\python.exe\" -u \"C:\\pdd-worker\\windows_playwright_worker.py\" *> \"C:\\pdd-worker\\worker.log\"'",
        ") -join \"`r`n\"",
        "[IO.File]::WriteAllText('C:\\pdd-worker\\run_worker.ps1', $runnerContent)",
        # The new VM has no worker to stop; register only this application's task.
        "$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\\pdd-worker\\run_worker.ps1'",
        "$trigger = New-ScheduledTaskTrigger -AtStartup",
        "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
        "$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest",
        "Register-ScheduledTask -TaskName 'PDDPlaywrightChromeWorker' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null",
        "Start-ScheduledTask -TaskName 'PDDPlaywrightChromeWorker'",
        "Start-Sleep -Seconds 8",
        "$chromeVer = (Get-Item $chromeExe).VersionInfo.ProductVersion",
        "Write-Output ('[BOOTSTRAP] Chrome=' + $chromeVer)",
        "if (Test-Path 'C:\\pdd-worker\\worker.log') { Get-Content 'C:\\pdd-worker\\worker.log' -Tail 20 }",
    ]

    print(f"[VM] Bootstrapping Google Chrome + Python 3.12 + Playwright + Worker on {VM_NAME} via Managed RunCommand...")
    cmd_res = run_vm_powershell(arm, vm_path, bootstrap_ps, timeout_sec=900,
                               protected_parameters={"PortalPasscode": sec["passcode"]})
    print("[VM] Bootstrap completed:")
    print(cmd_res.get("output") or json.dumps(cmd_res, indent=2)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
