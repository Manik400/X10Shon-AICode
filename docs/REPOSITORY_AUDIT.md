# Repository Audit

Date: 2026-10-16

## Summary

The working directory `D:\MANIK\Projects\Ext` was **empty** at the start of the build: no source
files, no configuration, no git repository, no existing Playwright tooling. There is nothing to
reuse, nothing to preserve, and no broken imports or duplicated components to resolve. All
architecture decisions below were therefore made from the development machine's capabilities, not
from existing code.

## Development machine

| Item | Finding |
| --- | --- |
| OS | Windows 11 Pro 10.0.26300 |
| CPU | Intel Core i7-1355U, 12 logical processors |
| RAM | 32 GB |
| GPU | NVIDIA GeForce MX550, 2 GB VRAM (too small for most 7B models; CPU inference is the realistic default) |
| Python | 3.12.10 (`python`); only `pytest 9.1.1` preinstalled globally |
| Node | 18.20.8 via nvm4w, npm 10.8.2 (no pnpm) |
| Git | 2.56.0 |
| Ollama | **Not installed**, nothing listening on 127.0.0.1:11434 |
| Playwright browsers | `chromium-1243`, `chromium_headless_shell-1243` already present in `%LOCALAPPDATA%\ms-playwright` |

## Consequences for the design

1. **No existing Playwright job tooling** exists, so the question "integrate or rewrite" is moot.
   A fresh browser layer is built behind interfaces (see `ARCHITECTURE.md`).
2. **No local model runtime is installed.** The model gateway is built provider-independent with
   an Ollama provider and an OpenAI-compatible provider (llama.cpp server, LM Studio). A
   deterministic `HeuristicProvider` is included so every workflow runs end to end, and is tested,
   without a model. Model-dependent features degrade to "needs review" rather than guessing.
   Actual model latency and memory figures cannot be measured until a runtime is installed; see
   `PERFORMANCE.md` for what was and was not measured.
3. **Node 18** pins the dashboard to Vite 5 and Vitest 2 (Vite 7 needs Node 20+).
4. **2 GB VRAM** means GPU offload is partial at best. Resource budgets default to CPU threads.

## Technical risks identified up front

| Risk | Mitigation |
| --- | --- |
| Extension content scripts cannot access cross-origin iframes or closed shadow roots | Documented as unsupported; detection reports `SITE_UNSUPPORTED` with reason |
| MV3 service worker suspension drops the WebSocket to the local agent | Alarm-driven reconnect, heartbeat every 20 s, all state in `chrome.storage.session`/`local` |
| Malicious web pages calling `127.0.0.1:8765` | Loopback bind, bearer token for the extension, `Origin` allow-list, mandatory custom header for the dashboard, strict CORS |
| Model proposes unsafe actions | Typed action schema, policy engine runs before every execution, risk levels, approval manager |
| False "applied" reports | Submission is only `APPLIED_CONFIRMED` after `PostActionVerifier` finds authoritative evidence |
| Duplicate submissions after restart | Composite application identity, uncertain outcomes block retry until reconciled |
| Standalone Playwright has no user sessions | Application workflows run through the extension in the user's own Chrome; Playwright is used for unauthenticated extraction and tests only |
