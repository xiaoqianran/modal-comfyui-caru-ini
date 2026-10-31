# ChatGPT ↔ Cloudflare Worker MCP ↔ private R2 ↔ PC2

Private transfer bridge for the existing ComfyUI/Qwen/H3 runner.

## Files

- `src/index.ts`: MCP tools, R2 storage, 10-minute HMAC-signed downloads.
- `src/upload.ts`: native file object schema, bounded download and upload diagnostics.
- `src/auth.ts`: Cloudflare OAuth 2.1 authorization + per-client consent and owner-passphrase login.
- `pc2_receive.py`: downloads, verifies SHA-256, safely extracts ZIPs.

## Three MCP tools

1. `upload_file(file)` declares `_meta["openai/fileParams"]`. ChatGPT supplies a file **object** with required `download_url`, `file_id` and optional `file_name`, `mime_type`. Worker fetches bytes and saves privately to R2, returning `key`, `size`, `sha256`, `request_id`.
2. `get_download_url(key)` returns an expiring (10-minute) *Worker-signed GET URL* plus stored SHA-256. It is **not** an S3 presigned URL; it needs no R2 keys on PC2.
3. `delete_file(key)` deletes an R2 object.

No PC2 inbound ports, no second MCP server, no new runner required. The only unauthenticated endpoint is a time-limited, HMAC-authenticated GET download route. All MCP tools use OAuth.

## Cloudflare bindings

The existing private R2 bucket `chatgpt-transfer` is bound as `FILES`. The OAuth KV namespace `chatgpt-transfer-oauth` is bound as `OAUTH_KV` in `wrangler.jsonc`. `MCP_RESOURCE` must exactly equal the deployed public `https://.../mcp` URL.

This setup uses the Cloudflare `OAuthProvider` one-Worker option. Clients authorize via a browser page that displays the requesting client name, redirect host and requested scopes, and requires the owner's 24+ character passphrase. No passphrase is committed in Git.

## Deployment

Configured endpoint: `https://chatgpt-file-transfer-mcp.wangran.workers.dev/mcp`. Local validation does not establish the currently deployed version or secret configuration.

Deployment verified on 2026-10-31 (Asia/Shanghai): v0.5.0, version `58e1b423-88e0-4469-918c-06cd753fcce2`, serves 100% of traffic. Anonymous MCP returns 401, OAuth resource metadata returns 200, and malformed download paths return 400. `OWNER_PASSWORD` and `DOWNLOAD_SECRET` remain encrypted Secret bindings.

Real plugin smoke tests passed for a 51-byte text file and a 184-byte ZIP. The ZIP was uploaded through `Cloudflare R2 Transfer`, downloaded with a signed Worker URL using `pc2_receive.py` on this development machine's D drive, verified against its SHA-256, and extracted with byte-for-byte matching contents. Worker logs confirm `upload_downloaded` and `upload_stored` for request `b59c8f30-9001-4cae-b98f-643c920420a5` on this version. This does not establish that the original 16-image ZIP has transferred, or that PC2 has run the updated receiver. Refresh the ChatGPT connection and start a new conversation before testing that original file.

The endpoint requires two Cloudflare secret bindings. Once configured, anonymous MCP requests return HTTP 401 and OAuth authorization is available.

1. Open **Cloudflare Dashboard → Workers & Pages → chatgpt-file-transfer-mcp → Settings → Variables and Secrets**.
2. Add **Secret** `OWNER_PASSWORD`: a random 32+ character passphrase. Save it in your password manager; you will enter it when ChatGPT prompts for OAuth consent.
3. Add **Secret** `DOWNLOAD_SECRET`: a **different** 32+ character random secret. This is used only for signed temporary GET URLs.
4. Deploy the resulting Worker version if the dashboard asks; then verify the MCP URL no longer returns 503. Anonymous access should be challenged by OAuth, not enabled.
5. In ChatGPT, add a **private custom MCP plugin** pointing to `https://chatgpt-file-transfer-mcp.wangran.workers.dev/mcp`, complete browser OAuth authorization, and verify the three tools.

Generate each random secret in a terminal, one at a time:

```powershell
uv run --no-project python -c "import secrets; print(secrets.token_urlsafe(36))"
```

**Do not commit secrets to Git or paste them into a chat.** Both must be stored only in Cloudflare's Secret bindings. If registering a private MCP plugin is not enabled for the current ChatGPT account/workspace, the user must enable it; do not weaken access controls.

## Local validation

Use Node.js 24.13+ and Python 3.13+. From this directory:

```powershell
npm ci
npm run check
python -m unittest discover -s tests -v
```

Commit `package-lock.json` alongside dependency changes. The Agents release and its MCP server peer dependency must match; CI uses `npm ci` to install the locked versions. `npm run check` typechecks, tests the upload pipeline, and bundles without publishing.

## End-to-end test

1. After deployment, open the ChatGPT plugin connection, select **Refresh**, confirm updated metadata, and **start a new conversation**. Select a small non-sensitive attachment first. The MCP wire input is an **object**, for example `{"file":{"download_url":"https://…","file_id":"file_…","file_name":"transfer-smoke.txt"}}`. If the client exposes a local-path string adapter, pass the actual file path as its tool description requires; do not serialize this object into the path field or invent a download URL. A client-visible string alone does not establish stale server metadata.
2. Confirm `key` and `sha256` are returned and the private R2 object exists.
3. Call `get_download_url` and pass its URL + SHA-256 to the connected PC2 AgentDock.
4. AgentDock executes:

```powershell
uv run python integrations/file-transfer-mcp/pc2_receive.py --url "<temporary-url>" --sha256 "<64-hex>" --output inputs/storyboard_16_frames.zip --extract-to inputs/storyboard_16
```

5. Verify `01.png`–`16.png` and start the existing Task/Runner only after matching the complete archive hash.
6. Call `delete_file` to remove the transient R2 object.

## Upload diagnostics

An authenticated MCP request emits `mcp_authenticated` and returns an `X-Request-ID` header. Upload success and tool errors also return `request_id`. In Cloudflare Dashboard → this Worker → Observability → Logs, filter by the request ID. Application logs record:

- `upload_started`: the upload handler was entered after input validation.
- `upload_downloaded`: source bytes were received, with their size.
- `upload_stored`: R2 confirmed the write.
- `upload_failed`: the failing stage, safe error code, and source HTTP status when available.

Error codes include `invalid_source`, `source_http_error`, `source_redirect_blocked`, `source_timeout`, `source_fetch_failed`, `source_read_failed`, `size_limit`, and `storage_write_failed`. Redirect responses are inspected in manual mode and rejected without following their target. URLs, file IDs, file names, credentials and raw exceptions are excluded from these logs. Invocation logs are disabled and query strings are redacted in the observability configuration.

If the client reports a safety block, preserve the exact error and time. An `upload_started` event proves the handler was reached. An authenticated request without that event can also mean an input-validation failure or a different MCP operation. Missing logs alone do not prove a platform block: first confirm logging, time range, retention, and that the request was expected to reach this Worker. Refreshing metadata does not override platform safety decisions.

For PC2, configure the receiver output under `D:\Users\yuminghui\Downloads\` as specified in the conversation. A Windows path belongs on PC2; it is not an attachment `download_url` for the Worker. The receiver identifies itself with `User-Agent: chatgpt-file-transfer-receiver/0.5`.

## Safety and limitations

- Source URLs must be HTTPS on the allowlist (ChatGPT/OpenAI domains). Redirects are blocked to reduce SSRF risks.
- 25 MiB file input cap, size-enforced even when Content-Length is missing.
- Object keys are randomized and private. No R2 secrets reach PC2.
- The shared owner login is suitable for **one trusted user only**. Apply Cloudflare WAF/rate-limiting rules to `/authorize` before production; add separate identities/ACLs before multi-user support.
- Receiver caps downloads at 25 MiB, ZIP members at 4096, and total declared uncompressed size at 250 MiB. It validates all member paths and rejects archive symlinks, Windows device names, and targets resolving outside the destination before extracting. Use a destination that other processes cannot modify concurrently; extraction is not transactional if an I/O or CRC error occurs.
- OAuth, build, plugin installation, file selection and full network transfer must pass real tests. Successful syntax checks are not proof of a deployed service.

References:
- https://developers.cloudflare.com/agents/model-context-protocol/guides/remote-mcp-server/
- https://developers.cloudflare.com/agents/model-context-protocol/protocol/authorization/
- https://developers.openai.com/plugins/reference
- https://github.com/cloudflare/workers-oauth-provider/blob/main/docs/consent-page.md
- https://developers.cloudflare.com/agents/model-context-protocol/apis/handler-api/
- https://docs.python.org/3/library/zipfile.html
- https://developers.openai.com/plugins/deploy/connect-chatgpt#refresh-metadata
- https://developers.cloudflare.com/workers/observability/logs/workers-logs/
