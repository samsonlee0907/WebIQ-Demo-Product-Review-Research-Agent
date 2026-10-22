"""Offline publication contracts; no cloud resources or private operator files."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest.mock import Mock

import httpx
import pytest
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import deploy_container_app as deploy
from scripts import provision_evidence_storage as storage
from scripts import provision_windows_vm_worker as vm
from scripts import windows_playwright_worker as worker


@pytest.fixture
def configured_deploy(monkeypatch, tmp_path):
    for name, value in {
        "TENANT_ID": "11111111-1111-4111-8111-111111111111",
        "SUBSCRIPTION_ID": "22222222-2222-4222-8222-222222222222",
        "RESOURCE_GROUP": "rg-example",
        "FOUNDRY_ACCOUNT_NAME": "example-foundry",
        "FOUNDRY_PROJECT_NAME": "example-project",
        "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/example",
        "FOUNDRY_MODEL_DEPLOYMENT": "research-model",
        "FOUNDRY_TOOLBOX_NAME": "research-tools",
        "LOCATION": "eastus2",
        "ACR_NAME": "examplebuildregistry",
    }.items():
        monkeypatch.setattr(deploy, name, value)
    monkeypatch.setattr(deploy.app_config, "FOUNDRY_RESOURCE_GROUP", "rg-example-foundry")
    monkeypatch.setenv("PDD_EVIDENCE_BLOB_ACCOUNT_URL", "https://exampleevidence.blob.core.windows.net")
    monkeypatch.setattr(sys, "argv", ["deploy", "--passcode-file", str(tmp_path / "access.txt")])
    return tmp_path / "access.txt"


def test_clean_checkout_and_template_are_offline(tmp_path):
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("FOUNDRY_", "PLAYWRIGHT_", "AZURE_", "PDD_"))
           and key != "TOOLBOX_NAME"}
    template = dotenv_values(ROOT / ".env.example")
    for key, value in template.items():
        if key.startswith(("FOUNDRY_", "PLAYWRIGHT_")):
            assert not value, key
    env.update({key: value for key, value in template.items() if value is not None})
    env["PDD_EVIDENCE_DIRECTORY"] = str(tmp_path / "history")
    result = subprocess.run(
        [sys.executable, "-c", """
from fastapi.testclient import TestClient
from api.app import config, main
from api.app.evidence_archive import get_store
assert not config.FOUNDRY_PROJECT_ENDPOINT
assert not config.FOUNDRY_MODEL_DEPLOYMENT
assert not config.FOUNDRY_AUTHORIZED_OBJECT_ID
assert not config.PLAYWRIGHT_WORKSPACE_RESOURCE_ID
with TestClient(main.app) as client:
    assert client.get('/').status_code == 200
    health = client.get('/api/health').raise_for_status().json()
    assert 'Offline walkthroughs' in health['mode_label']
    assert health['capabilities']['model_synthesis']['live_phase1_state'] == 'configuration_required'
    assert health['phase1_live_config']['foundry_model_deployment'] == ''
assert get_store().root is not None
print('clean-template-ok')
"""],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean-template-ok" in result.stdout


def test_portal_plan_never_authenticates_or_writes_secrets(configured_deploy, monkeypatch, capsys):
    arm = Mock(side_effect=AssertionError("A preview must not authenticate"))
    monkeypatch.setattr(deploy, "ArmClient", arm)
    assert deploy.main() == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "plan_only"
    assert plan["include_private_replay"] is False
    assert not configured_deploy.exists()
    assert not configured_deploy.with_suffix(".json").exists()
    arm.assert_not_called()


def test_missing_configuration_fails_before_cloud_calls(configured_deploy, monkeypatch):
    monkeypatch.setattr(deploy, "FOUNDRY_PROJECT_ENDPOINT", "")
    arm = Mock(side_effect=AssertionError("Missing config must fail before auth"))
    monkeypatch.setattr(deploy, "ArmClient", arm)
    with pytest.raises(SystemExit) as error:
        deploy.main()
    assert error.value.code == 2
    arm.assert_not_called()


def test_existing_portal_is_not_overwritten(configured_deploy, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["deploy", "--apply", "--passcode-file", str(configured_deploy)])
    arm = Mock()
    arm.request.return_value = httpx.Response(200, json={})
    monkeypatch.setattr(deploy, "ArmClient", Mock(return_value=arm))
    with pytest.raises(RuntimeError, match="will not overwrite"):
        deploy.main()
    assert [call.args[0] for call in arm.request.call_args_list] == ["GET"]
    assert not configured_deploy.with_suffix(".json").exists()


@pytest.mark.parametrize("content", ["[]", "null", "{", '{"passcode":"short"}'])
def test_invalid_access_file_is_not_silently_regenerated(tmp_path, content):
    access = tmp_path / "access.txt"
    state = access.with_suffix(".json")
    state.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="private access file"):
        deploy.load_or_create_secrets(access)
    assert state.read_text(encoding="utf-8") == content


def test_vm_plan_does_not_authenticate_or_invent_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PDD_PORTAL_URL", "https://portal.example")
    monkeypatch.setattr(vm, "VM_LOCATION", "eastus2")
    monkeypatch.setattr(vm.d, "SUBSCRIPTION_ID", "example-subscription")
    monkeypatch.setattr(vm.d, "RESOURCE_GROUP", "example-group")
    monkeypatch.setattr(sys, "argv", ["vm", "--credentials-file", str(tmp_path / "vm.json"),
                                     "--passcode-file", str(tmp_path / "access.txt")])
    arm = Mock(side_effect=AssertionError("A VM preview must not authenticate"))
    monkeypatch.setattr(vm.d, "ArmClient", arm)
    assert vm.main() == 0
    assert json.loads(capsys.readouterr().out)["status"] == "plan_only"
    assert not list(tmp_path.iterdir())
    arm.assert_not_called()


def test_existing_vm_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("PDD_PORTAL_URL", "https://portal.example")
    monkeypatch.setattr(vm, "VM_LOCATION", "eastus2")
    monkeypatch.setattr(vm.d, "SUBSCRIPTION_ID", "example-subscription")
    monkeypatch.setattr(vm.d, "RESOURCE_GROUP", "example-group")
    monkeypatch.setattr(sys, "argv", ["vm", "--apply", "--credentials-file", str(tmp_path / "vm.json")])
    arm = Mock()
    arm.request.return_value = httpx.Response(200, json={})
    monkeypatch.setattr(vm.d, "ArmClient", Mock(return_value=arm))
    with pytest.raises(RuntimeError, match="will not overwrite"):
        vm.main()
    assert [call.args[0] for call in arm.request.call_args_list] == ["GET"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("url", ["http://portal.example", "https://portal.example/path",
                                "https://portal.example?token=example", "https://portal'.example"])
def test_vm_rejects_unsafe_or_non_origin_portal_url(monkeypatch, url):
    monkeypatch.setenv("PDD_PORTAL_URL", url)
    monkeypatch.setattr(sys, "argv", ["vm"])
    arm = Mock(side_effect=AssertionError("Invalid URL must fail before auth"))
    monkeypatch.setattr(vm.d, "ArmClient", arm)
    with pytest.raises(SystemExit) as error:
        vm.main()
    assert error.value.code == 2
    arm.assert_not_called()


def test_run_command_uses_protected_parameter_not_source():
    arm = Mock()
    arm.request.side_effect = [
        httpx.Response(202),
        httpx.Response(200, json={"properties": {"instanceView": {"executionState": "Succeeded", "exitCode": 0}}}),
    ]
    secret = "fixture-only-worker-passcode"
    vm.run_vm_powershell(arm, "/example/vm", ["param([string]$PortalPasscode)", "Write-Output 'ready'"],
                         protected_parameters={"PortalPasscode": secret})
    properties = arm.request.call_args_list[0].kwargs["json_body"]["properties"]
    assert secret not in properties["source"]["script"]
    assert properties["protectedParameters"] == [{"name": "PortalPasscode", "value": secret}]
    assert properties["treatFailureAsDeploymentFailure"] is True


def test_run_command_nonzero_exit_is_not_success():
    arm = Mock()
    arm.request.side_effect = [
        httpx.Response(202),
        httpx.Response(200, json={"properties": {"instanceView": {"executionState": "Succeeded", "exitCode": 7}}}),
    ]
    with pytest.raises(RuntimeError, match="exited unsuccessfully"):
        vm.run_vm_powershell(arm, "/example/vm", ["exit 7"])


def test_generated_vm_bootstrap_is_valid_powershell():
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("PowerShell parser is required to inspect the Windows bootstrap")
    tree = ast.parse(Path(vm.__file__).read_text(encoding="utf-8"))
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "bootstrap_ps" for target in node.targets))
    lines = eval(compile(ast.Expression(assignment.value), "<bootstrap-fixture>", "eval"), {}, {
        "portal_url": "https://portal.example", "worker_b64": "Zml4dHVyZQ==", "community_b64": "Zml4dHVyZQ==",
    })
    parsed = subprocess.run([pwsh, "-NoProfile", "-Command", """
$tokens = $null; $errors = $null
[System.Management.Automation.Language.Parser]::ParseInput([Console]::In.ReadToEnd(), [ref]$tokens, [ref]$errors) | Out-Null
if ($errors) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }
"""], input="\n".join(lines), capture_output=True, text=True, timeout=30)
    assert parsed.returncode == 0, parsed.stdout + parsed.stderr
    assert "$PortalPasscode" in lines[0]
    assert not any("Stop-Process" in line or "Get-Process" in line for line in lines)


@pytest.mark.parametrize("url", ["", "http://portal.example", "https://portal.example/path"])
def test_standalone_worker_requires_safe_portal_origin(monkeypatch, url):
    monkeypatch.setenv("PDD_PORTAL_URL", url)
    monkeypatch.setattr(sys, "argv", ["worker", "--once"])
    run = Mock(side_effect=AssertionError("Invalid origin must not start the worker"))
    monkeypatch.setattr(worker, "run_worker_loop", run)
    with pytest.raises(SystemExit) as error:
        worker.main()
    assert error.value.code == 2
    run.assert_not_called()


def test_evidence_storage_preview_does_not_authenticate(monkeypatch):
    for name, value in {"SUBSCRIPTION_ID": "example-subscription", "RESOURCE_GROUP": "example-group",
                        "ACA_LOCATION": "eastus2"}.items():
        monkeypatch.setattr(storage, name, value)
    arm = Mock(side_effect=AssertionError("A storage preview must not authenticate"))
    monkeypatch.setattr(storage, "ArmClient", arm)
    plan = storage.provision(apply=False, enforced_perimeter=True)
    assert not plan["applied"]
    assert plan["public_network_access"] == "SecuredByPerimeter"
    assert not plan["soft_delete"] and not plan["versioning"] and not plan["shared_keys"]
    arm.assert_not_called()


def test_extension_only_trusts_loopback_portal_by_default():
    manifest = json.loads((ROOT / "browser_extension" / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["content_scripts"][0]["matches"]) == {"http://127.0.0.1/*", "http://localhost/*"}
    assert "https://*/*" in manifest["host_permissions"]


def test_served_portal_does_not_claim_a_fixed_operator_deployment():
    text = (ROOT / "api" / "app" / "portal.html").read_text(encoding="utf-8")
    assert "GPT-5.6-Terra" not in text
    assert "gpt-5.6-terra" not in text
    assert "reuses Foundry account" not in text
    assert "read-only local checklist" in text


def test_public_guides_have_resolvable_relative_links_and_neutral_examples():
    paths = [ROOT / "README.md", ROOT / "browser_extension" / "README.md"]
    paths += [ROOT / "docs" / name for name in ("SETUP.md", "DEPLOYMENT.md", "USER_GUIDE.md", "PUBLISHING.md")]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\b(?:sony|dyson|bose)\b|WH-1000XM6|yellowpond|6a7da9fe", text, re.IGNORECASE), path
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            assert (path.parent / target.split("#")[0]).resolve().exists(), (path, target)
    assert (ROOT / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")
