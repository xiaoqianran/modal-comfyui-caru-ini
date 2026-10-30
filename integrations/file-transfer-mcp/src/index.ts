import { McpServer, createMcpHandler } from "@modelcontextprotocol/server";
import { z } from "zod";

interface Env {
  FILES: R2Bucket;
  TRANSFER_TOKEN?: string;
}
const MAX_BYTES = 25 * 1024 * 1024; // 25 MiB: deliberately bounded prototype
const ALLOWED_HOSTS = ["chatgpt.com", "openai.com", "oaiusercontent.com"] as const;

const fileSchema = z.object({
  download_url: z.string().url(),
  file_id: z.string().min(1),
  mime_type: z.string().optional(),
  file_name: z.string().optional(),
}).strict();

export function validateFileUrl(raw: string): URL {
  const url = new URL(raw);
  if (url.protocol !== "https:" || url.username || url.password || url.port ||
      !ALLOWED_HOSTS.some(host => url.hostname === host || url.hostname.endsWith("." + host))) {
    throw new Error("Unsupported file download URL");
  }
  return url;
}

export function safeFileName(name?: string): string {
  const cleaned = (name ?? "upload.bin").replace(/[^a-zA-Z0-9._-]/g, "_").replace(/^\.+/, "").slice(0, 100);
  return cleaned || "upload.bin";
}

// The input endpoint must not be able to redirect to a private network.
// Stream reads are bounded regardless of a missing or false Content-Length.
async function readBounded(body: ReadableStream<Uint8Array>): Promise<Uint8Array> {
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_BYTES) throw new Error("File exceeds 25 MiB limit");
      chunks.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

function createServer(env: Env): McpServer {
  const server = new McpServer({ name: "chatgpt-file-transfer", version: "0.2.0" });
  server.registerTool("upload_file", {
    title: "Upload file to private R2",
    description: "Upload one ChatGPT-authorized file (max 25 MiB) to a private R2 bucket; returns an object key and SHA-256.",
    inputSchema: { file: fileSchema },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
    _meta: { "openai/fileParams": ["file"] },
  }, async ({ file }) => {
    try {
      const url = validateFileUrl(file.download_url);
      const response = await fetch(url.toString(), { redirect: "error", signal: AbortSignal.timeout(30_000) });
      if (!response.ok || !response.body) throw new Error("File download failed");
      const declared = response.headers.get("content-length");
      if (declared !== null && Number(declared) > MAX_BYTES) throw new Error("File exceeds 25 MiB limit");
      const bytes = await readBounded(response.body);
      const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
      const sha256 = Array.from(hash, b => b.toString(16).padStart(2, "0")).join("");
      const key = `chatgpt/${crypto.randomUUID()}/${safeFileName(file.file_name)}`;
      await env.FILES.put(key, bytes, {
        httpMetadata: { contentType: file.mime_type || "application/octet-stream" },
        customMetadata: { sha256 },
      });
      const result = { key, bytes: bytes.byteLength, sha256 };
      return { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result };
    } catch (e) {
      // No signed file URLs or credentials should be echoed to model output.
      const message = e instanceof Error ? e.message : "Upload failed";
      return { isError: true, content: [{ type: "text", text: message }] };
    }
  });
  return server;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname !== "/mcp") return new Response("Not found", { status: 404 });
    if (!env.TRANSFER_TOKEN) return new Response("Not configured", { status: 503 });
    if (request.headers.get("Authorization") !== `Bearer ${env.TRANSFER_TOKEN}`) {
      return new Response("Unauthorized", { status: 401 });
    }
    // Static per-request, sessionless MCP handler; no Durable Object needed.
    return createMcpHandler(() => createServer(env))(request, env);
  },
};
