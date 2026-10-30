import { createMcpHandler } from "agents/mcp/server";
import { McpServer } from "@modelcontextprotocol/server";
import { z } from "zod";

interface Env { FILES: R2Bucket; TRANSFER_TOKEN: string; }
const MAX_BYTES = 100 * 1024 * 1024;
const FileInput = z.object({
  download_url: z.string().url(),
  file_id: z.string().min(1),
  mime_type: z.string().optional(),
  file_name: z.string().optional(),
});

function createServer(env: Env) {
  const server = new McpServer({ name: "chatgpt-file-transfer", version: "0.1.0" });
  server.registerTool("upload_file", {
    title: "Upload ChatGPT file to R2",
    description: "Fetch an explicitly authorized ChatGPT file and store it privately in R2.",
    inputSchema: { file: FileInput },
    annotations: { destructiveHint: false, openWorldHint: true, readOnlyHint: false },
    _meta: { "openai/fileParams": ["file"] }
  }, async ({ file }) => {
    // Never trust arbitrary URLs: protect against SSRF into private networks.
    const url = new URL(file.download_url);
    const trustedDomains = ["chatgpt.com", "openai.com", "oaiusercontent.com"];
    if (url.protocol !== "https:" || !trustedDomains.some(d => url.hostname === d || url.hostname.endsWith("." + d))) {
      throw new Error("Untrusted file download host");
    }
    const response = await fetch(url, { redirect: "error" });
    if (!response.ok || !response.body) throw new Error("Unable to fetch file");
    const len = Number(response.headers.get("content-length") || "0");
    if (len > MAX_BYTES) throw new Error("File too large");
    const body = await response.arrayBuffer();
    if (body.byteLength > MAX_BYTES) throw new Error("File too large");
    const fileName = (file.file_name || "upload.bin").replace(/[^a-zA-Z0-9._-]/g, "_").slice(0, 100);
    const key = "chatgpt/" + crypto.randomUUID() + "/" + fileName;
    const digest = await crypto.subtle.digest("SHA-256", body);
    const sha256 = [...new Uint8Array(digest)].map(x => x.toString(16).padStart(2, "0")).join("");
    await env.FILES.put(key, body, { httpMetadata: { contentType: file.mime_type || "application/octet-stream" },
      customMetadata: { sha256, fileId: file.file_id } });
    const result = { bucket: "chatgpt-transfer", key, bytes: body.byteLength, sha256 };
    return { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result };
  });
  return server;
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    // Fail closed until OAuth is configured; bearer gate is for private transport tests only.
    if (!env.TRANSFER_TOKEN || request.headers.get("Authorization") !== "Bearer " + env.TRANSFER_TOKEN) {
      return new Response("Unauthorized", { status: 401 });
    }
    const url = new URL(request.url);
    if (url.pathname !== "/mcp") return new Response("Not found", { status: 404 });
    return createMcpHandler(() => createServer(env))(request, env, ctx);
  }
};
