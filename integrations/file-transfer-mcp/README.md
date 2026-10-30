# ChatGPT File Transfer MCP

Minimal **private Cloudflare Worker** using the official stateless MCP SDK and a private R2 bucket.

## Current scope

- One `upload_file` MCP tool, with `_meta["openai/fileParams"]` for ChatGPT file inputs.
- Only HTTPS URLs on an explicitly allowlisted ChatGPT/OpenAI host (no redirect).
- Streaming read with a hard **25 MiB** cap, SHA-256, unique object keys and private R2 storage.
- No file bytes or signed source URLs are returned to ChatGPT.
- **Fail-closed** authentication: missing `TRANSFER_TOKEN` returns HTTP 503, invalid bearer token returns 401.

This is a **test-only bearer-authenticated prototype**. ChatGPT plugin installation normally requires a properly supported OAuth flow. Do **not** deploy or register as a ChatGPT app until OAuth is added and verified. The Worker is not a public upload API.

## Development

1. Cloudflare R2 bucket `chatgpt-transfer` must exist.
2. Run `npm install` and `npm run check`.
3. For a controlled private test, configure `wrangler secret put TRANSFER_TOKEN`. Do not write tokens into repo files.
4. Run `npm run dev` and exercise `/mcp` with an authenticated MCP Inspector/client.
5. Before ChatGPT integration, adopt the official Cloudflare authenticated MCP example, with OAuth + consent and per-user authorization.
6. Connect a private plugin, select an explicitly authorized file, verify the R2 object's SHA-256 matches local source.

## Production follow-ups

- OAuth authentication instead of the test bearer token.
- Signed short-lived GET URLs with per-user access control, and delete/TTL cleanup.
- PC2 download: verify SHA-256 before extracting, reject ZIP path traversal, then invoke existing `batch_run.py`.
- Large files: stream/multipart upload rather than buffering; keep a separate upload limit.
- Verify real ChatGPT file download URL domains before extending the allowlist.

References:
- https://developers.cloudflare.com/agents/model-context-protocol/apis/handler-api/
- https://developers.openai.com/plugins/reference#file-apis
