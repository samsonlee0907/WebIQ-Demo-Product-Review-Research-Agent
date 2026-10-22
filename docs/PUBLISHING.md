# Publishing checklist

This repository is prepared as a reusable research prototype with product-neutral onboarding. Publishing it does not publish a running cloud service, transfer Azure resources or establish permission to redistribute third-party content.

## Scope of the public source

Keep the reusable source, four governed skills, environment template, guides and MIT license. Existing product-specific demo scenarios, benchmark metadata/HTML and presentation files are deliberately preserved as historical artifacts. They are not product-neutral or current product advice.

Private saved history, original browser captures, credentials, token caches and environment files must remain outside Git and public release images. The source benchmark can show metadata without private screenshots or expired briefs. Do not manufacture replacements to make a public checkout look like a live run.

The canonical portal is served by `api.app.main:app`; do not publish `portal\index.html` as if it were the current standalone frontend.

## Before making a repository public

1. Review tracked files and the **full Git history** for credentials, private infrastructure details and material you cannot redistribute. Sanitizing the current template does not rewrite history.
2. Review presentations manually, including speaker notes, author metadata, hidden slides, embedded images and attachments. User-edited decks are not automatically rewritten or anonymized by this preparation.
3. Confirm rights to retained examples, citations and any third-party content. The repository's MIT license does not relicense publisher screenshots, provider data or dependency code.
4. Ensure `.env`, local access/credential JSON, token caches, logs and `api\private_history\` / `api\private_replay\` are not tracked. Ignore rules do not untrack a file already committed.
5. Do not stage everything blindly; stage intended paths and inspect the staged diff. Leave unrelated user edits alone.

Useful read-only checks:

```powershell
git status --short
git ls-files
git diff --check
git diff --cached --stat
git diff --cached
```

Use your organization's approved secret/history scanner before release. If a real credential was committed, revoke/rotate it and follow the provider's history-cleanup process; deleting only the latest copy is insufficient. Do not publish a secret-scanning report containing the secret itself.

No remote repository or public deployment URL is assumed. Create/link your chosen repository only when ready; this preparation does not push or change visibility.

## This public snapshot

The public source repository is [WebIQ-Demo-Product-Review-Research-Agent](https://github.com/samsonlee0907/WebIQ-Demo-Product-Review-Research-Agent). Its initial commit is an exported source snapshot, not a push of the development repository's history.

The release includes the editable solution-concept presentation, its five diagram previews and the three project-owned portal UI captures used by the README. The rest of the original walkthrough capture set, its deployment-specific presentation and its provenance/receipt are excluded; this avoids redistributing publisher captures, extracted page bodies and operational device labels. The private originals and unrelated user-edited presentations remain unchanged in the development workspace. Public copies of older discussion presentations replace operational portal links with an example origin.

Browse-access experiment reports, credentials, private history, screenshot archives and session artifacts are not release inputs. Run secret checks on the exact export, including presentation ZIP contents, before pushing. Do not substitute an unreviewed working-directory copy for the committed source export.

## Validate a clean checkout

Use Python 3.12 and the [quick start](../README.md#quick-start-no-cloud-credentials), without private cloud values. Confirm the offline portal starts and completes a sample without paid work.

```powershell
.\.venv\Scripts\python.exe -m pytest api\tests -q
.\.venv\Scripts\python.exe -m compileall -q api agents scripts
```

The unit/regression suite isolates saved evidence in temporary directories. Optional browser checks and their desktop Edge requirement are documented in the README. CI runs the regression suite with no Azure secrets.

For a cloud release, execute the [deployment acceptance checklist](SETUP.md#7-acceptance-checklist) in the target environment. Source-level tests do not prove quotas, roles, network policy, worker readiness, provider availability or live answer quality.

## Build and distribute safely

The Dockerfile and default filtered ACR build exclude private history/benchmark captures. A source checkout does not contain the private evidence from an operator's deployment. The `--include-private-replay` option is only for explicitly permitted **private** images; it changes the filtered build context and is not a public-release option.

Build with an explicit image tag and record the code revision. The requirements file specifies minimum compatible package bounds, not a complete dependency lock; pin/test resolved dependencies and image digests for a reproducible operational release.

Do not bundle a developer's environment file, token cache, generated access file, VM credentials, ZIP evidence export or live raw grounding. Never use a baked-in passcode for a release.

## Document the operational boundary

Release notes should identify:

- The tested Python/runtime and dependency resolution, source revision and image digest.
- Which paths were actually exercised, under which provider access and network conditions.
- The shared passcode limitation and the requirement for real identity/authorization before confidential multi-user use.
- The retention/deletion scope, publisher/provider restrictions and remaining production-hardening work.

Foundry/browser integrations can evolve independently. `scripts\provision_foundry.ps1` is a **read-only checklist**, not a supported one-command Foundry deployment. `agents\hosted\pdd_hosted_agent.py` is a standalone reference scaffold with optional packages not installed by the portal requirements; do not advertise it as deployed or required for the five portal paths.

The implementation draws architectural inspiration from the [reference portal/toolbox demo](https://github.com/samsonlee0907/anthropic-fsi-skill-demo-with-ghcp). That reference is not a runtime dependency or proof that its separate integrations are available here.
