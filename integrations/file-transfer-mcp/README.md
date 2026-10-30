# ChatGPT ↔ Cloudflare Worker MCP ↔ private R2 ↔ PC2

Private transfer bridge for the existing ComfyUI/Qwen/H3 runner.

## Files

- `src/index.ts`: MCP tools, R2 storage, 10-minute HMAC-signed downloads.
- `src/auth.ts`: Cloudflare OAuth 2.1 authorization + per-client consent and owner-passphrase login.
- `pc2_receive.py`: downloads, verifies SHA-256, safely extracts ZIPs.

## Three MCP tools

1. `upload_file(file)` declares `_meta["openai/fileParams"]`. ChatGPT supplies a file object with `download_url`, `file_id`, `file_name`, `mime_type`. Worker fetches **real bytes** and saves privately to R2, returns `key`, `size`, `sha256`.
2. `get_download_url(key)` returns an expiring (10-minute) *Worker-signed GET URL* plus stored SHA-256. It is **not** an S3 presigned URL; it needs no R2 keys on PC2.
3. `delete_file(key)` deletes an R2 object.

No PC2 inbound ports, no second MCP server, no new runner required. The only unauthenticated endpoint is a time-limited, HMAC-authenticated GET download route. All MCP tools use OAuth.

## Cloudflare bindings

The existing private R2 bucket `chatgpt-transfer` is bound as `FILES`. The OAuth KV namespace `chatgpt-transfer-oauth` is bound as `OAUTH_KV` in `wrangler.jsonc`. `MCP_RESOURCE` must exactly equal the deployed public `https://.../mcp` URL.

This setup uses the Cloudflare `OAuthProvider` one-Worker option. Clients authorize via a browser page that displays the requesting client name, redirect host and requested scopes, and requires the owner's 24+ character passphrase. No passphrase is committed in Git.

## Deployment (2026-10-30)

**Deployed** to `https://chatgpt-file-transfer-mcp.wangran.workers.dev/mcp` using Cloudflare's Worker API. The private R2 bucket and OAuth KV are bound. GitHub Actions typecheck + Worker bundle have passed.

**Current endpoint returns HTTP 503 `Not configured` deliberately**. Two secret bindings must still be created by the account owner in the Cloudflare dashboard before the OAuth endpoint can be used.

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

After deployment, open ChatGPT Plugins settings, add a private custom MCP endpoint at the configured `MCP_RESOURCE`, complete the authorization prompt, and check that all three tools appear. If the account/workspace cannot install private MCP plugins, do not bypass that by exposing unsecured endpoints.

## End-to-end test

1. In ChatGPT, select the generated `storyboard_16_frames.zip` as the `upload_file` file parameter.
2. Confirm `key` and `sha256` are returned and the private R2 object exists.
3. Call `get_download_url` and pass its URL + SHA-256 to the connected PC2 AgentDock.
4. AgentDock executes:

```powershell
uv run python integrations/file-transfer-mcp/pc2_receive.py --url "<temporary-url>" --sha256 "<64-hex>" --output inputs/storyboard_16_frames.zip --extract-to inputs/storyboard_16
```

5. Verify `01.png`–`16.png` and start the existing Task/Runner only after matching the complete archive hash.
6. Call `delete_file` to remove the transient R2 object.

## Safety and limitations

- Source URLs must be HTTPS on the allowlist (ChatGPT/OpenAI domains). Redirects are blocked to reduce SSRF risks.
- 25 MiB file input cap, size-enforced even when Content-Length is missing.
- Object keys are randomized and private. No R2 secrets reach PC2.
- The shared owner login is suitable for **one trusted user only**. Apply Cloudflare WAF/rate-limiting rules to `/authorize` before production; add separate identities/ACLs before multi-user support.
- ZIPs must be checked for path traversal and symlinks; do not extract unknown/untrusted archives without resource limits.
- OAuth, build, plugin installation, file selection and full network transfer must pass real tests. Successful syntax checks are not proof of a deployed service.

References:
- https://developers.cloudflare.com/agents/model-context-protocol/guides/remote-mcp-server/
- https://developers.cloudflare.com/agents/model-context-protocol/protocol/authorization/
- https://developers.openai.com/plugins/reference
