"""Provision the explicitly approved private evidence store, not a public content archive.

Uses the existing tenant-pinned management credential. No storage keys, SAS tokens,
anonymous access, soft-deleted copies, or automatic expiry rules are introduced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_container_app import (
    ACA_LOCATION, ArmClient, RESOURCE_GROUP, SUBSCRIPTION_ID, UAMI_NAME, require_configuration,
)

API_VERSION = "2023-05-01"
NSP_VERSION = "2026-01-01"
CONTAINER = "research-evidence"
BLOB_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"


def provision(*, apply: bool, enforced_perimeter: bool) -> dict:
    require_configuration({"FOUNDRY_SUBSCRIPTION_ID": SUBSCRIPTION_ID,
                           "PDD_RESOURCE_GROUP": RESOURCE_GROUP, "PDD_AZURE_LOCATION": ACA_LOCATION})
    rg = f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    name = "pddresearch" + hashlib.sha256(rg.lower().encode()).hexdigest()[:10]
    account = f"{rg}/providers/Microsoft.Storage/storageAccounts/{name}"
    container = f"{account}/blobServices/default/containers/{CONTAINER}"
    result = {
        "account": name, "account_url": f"https://{name}.blob.core.windows.net",
        "container": CONTAINER, "retention": "until_explicit_deletion",
        "redundancy": "Standard_ZRS", "anonymous_access": False, "shared_keys": False,
        "soft_delete": False, "versioning": False, "applied": apply,
        "public_network_access": "SecuredByPerimeter" if enforced_perimeter else "Disabled",
    }
    if not apply:
        return result
    arm = ArmClient()
    existing = arm.request("GET", account + f"?api-version={API_VERSION}", expected_statuses=(200, 404))
    if existing.status_code == 200 and existing.json().get("tags", {}).get("project") != "pdd-research-evidence":
        raise RuntimeError("Refusing to modify a storage account not owned by this evidence configuration.")
    perimeter = f"{rg}/providers/Microsoft.Network/networkSecurityPerimeters/nsp-pdd-evidence"
    if enforced_perimeter:
        existing_perimeter = arm.request("GET", perimeter + f"?api-version={NSP_VERSION}", expected_statuses=(200, 404))
        if existing_perimeter.status_code == 200 and existing_perimeter.json().get("tags", {}).get("project") != "pdd-research-evidence":
            raise RuntimeError("Refusing to modify a network perimeter not owned by this evidence configuration.")
    arm.request("PUT", account + f"?api-version={API_VERSION}", json_body={
        "location": ACA_LOCATION, "kind": "StorageV2", "sku": {"name": "Standard_ZRS"},
        "tags": {"project": "pdd-research-evidence", "retention": "until-explicit-deletion"},
        "properties": {
            "supportsHttpsTrafficOnly": True, "minimumTlsVersion": "TLS1_2",
            "publicNetworkAccess": result["public_network_access"],
            "allowBlobPublicAccess": False, "allowSharedKeyAccess": False,
            "allowCrossTenantReplication": False, "defaultToOAuthAuthentication": True,
            "encryption": {"keySource": "Microsoft.Storage", "requireInfrastructureEncryption": True,
                           "services": {"blob": {"enabled": True}, "file": {"enabled": True}}},
        },
    })
    arm.wait_for_provisioning(account, API_VERSION, timeout_sec=600)
    arm.request("PATCH", account + f"?api-version={API_VERSION}",
                json_body={"properties": {"publicNetworkAccess": result["public_network_access"]}})
    observed = arm.request("GET", account + f"?api-version={API_VERSION}").json()["properties"]
    if observed.get("publicNetworkAccess") != result["public_network_access"]:
        raise RuntimeError("Azure did not accept the requested network mode. No policy bypass or public-access fallback will be attempted.")
    if enforced_perimeter:
        profile = perimeter + "/profiles/portal-storage"
        arm.request("PUT", perimeter + f"?api-version={NSP_VERSION}", json_body={
            "location": ACA_LOCATION, "tags": {"project": "pdd-research-evidence"}, "properties": {},
        })
        arm.wait_for_provisioning(perimeter, NSP_VERSION)
        arm.request("PUT", profile + f"?api-version={NSP_VERSION}", json_body={"properties": {}})
        arm.request("PUT", profile + f"/accessRules/portal-subscription?api-version={NSP_VERSION}", json_body={
            "properties": {"direction": "Inbound", "subscriptions": [{"id": f"/subscriptions/{SUBSCRIPTION_ID}"}]},
        })
        association = perimeter + "/resourceAssociations/evidence-storage"
        arm.request("PUT", association + f"?api-version={NSP_VERSION}", json_body={
            "properties": {"accessMode": "Enforced", "privateLinkResource": {"id": account}, "profile": {"id": profile}},
        })
        associated = arm.wait_for_provisioning(association, NSP_VERSION)
        if associated["properties"].get("accessMode") != "Enforced":
            raise RuntimeError("The evidence perimeter is not in Enforced mode.")
        result["perimeter"] = "nsp-pdd-evidence"
        result["perimeter_mode"] = "Enforced"
        result["inbound_subscription"] = SUBSCRIPTION_ID
    arm.request("PUT", f"{account}/blobServices/default?api-version={API_VERSION}", json_body={
        "properties": {"isVersioningEnabled": False,
                       "deleteRetentionPolicy": {"enabled": False},
                       "containerDeleteRetentionPolicy": {"enabled": False}},
    })
    arm.request("PUT", container + f"?api-version={API_VERSION}", json_body={"properties": {"publicAccess": "None"}})
    identity = arm.request("GET", f"{rg}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{UAMI_NAME}?api-version=2023-01-31").json()
    for principal, principal_type in ((identity["properties"]["principalId"], "ServicePrincipal"),):
        assignment = uuid.uuid5(uuid.NAMESPACE_URL, container.lower() + principal.lower() + BLOB_CONTRIBUTOR)
        arm.request("PUT", f"{container}/providers/Microsoft.Authorization/roleAssignments/{assignment}?api-version=2022-04-01", json_body={
            "properties": {
                "principalId": principal, "principalType": principal_type,
                "roleDefinitionId": f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/{BLOB_CONTRIBUTOR}",
            },
        })
    result["managed_identity_client_id"] = identity["properties"]["clientId"]
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Create the approved account/container and container-scoped roles.")
    parser.add_argument("--enforced-perimeter", action="store_true",
                        help="Explicitly approved Enforced network perimeter with subscription-based managed-identity inbound access.")
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()
    result = provision(apply=args.apply, enforced_perimeter=args.enforced_perimeter)
    if args.output_json:
        args.output_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
