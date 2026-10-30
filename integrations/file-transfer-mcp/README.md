# ChatGPT → Cloudflare Worker MCP → R2 → PC2

A small, private file bridge independent of the existing Qwen / H3 runner.

## Included

- `upload_file`: accepts a ChatGPT-authorized file input (`openai/fileParams`), downloads its bytes and stores privately in R2; returns `key`, `size`, `sha256`.
- `get_download_url`: creates a 10-minute HMAC-signed Worker download URL for that key.
- `delete_file`: deletes the temporary R2 object (destructive action).
- `pc2_receive.py`: downloads and checks SHA-256, optionally safely extracts ZIP.

Only `/download/*` with an unexpired HMAC signature is available without bearer authorization. All MCP tools require `TRANSFER_TOKEN`. The R2 bucket is **not public**. Upload input is limited to 25 MiB and approved HTTPS source hosts, and never follows HTTP redirects.

## Cloudflare setup

Existing private R2 bucket: `chatgpt-transfer`, bound to `FILES` by `wrangler.jsonc`.

```powershell
cd integrations/file-transfer-mcp
npm install --registry=https://registry.npmjs.org
npm run check
npx wrangler login
npx wrangler secret put TRANSFER_TOKEN
npm run deploy
```

Set a randomly generated, high-entropy secret through Wrangler. Never save a token in Git.

The service deliberately returns 503 if the secret is not set; a mismatched bearer token returns 401. For **ChatGPT private plugin installation**, replace this development bearer gate with the Cloudflare **OAuth authenticated remote MCP** template before connecting. ChatGPT's OAuth handshake, identity handling and consent flow are **not** implemented in this repository yet. It is therefore not production-ready or verified with ChatGPT.

## Use after ChatGPT plugin authorization

Call `upload_file`, followed by `get_download_url`. Then tell the connected PC2 AgentDock to run (with the actual values returned, not placeholders):

```powershell
python integrations/file-transfer-mcp/pc2_receive.py --url "<temporary URL>" --sha256 "<sha256>" --output "inputs/storyboard_16_frames.zip" --extract-to "inputs/storyboard_16"
```

The Worker URL is **a custom signed download proxy**, not an S3 presigned GET URL. PC2 needs no S3 secrets and no inbound ports. After validating the 16 files, invoke the project's existing `batch_run.py` task. Then call `delete_file` for cleanup.

## Verification

```powershell
python -m py_compile integrations/file-transfer-mcp/pc2_receive.py
node --experimental-strip-types --check integrations/file-transfer-mcp/src/index.ts
```

Passing syntax checks alone does not verify MCP routing, OAuth, deployment or network transfer. Do not declare the workflow complete without a full ChatGPT → Worker → R2 → PC2 SHA-256 test.

References:
- https://developers.cloudflare.com/agents/model-context-protocol/apis/handler-api/
- https://developers.openai.com/plugins/reference#file-apis
