# Setup and prerequisites

This guide describes the runnable portal, not the separate hosted-agent reference. No specific product or review platform is required.

## 1. Choose what to enable

| Capability | Local prerequisites | Cloud/provider prerequisites |
|---|---|---|
| Offline sample | Python 3.12, installed requirements, modern browser | None; keep the WebIQ key empty |
| All live planning and synthesis | Reachable portal; configured environment | Foundry project, compatible deployed model, authorized identity, available quota |
| WebIQ discovery | Key entered in portal over HTTPS or verified loopback | WebIQ account/access to Search |
| Toolbox discovery without a WebIQ key | Cached local sign-in or hosted managed identity | A project toolbox exposing `web_search` |
| WebIQ Browse inspector | Per-request key in portal | WebIQ Browse access; an accessible listing |
| Azure BAT inspector | Python Playwright client package from requirements | Foundry toolbox exposing `browser_automation___create_session`, connected Playwright workspace and permissions |
| Local browser inspector | Desktop Chrome/Edge, extension, connected portal tab | Foundry model; either discovery provider. No BAT workspace needed for WebIQ discovery |
| VM inspector | Updated Windows worker, Chrome and adjacent `community_snapshot.js` | VM or managed Windows host with outbound HTTPS; Foundry model and a discovery provider |
| Durable hosted history | None in the visitor's browser | Private Blob container, runtime identity data role and an allowed network path |

An online VM is not necessarily an online worker. A connected extension is not proof of cloud model access. Successful model access is not proof of access to every website.

## 2. Local installation

Use the [README quick start](../README.md#quick-start-no-cloud-credentials). Python 3.12 is the tested development baseline. Windows is required for the supplied Windows worker/bootstrap; container hosting uses the Linux Docker image. Git is useful for releases; Node.js and a frontend package manager are not required.

The dependency file includes Uvicorn's standard extras, including the dotenv loader. Do not activate hosted mode for an unauthenticated loopback walkthrough.

The configuration file is private:

```powershell
Copy-Item .env.example .env
```

Edit `.env`, not `.env.example`, with your own values. Do not run this copy command over an existing configuration.

Restart the API after changing settings; many provider settings are read when modules load. Existing process variables override `uvicorn --env-file` values.

## 3. Configure Foundry for live research

Create or select a Foundry resource/project and deploy a supported chat model using the [official project guidance](https://learn.microsoft.com/en-us/azure/foundry/how-to/create-projects) and [model deployment guidance](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/deploy-foundry-models). Select a model/region with sufficient quota and structured-output support. Screenshot-assisted extraction benefits from image support; verify compatibility rather than assuming every deployment supports every operation.

Set these values in `.env`:

| Setting | Meaning |
|---|---|
| `FOUNDRY_TENANT_ID` | Your Entra tenant ID |
| `FOUNDRY_SUBSCRIPTION_ID` | Your Azure subscription ID |
| `FOUNDRY_RESOURCE_GROUP` | Resource group containing Foundry |
| `FOUNDRY_ACCOUNT_NAME`, `FOUNDRY_PROJECT_NAME` | Existing resource/project names |
| `FOUNDRY_PROJECT_ENDPOINT` | Actual project HTTPS endpoint copied from Foundry |
| `FOUNDRY_MODEL_DEPLOYMENT` | Your deployment name, not a catalog model name |
| `FOUNDRY_TOOLBOX_NAME` | Toolbox used by BAT and/or keyless discovery |

For local live access, sign in **manually once** to the intended tenant with Azure CLI, then set `FOUNDRY_AUTHORIZED_OBJECT_ID` and/or `FOUNDRY_ACCOUNT_UPN` to pin the cached account. For example, after replacing the quoted placeholder:

```powershell
az login --tenant "YOUR_TENANT_ID"
```

The application's local credential reads the Azure CLI MSAL cache silently; it does not invoke Azure CLI login, open authentication windows, guess between cached users or support arbitrary pasted bearer tokens. Environments where that cache cannot be read need an adapted credential integration; an Azure CLI login alone is not a guarantee.

Hosted access uses managed identity instead; see [deployment](DEPLOYMENT.md). The `FOUNDRY_MANAGED_IDENTITY_CLIENT_ID` setting is a **client ID**, not the identity's principal/object ID.

## 4. Configure discovery

**WebIQ:** obtain appropriate provider access and enter the key inside the portal's research settings for each browser session/run as needed. Never place it in `.env`, a Git file, query string, worker script or command-line argument. Search and Browse availability/quotas are separate prerequisites. The app requests query-relevant search passages; a short displayed excerpt does not mean only that excerpt reached synthesis.

**Toolbox web search:** if no key is supplied, live discovery calls `web_search` in your configured project toolbox. Follow [Create and manage a toolbox](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox), select/publish a usable default version, and verify the consumer identity can list and call the expected tool. This repo does not create that service merely by starting the portal.

## 5. Enable each inspector

### WebIQ Browse

Choose WebIQ Browse and supply an authorized WebIQ key. It retrieves extracted page content, not native browser viewport images. Indexed/fallback content may not be fresh. HTTP 202, restrictions, missing pages and throttling remain explicit outcomes.

### Azure BAT

Follow [Foundry browser automation setup](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/browser-automation). Create a Playwright workspace, connect it to the project/toolbox, and verify that the toolbox exposes `browser_automation___create_session`.

Set `PLAYWRIGHT_WORKSPACE_NAME`, `PLAYWRIGHT_CONNECTION_NAME` and, when applicable, `PLAYWRIGHT_WORKSPACE_RESOURCE_ID`. Use the actual names from your configuration, not assumed sample connection names.

Grant the identities required by the current connection design access to the project/workspace. The application authenticates the project tool call and uses its returned remote browser session/CDP endpoint. A local browser binary installation is not needed for the remote BAT session.

### Local browser / Playwright

Follow the [extension instructions](../browser_extension/README.md). Use an actual desktop browser, preferably a dedicated profile, not an embedded app preview. Install the whole folder, keep the portal tab open and confirm its connection badge before selecting this path.

For a hosted or custom-domain portal, add **that exact portal host** to the extension's content-script matches before loading/reloading it. The repository template trusts loopback only. Research-target permissions and trusted portal matches are different settings.

This route still uses the configured Foundry model. It does not run the isolated Playwright benchmark driver on the API server; that driver is separate tooling.

### VM + Playwright

Use the [VM instructions](DEPLOYMENT.md#5-enable-the-vm-path). Install Chrome, Python, `httpx`, `playwright`, the worker and the shared extractor together. The worker polls the portal over outbound HTTPS. No extension, public RDP port or browser on the API container is required.

### Search-only reviews

Select this strategy in the portal. It still inspects the initial listing. By default that is WebIQ Browse and requires a key. To use Toolbox discovery without a WebIQ key, explicitly select a working browser for the listing.

API shape:

```json
{
  "product_url": "https://shop.example.com/products/item-id",
  "user_intent": "Long-session comfort, reliable connectivity and a clear return policy",
  "execution_mode": "webiq_browse",
  "inspect_review_pages": false
}
```

The URL is a placeholder, not a live demo source. Replace it with an eligible public product URL. Authenticated POST requests also require the portal's session/CSRF protections; WebIQ keys belong only in the request header.

## 6. Permissions and network checks

| Identity | Minimum purpose/scope |
|---|---|
| Local developer/runtime user | Foundry User on the appropriate project/resource; access to the configured browser/search connections |
| Hosted portal managed identity | Foundry User for the configured project/resource; `AcrPull` for a standard RBAC registry; Storage Blob Data Contributor on the evidence container |
| Connected BAT service identity | Workspace/connection permissions required by the official BAT setup |
| Provisioning operator | Relevant resource-management permissions plus permission to create the required role assignments |
| VM worker | Portal worker-route credential; no Azure user token or WebIQ key needed |

Resource Contributor alone does not grant role-assignment rights or Blob data access. A management-plane Owner role does not automatically solve a data-plane firewall/perimeter denial. ABAC-enabled registries can require repository-specific roles rather than the helper's `AcrPull` assumption.

Check provider registration, regional service availability, model quota, preview access where applicable, outbound HTTPS/DNS and organizational policy before provisioning. Private endpoints need working DNS/routing from the actual caller. Do not bypass isolation policies or weaken storage access to make a failed probe pass.

## 7. Acceptance checklist

First run an offline sample. Then verify a single eligible live URL independently on each configured path. Confirm the chosen inspector/discovery route, real-versus-illustrative capture labels, access-denial reporting, generated-report attribution and saved-run link. Use a browser route to verify actual PNG export.

For hosted history, reopen/export a saved record after an actual app revision replacement and compare capture hashes. Confirm signed-out access is denied and deletion requires a signed-in same-origin request. Do not run the paid five-path benchmark merely to check that a server starts.

Automatic verification can wait/recheck twice in the same browser session. It does not solve interactive challenges or evade denials. Visible comments may remain unloaded, and community assertions are not authenticated feedback.
