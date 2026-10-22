#Requires -Version 7.0
<#
.SYNOPSIS
Read-only Foundry preparation checklist. Does not provision or deploy resources.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^https://')]
  [string]$ProjectEndpoint,

  [Parameter(Mandatory = $true)]
  [string]$ModelDeployment,

  [string]$ToolboxName = "pdd-research"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Skills = @(
  "product-listing-digestion",
  "multi-platform-review-discovery",
  "claim-reality-matrix",
  "human-gated-cart-action"
)
foreach ($Name in $Skills) {
  $Path = Join-Path $RepoRoot "skills" $Name "SKILL.md"
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Missing governed skill: $Path"
  }
}

[ordered]@{
  status = "local_files_validated_cloud_setup_required"
  project_endpoint = $ProjectEndpoint.TrimEnd("/")
  model_deployment = $ModelDeployment
  toolbox_name = $ToolboxName
  skills = $Skills
  required_remote_tools = @("browser_automation___create_session", "web_search")
  remote_tools_validated = $false
  provisions_resources = $false
  portal_runtime = "FastAPI in Azure Container Apps; standalone hosted-agent entrypoint is reference-only"
  instructions = "docs/SETUP.md and docs/DEPLOYMENT.md"
} | ConvertTo-Json -Depth 4
