import { McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { OAuthProvider, insufficientScope, type OAuthHelpers, type OAuthResourceContext } from "@cloudflare/workers-oauth-provider";
import { authHandler } from "./auth";
import { z } from "zod";
import { fileSchema, uploadFile, UploadError } from "./upload";

interface Env {
  FILES: R2Bucket;
  OAUTH_KV: KVNamespace;
  OAUTH_PROVIDER: OAuthHelpers;
  OWNER_PASSWORD?: string;
  DOWNLOAD_SECRET?: string;
  MCP_RESOURCE: string;
}

const DOWNLOAD_TTL_SECONDS = 600;
const KEY_PATTERN = /^chatgpt\/[0-9a-f-]{36}\/[A-Za-z0-9._-]{1,100}$/;
const encoder = new TextEncoder();

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

function makeServer(env: Env, origin: string, requestId: string): McpServer {
  const server = new McpServer({ name: "chatgpt-file-transfer", version: "0.5.0" });

  server.registerTool("upload_file", {
    title: "Upload ChatGPT file",
    description: "Upload a ChatGPT attachment (maximum 25 MiB) to private R2.",
    inputSchema: { file: fileSchema },
    outputSchema: { key: z.string(), size: z.number(), sha256: z.string(), request_id: z.string() },
    _meta: { "openai/fileParams": ["file"] },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: true },
  }, async ({ file }) => {
    try {
      return toolResult(await uploadFile(file, env.FILES, requestId));
    } catch (error) {
      return { isError: true, content: [{ type: "text", text: JSON.stringify({
        error: error instanceof UploadError ? error.code : "upload_failed",
        message: error instanceof UploadError ? error.message : "Upload failed.",
        request_id: requestId,
      }) }] };
    }
  });

  server.registerTool("get_download_url", {
    title: "Create temporary download URL",
    description: "Make a 10-minute read-only download URL for an uploaded object.",
    inputSchema: { key: z.string().regex(KEY_PATTERN) },
    outputSchema: { url: z.string(), sha256: z.string().nullable(), expires: z.number() },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  }, async ({ key }) => {
    const object = await env.FILES.head(key);
    if (!object || !env.DOWNLOAD_SECRET) {
      return { isError: true, content: [{ type: "text", text: "Object not found" }] };
    }
    const expires = Math.floor(Date.now() / 1000) + DOWNLOAD_TTL_SECONDS;
    const signature = await hmac(env.DOWNLOAD_SECRET, `${key}\n${expires}`);
    const url = new URL(`/download/${key}`, origin);
    url.searchParams.set("expires", String(expires));
    url.searchParams.set("sig", signature);
    return toolResult({ url: url.toString(), sha256: object.customMetadata?.sha256 ?? null, expires });
  });

  server.registerTool("delete_file", {
    title: "Delete uploaded file",
    description: "Permanently remove an uploaded object from R2.",
    inputSchema: { key: z.string().regex(KEY_PATTERN) },
    outputSchema: { deleted: z.boolean(), key: z.string() },
    annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: false },
  }, async ({ key }) => {
    await env.FILES.delete(key);
    return toolResult({ deleted: true, key });
  });
  return server;
}

async function serveDownload(request: Request, env: Env, path: string): Promise<Response> {
  if (request.method !== "GET" || !env.DOWNLOAD_SECRET) return new Response("Not found", { status: 404 });
  let key: string;
  try {
    key = decodeURIComponent(path.slice("/download/".length));
  } catch {
    return new Response("Invalid key", { status: 400 });
  }
  if (!KEY_PATTERN.test(key)) return new Response("Invalid key", { status: 400 });
  const url = new URL(request.url);
  const expires = Number(url.searchParams.get("expires"));
  const sig = url.searchParams.get("sig");
  const now = Math.floor(Date.now() / 1000);
  if (!Number.isSafeInteger(expires) || expires < now || expires > now + DOWNLOAD_TTL_SECONDS || !sig || !/^[a-f0-9]{64}$/.test(sig)) {
    return new Response("Invalid or expired link", { status: 403 });
  }
  const expected = await hmac(env.DOWNLOAD_SECRET, `${key}\n${expires}`);
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
  fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/download/")) return serveDownload(request, env, url.pathname);
    if (!env.MCP_RESOURCE || !env.DOWNLOAD_SECRET) {
      return Promise.resolve(new Response("Not configured", { status: 503 }));
    }
    const provider = new OAuthProvider<Env>({
      authorizeEndpoint: "/authorize",
      tokenEndpoint: "/oauth/token",
      apiRoute: "/mcp",
      apiHandler: {
        async fetch(req: Request, bindings: Env, ctx: ExecutionContext) {
          const auth = (ctx as OAuthResourceContext<{ userId: string }>).auth;
          if (!auth.scope.includes("mcp:files")) {
            return insufficientScope(auth, ["mcp:files"]);
          }
          const requestId = crypto.randomUUID();
          console.info({ event: "mcp_authenticated", request_id: requestId, method: req.method });
          const response = await createMcpHandler(() => makeServer(bindings, url.origin, requestId))(req, bindings, ctx);
          const result = new Response(response.body, response);
          result.headers.set("X-Request-ID", requestId);
          return result;
        },
      },
      defaultHandler: authHandler,
      scopesSupported: ["mcp:files"],
      requiredScopes: ["mcp:files"],
      resourceMetadata: {
        resource: env.MCP_RESOURCE,
        authorization_servers: [new URL(env.MCP_RESOURCE).origin],
      },
      clientIdMetadataDocumentEnabled: true,
    });
    return provider.fetch(request, env, ctx);
  },
};
