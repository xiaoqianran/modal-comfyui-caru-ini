import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { z } from "zod";

interface Env {
  FILES: R2Bucket;
  TRANSFER_TOKEN?: string;
}

const MAX_BYTES = 25 * 1024 * 1024;
const DOWNLOAD_TTL_SECONDS = 600;
const HOSTS = ["chatgpt.com", "openai.com", "oaiusercontent.com"] as const;
const KEY_PATTERN = /^chatgpt\/[0-9a-f-]{36}\/[A-Za-z0-9._-]{1,100}$/;
const encoder = new TextEncoder();

const fileSchema = z.object({
  download_url: z.string(),
  file_id: z.string().min(1),
  mime_type: z.string().optional(),
  file_name: z.string().optional(),
}).strict();

function sourceUrl(value: string): URL {
  const url = new URL(value);
  if (url.protocol !== "https:" || url.username || url.password || url.port ||
    !HOSTS.some(host => url.hostname === host || url.hostname.endsWith("." + host))) {
    throw new Error("Untrusted file download host");
  }
  return url;
}

function cleanName(value = "upload.bin"): string {
  return value.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^\.+/, "").slice(0, 100) || "upload.bin";
}

async function readFile(response: Response): Promise<Uint8Array> {
  if (!response.ok || !response.body) throw new Error("File download failed");
  const contentLength = response.headers.get("content-length");
  if (contentLength !== null && (!Number.isFinite(+contentLength) || +contentLength > MAX_BYTES)) {
    throw new Error("File exceeds size limit");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_BYTES) throw new Error("File exceeds size limit");
      chunks.push(value);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  const result = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

async function sha256(bytes: Uint8Array): Promise<string> {
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return [...hash].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

async function hmac(secret: string, payload: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
  );
  const signature = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(payload)));
  return [...signature].map(byte => byte.toString(16).padStart(2, "0")).join("");
}

function toolResult(result: Record<string, unknown>) {
  return {
    content: [{ type: "text" as const, text: JSON.stringify(result) }],
    structuredContent: result,
  };
}

function makeServer(env: Env, origin: string): McpServer {
  const server = new McpServer({ name: "chatgpt-file-transfer", version: "0.3.0" });

  server.registerTool("upload_file", {
    title: "Upload ChatGPT file",
    description: "Upload an explicitly authorized ChatGPT attachment (maximum 25 MiB) to private R2.",
    inputSchema: { file: fileSchema },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
    _meta: { "openai/fileParams": ["file"] },
  }, async ({ file }) => {
    try {
      const response = await fetch(sourceUrl(file.download_url).toString(), {
        redirect: "error", signal: AbortSignal.timeout(30_000),
      });
      const bytes = await readFile(response);
      const key = `chatgpt/${crypto.randomUUID()}/${cleanName(file.file_name)}`;
      const digest = await sha256(bytes);
      await env.FILES.put(key, bytes, {
        httpMetadata: { contentType: file.mime_type || "application/octet-stream" },
        customMetadata: { sha256: digest },
      });
      return toolResult({ key, size: bytes.byteLength, sha256: digest });
    } catch {
      return { isError: true, content: [{ type: "text", text: "Upload failed: invalid URL, inaccessible file, or size limit." }] };
    }
  });

  server.registerTool("get_download_url", {
    title: "Create temporary download URL",
    description: "Make a 10-minute read-only download URL for an uploaded object.",
    inputSchema: { key: z.string().regex(KEY_PATTERN) },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  }, async ({ key }) => {
    const object = await env.FILES.head(key);
    if (!object || !env.TRANSFER_TOKEN) {
      return { isError: true, content: [{ type: "text", text: "Object not found" }] };
    }
    const expires = Math.floor(Date.now() / 1000) + DOWNLOAD_TTL_SECONDS;
    const signature = await hmac(env.TRANSFER_TOKEN, `${key}\n${expires}`);
    const url = new URL(`/download/${key}`, origin);
    url.searchParams.set("expires", String(expires));
    url.searchParams.set("sig", signature);
    return toolResult({ url: url.toString(), sha256: object.customMetadata?.sha256 ?? null, expires });
  });

  server.registerTool("delete_file", {
    title: "Delete uploaded file",
    description: "Permanently remove an uploaded object from R2.",
    inputSchema: { key: z.string().regex(KEY_PATTERN) },
    annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: false },
  }, async ({ key }) => {
    await env.FILES.delete(key);
    return toolResult({ deleted: true, key });
  });
  return server;
}

async function serveDownload(request: Request, env: Env, path: string): Promise<Response> {
  if (request.method !== "GET" || !env.TRANSFER_TOKEN) return new Response("Not found", { status: 404 });
  const key = decodeURIComponent(path.slice("/download/".length));
  if (!KEY_PATTERN.test(key)) return new Response("Invalid key", { status: 400 });
  const url = new URL(request.url);
  const expires = Number(url.searchParams.get("expires"));
  const sig = url.searchParams.get("sig");
  const now = Math.floor(Date.now() / 1000);
  if (!Number.isSafeInteger(expires) || expires < now || expires > now + DOWNLOAD_TTL_SECONDS || !sig || !/^[a-f0-9]{64}$/.test(sig)) {
    return new Response("Invalid or expired link", { status: 403 });
  }
  const expected = await hmac(env.TRANSFER_TOKEN, `${key}\n${expires}`);
  if (sig !== expected) return new Response("Invalid signature", { status: 403 });
  const object = await env.FILES.get(key);
  if (!object) return new Response("Not found", { status: 404 });
  return new Response(object.body, {
    headers: {
      "Content-Type": object.httpMetadata?.contentType || "application/octet-stream",
      "Content-Disposition": "attachment; filename=\"download.bin\"",
      "Cache-Control": "private, no-store",
      "X-Content-SHA256": object.customMetadata?.sha256 || "",
    },
  });
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/download/")) return serveDownload(request, env, url.pathname);
    if (url.pathname !== "/mcp") return new Response("Not found", { status: 404 });
    if (!env.TRANSFER_TOKEN) return new Response("Not configured", { status: 503 });
    if (request.headers.get("Authorization") !== `Bearer ${env.TRANSFER_TOKEN}`) {
      return new Response("Unauthorized", { status: 401 });
    }
    return createMcpHandler(() => makeServer(env, url.origin), { corsOptions: false })(request, env, ctx);
  },
};
