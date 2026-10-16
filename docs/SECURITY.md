# Security

## Threat model

| Threat | Control |
| --- | --- |
| A malicious web page calls the local API (`127.0.0.1:8765`) from the user's browser | Every `/api/*` request must carry the custom `X-LFA-Client` header, which forces a CORS preflight; the CORS allow-list contains only `http://127.0.0.1:*`, `http://localhost:*` and `chrome-extension://<32 chars>`. The `Origin` header is additionally validated server-side, and the `Host` header must be a loopback name (DNS-rebinding guard). |
| Another local process impersonates the extension | Extension requests and the WebSocket need a bearer token issued through a short-lived (5 min), single-use, 8-character pairing code shown in the dashboard. Claims are rate-limited to 5 failures/minute. Tokens are stored as SHA-256 hashes. The paired `chrome-extension://` id is pinned; a token used from a different extension origin is rejected. |
| Extension acts on sites the user did not intend | The manifest requests **no** blanket host permissions. Sites are granted one at a time from the side panel ("Allow this site") via `optional_host_permissions`; the service worker refuses snapshots/actions/navigation for origins without a grant. The agent independently enforces its own domain allow-list. |
| Model proposes dangerous actions | Model output must validate against the typed `BrowserAction` schema (no `script`/`eval`/`sql`/`shell` arguments, no `javascript:` URLs, uploads reference a resume id only). The `PolicyEngine` runs before every execution: domain allow-list, risk classification (model-declared risk can only raise the level), credential fields are never typed into, high-risk clicks (submit/pay/delete/send) need an approval record. |
| Prompt injection from page content | Page text is passed inside `<page>` delimiters with an explicit untrusted-data instruction; page content never reaches the policy engine; authentication/challenge detection is deterministic and cannot be overridden by text on the page. |
| Credential or secret leakage | No passwords, cookies, OTPs, tokens or session data are ever read from the browser or stored. Password field values are stripped from snapshots inside the page. Logs and audit events pass through a redactor (secret-like keys removed, bearer tokens masked, PII masked in diagnostics exports). |
| Exposure on the network | The service refuses to bind `0.0.0.0`/`::`; the default is `127.0.0.1`. The dashboard is only reachable on loopback. |
| Oversized or malformed input | 2 MB request/WebSocket message limit, pydantic validation on every endpoint, snapshot node budget, bounded model context and response size. |
| Runaway automation | Per-task action cap, per-action timeout, overall deadline, loop detection, bounded retries with jitter, daily application limit, per-domain concurrency and delay, pause-all that stops the running task at its next step. |
| Duplicate / unverified submissions | Composite application identity (unique index), authoritative post-submit verification, `SUBMISSION_UNCERTAIN` outcomes block retries until reconciled by the user. |
| Local data exposure | Data directory and database are created under the user's profile; on POSIX the file mode is 0600 and the directory 0700. Windows inherits the user-profile ACL. |

## Code-less pairing for the project's own build

Chrome derives an unpacked extension's id from the absolute path it was loaded from. The agent
computes the id of its own `extension/dist` folder and accepts a `AUTOPAIR` claim only when the
request's `Origin` is exactly `chrome-extension://<that id>` and no active pairing exists for it.
A web page cannot send a `chrome-extension://` origin, and another extension can only carry this
id if it was loaded from this very folder on this machine, which already implies local file
access. Revoking the pairing in the dashboard re-enables a single new auto-claim.

## Unattended mode

- **Tab takeover** (toolbar icon) still goes through Chrome's per-site permission prompt; the grant is per origin and revocable in the extension options. The agent's allow-list entry is persisted in the scheduler settings and editable in the dashboard.
- **Auto-submit** (`approval_policy = never`) removes the human confirmation before the submit click only. Every other control stays: domain allow-list, risk classification, credential fields never typed, legal questions only answered from your profile or approved answers, authoritative verification before anything is called "applied", duplicate prevention, daily limits.
- **Auto-resume** only re-checks a tab the agent itself handed over, only after a page load in that tab, and only resumes when the deterministic classifier no longer sees a login/challenge page. It never interacts with the login page.

## What the extension can and cannot do

- Can: read the DOM of tabs on sites you allowed, fill fields, click, upload a resume the agent registered, open/close tabs, navigate to allowed origins.
- Cannot: read cookies, local storage, passwords, history, or other tabs' content; act on sites you did not allow; run code supplied by the model.

## Revocation and shutdown

1. Dashboard → Settings → Extension pairing → **Revoke** (immediately invalidates the token; the WebSocket closes with code 4001).
2. Extension options → **Unpair**, and remove per-site grants (or `chrome://extensions` → Remove).
3. Stop the agent process (Ctrl+C or close the terminal). Nothing else runs in the background.
4. To erase everything: Dashboard → Activity & diagnostics → **Delete all data**, or delete the data directory (`%LOCALAPPDATA%\.lfa`).

## Data export

Activity & diagnostics → **Export all data** (full JSON) or **Export diagnostics** (redacted).

## Remote dashboard (future)

When the dashboard is hosted elsewhere, the local agent must still only listen on loopback. The intended model is: a user-authenticated hosted dashboard (OIDC), TLS everywhere, and an outbound agent pairing (agent initiates a WebSocket to the hosted service with a per-device key), so the agent is never reachable from the internet. The API client already isolates the base URL and auth header in one module.
