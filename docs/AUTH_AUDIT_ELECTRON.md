# Auth Audit - Electron Path Hardening & Logging

**Audience:** the session working on the Electron build / logging hardening.
**Scope:** the custom isolated `az login` profile (`AZURE_CONFIG_DIR`) and every path that touches it. Read-only audit - no code changed by the audit session.
**Date:** 2026-07-16

---

## TL;DR - what needs doing

1. **Electron never disables the WAM broker** in the isolated config dir. This silently voids the whole isolation guarantee. **Highest priority.**
2. **Electron never migrates existing `~/.azure` creds** into the isolated dir, contradicting the CHANGELOG "auto-migrated on first run" promise.
3. **No Python-side guardrail** if `AZURE_CONFIG_DIR` is unset - silent fallback to machine-wide az login.
4. **Auth logging has holes** - token acquisition failures, the credential fallback, wrong-tenant rejections, and the effective config dir are all invisible in logs.

---

## Background - how the custom auth profile works

Goal: decouple the app's `az login` state from the machine-wide `az login`, so signing out of `az` in a normal terminal does not log the app out (and vice versa).

Mechanism: point the Azure CLI at an isolated per-environment config dir via the `AZURE_CONFIG_DIR` env var:
- prod: `%USERPROFILE%\SalesBuddy\.azure`
- dev:  `%USERPROFILE%\SalesBuddyDev\.azure`

Two Python consumers read that env var (both inherit it from the process environment):
- `app/gateway_client.py` - AI gateway. Uses `AzureCliCredential`, falls back to `DefaultAzureCredential`. Requests a token for `https://management.azure.com`, then tenant-verifies the `tid` claim against the Microsoft corp tenant.
- `app/services/msx_auth.py` - MSX/Dynamics CRM. Shells out to `az account get-access-token`, `az login`, `az account show/clear/set`. When it launches `az login` it passes `env=os.environ.copy()`, so the child inherits the isolated `AZURE_CONFIG_DIR` (this part is correct).

## Who sets AZURE_CONFIG_DIR

| Entrypoint | Sets dir? | Migrates old creds? | Disables WAM broker? |
|---|---|---|---|
| `scripts/server.ps1` | yes | yes | **yes** |
| `scripts/supervisor.ps1` | yes (if unset) | no | **no** |
| `electron/main.js` `buildStackEnv()` | yes (if unset) | no | **no** |
| `start.bat` / `stop.bat` / `update.bat` | yes | no | no |
| plain `flask run` / `python -m app.supervisor` / tests / IDE debug | **no** | no | no |
| Python code itself | **never** | - | - |

The Electron shell spawns `python -m app.supervisor` **directly** - it does NOT go through `server.ps1` or `supervisor.ps1`. So it only gets whatever `buildStackEnv()` sets, which is the bare env var and nothing else.

---

## Finding 1 - WAM broker disable is missing on the Electron path (HIGHEST RISK)

`scripts/server.ps1` (around lines 79-120) is the only place that writes `enable_broker_on_windows = false` into the isolated `.azure/config` file. Its own comment explains why this matters:

> With the broker enabled (az CLI default since 2.61), refresh tokens live in WAM keyed by `{client_id, account}` and are **shared across every `AZURE_CONFIG_DIR` on the machine** - defeating the isolation. An `az logout` in any other shell flips the broker account to `Status_AccountUnusable` and Sales Buddy starts returning 401 on every MSX/gateway call. File-backed MSAL cache (DPAPI-encrypted, `msal_token_cache.bin` inside this dir) is per-`AZURE_CONFIG_DIR` and gives us real isolation.

Consequence: if a user's isolated `.azure` dir is first created by **Electron** (or by `supervisor.ps1`, or by the batch files), the broker is left **enabled**, so:
- The isolation is silently void - the app shares WAM refresh tokens with the machine-wide CLI.
- An `az logout` in any other terminal can flip the app to 401 on every MSX and gateway call.
- Only a launch via `server.ps1` ever repairs it.

### Fix

Port the broker-disable logic out of `server.ps1` so it runs on **every** startup path, including Electron. Two options:

- **Preferred: do it in Python**, once, early in `app/supervisor.py` (and/or the app factory) before any credential is used. That covers Electron, `supervisor.ps1`, batch files, plain `flask run`, and tests in one place. Pseudocode:
  1. Resolve `AZURE_CONFIG_DIR` (see Finding 3 for resolution).
  2. Ensure the dir exists.
  3. Read `<AZURE_CONFIG_DIR>/config`. If it lacks `enable_broker_on_windows = false` under `[core]`, insert/replace it (idempotent - mirror the exact logic in `server.ps1`).
- Alternative: replicate the PowerShell block in `supervisor.ps1` AND add a JS equivalent in `electron/main.js` `buildStackEnv()`. More surface area, easy to drift. Prefer the Python approach.

Idempotency requirements (match `server.ps1`):
- Strip any existing `enable_broker_on_windows = ...` line first.
- Ensure a `[core]` section exists, then place the setting under it.
- Safe to run every boot.

---

## Finding 2 - credential migration is missing on the Electron path

`scripts/server.ps1` (around lines 58-75) copies an existing default `~/.azure` into the isolated dir on first run (when the isolated dir is missing or empty), with retry-on-empty recovery. The CHANGELOG promises:

> Existing credentials are auto-migrated on first run - no re-authentication required.

Electron, `supervisor.ps1`, and the batch files do **not** migrate. So the first Electron launch on a machine that has never run `server.ps1` gives the user an empty isolated dir and forces a fresh sign-in - breaking that promise.

### Fix

Fold the migration into the same Python bootstrap as Finding 1 (do migration first, then broker-disable):
1. If isolated dir is missing or empty AND `%USERPROFILE%\.azure` exists, copy it in (recurse), with a warning (not a hard failure) if the copy fails.
2. Then apply the broker-disable step.

Keep it idempotent and non-destructive - never overwrite a non-empty isolated dir.

---

## Finding 3 - no Python-side guardrail if AZURE_CONFIG_DIR is unset

Python fully trusts the launcher to set `AZURE_CONFIG_DIR`. Any path that skips it (plain `flask run`, direct `python -m app.supervisor` outside Electron, pytest, VS Code debug) makes the az CLI silently fall back to the machine-wide `~/.azure`. The app then reads/writes the wrong token cache with zero warning.

### Fix

In the same boot bootstrap, if `AZURE_CONFIG_DIR` is unset:
- Resolve it the same way the launchers do (prod vs dev from `FLASK_ENV`, base `%USERPROFILE%`), set it in `os.environ`, and log that it was defaulted.
- If it cannot be resolved, emit `logger.warning("AZURE_CONFIG_DIR not set - using machine-wide az login, isolation disabled")`.

This makes the Python app self-sufficient regardless of which launcher (or no launcher) started it, and matches the "self-contained" intent of the Electron shell.

---

## Finding 4 - minor: Electron prod/dev resolution

`electron/main.js` `buildStackEnv()` derives prod vs dev from `.env` `FLASK_ENV`, matching `server.ps1`/`supervisor.ps1`. Correct. Only edge case: if `.env` is missing/unreadable it defaults to `production`, which could point a dev user at the prod dir. Low priority - just be aware when centralizing the resolution logic (Finding 3), keep the default consistent with the existing launchers.

---

## Logging assessment

### What exists
- `app/gateway_client.py` calls `diag_log('gateway', ...)` with endpoint / status / request+response body. Good for gateway HTTP errors.
- `app/services/msx_auth.py` has `logger.info/warning` on token refresh success/failure, VPN block, and login launch. Decent.

### Gaps (what to add)
- **Token acquisition failures in `gateway_client._get_token` are not logged.** It just raises `GatewayError`. Same for `_verify_tenant` rejecting a non-Microsoft account. Nothing tells you *why* auth failed.
- **No path logs the effective `AZURE_CONFIG_DIR`.** This is the single most useful line when debugging "the app randomly logged out." Neither Python nor Electron records which dir is in use.
- **The `AzureCliCredential` -> `DefaultAzureCredential` fallback** in `gateway_client._get_token` is silent, hiding the root cause.
- **`check_az_logged_in` / `get_az_cli_status` swallow subprocess errors** and return `False`, so "az crashed / timed out" looks identical to "not logged in" in the logs.
- **No log of the acquired token's tenant/user/expiry** on success, so you cannot confirm which account the app is actually using.

## Suggested logging additions

1. **Startup one-liner** (Python boot bootstrap + Electron `log()`): print resolved `AZURE_CONFIG_DIR`, `SALESBUDDY_HOME`, whether the dir exists, and whether `config` contains `enable_broker_on_windows = false`. Highest-value diagnostic.
2. **`gateway_client._get_token`**: `logger.warning` on `AzureCliCredential` failure and which fallback was chosen; `logger.error` (with exception) on final token failure; `logger.info` on success with the token's `tid` / `name` / expiry.
3. **`gateway_client._verify_tenant`**: `logger.warning` when a wrong-tenant token is rejected, including the offending `tid`.
4. **`msx_auth.check_az_logged_in` / `get_az_cli_status`**: distinguish subprocess error/timeout from logged-out - `logger.warning` on nonzero exit or exception, including `stderr`.
5. **New `auth` diag_log category**: emit on every token refresh (success + failure) for both gateway and MSX, so auth events land in the same `logs/diagnostic.jsonl` timeline already shipped. Follow the existing `diag_log(category, **fields)` shape in `app/services/diagnostic_log.py`.
6. **Guardrail warning** from Finding 3 when `AZURE_CONFIG_DIR` is unset.

---

## Suggested implementation order

1. Write one Python bootstrap function (e.g. `app/services/azure_profile.py` or a helper in `app/supervisor.py`) that: resolves `AZURE_CONFIG_DIR` (Finding 3) -> migrates old creds if needed (Finding 2) -> ensures broker-disable (Finding 1) -> logs the resolved state (Logging #1). Call it as the very first thing in `app/supervisor.main()` and defensively in the app factory before any credential use.
2. Once that exists, `server.ps1` / `supervisor.ps1` broker+migration blocks become redundant (leave them or slim them - they are harmless and idempotent).
3. Add the auth logging (Logging #2-6).

### Key files
- `app/gateway_client.py` - gateway credential + token + tenant verify
- `app/services/msx_auth.py` - MSX az CLI auth, login launcher, status checks
- `app/supervisor.py` - direct entrypoint Electron spawns (`python -m app.supervisor`)
- `electron/main.js` - `buildStackEnv()` sets the env var (Electron path)
- `scripts/server.ps1` - reference implementation of migration + broker-disable
- `scripts/supervisor.ps1` - sets env var only
- `app/services/diagnostic_log.py` - `diag_log()` sink (`logs/diagnostic.jsonl`)
