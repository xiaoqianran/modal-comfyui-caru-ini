# ChatGPT File Transfer MCP — prototype

ChatGPT file input -> Worker MCP -> private Cloudflare R2.

This is an **authenticated transport prototype**, not yet a ChatGPT-connectable OAuth app. Do not deploy without configuring OAuth (or equivalent supported plugin authentication). The current TRANSFER_TOKEN check deliberately rejects requests by default.

## Configure
1. Create a private R2 bucket named `chatgpt-transfer`.
2. Install dependencies: `npm install`
3. Set secret for transport tests: `npx wrangler secret put TRANSFER_TOKEN`
4. `npm run typecheck`, then `npm run deploy`.
5. Integrate OAuth resource-server before connecting from ChatGPT Plugins.
6. Verify the plugin's tool schema advertises `_meta["openai/fileParams"]`, select a file in ChatGPT, check resulting R2 object's SHA-256 against the original.
7. Mint a short-lived **GET** presigned URL (not exposed by this prototype); let PC2 download, verify SHA-256, and run existing batch_run.py.

## Security
- Never expose R2 object publicly.
- Do not expose R2 API keys or the Worker token in source control.
- Restrict source download URLs to trusted file hosts. Recheck against actual authorized ChatGPT file URLs.
- Max file size 100 MB (prototype buffers entire file).
- Add OAuth and server-side per-user ACLs before production.
- Add signed GET/delete tools only after upload validation.
