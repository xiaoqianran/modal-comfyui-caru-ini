import { z } from "zod";

const MAX_BYTES = 25 * 1024 * 1024;
// Accept public HTTPS hosts, not a fixed provider list. Workers fetch handles DNS.
const BLOCKED_SUFFIXES = [".localhost", ".local", ".internal", ".home.arpa", ".test", ".invalid"];

export const fileSchema = z.object({
  download_url: z.string(),
  file_id: z.string().min(1),
  mime_type: z.string().optional(),
  file_name: z.string().optional(),
}).strict();

export class UploadError extends Error {
  code: string;
  sourceStatus?: number;

  constructor(code: string, message: string, sourceStatus?: number) {
    super(message);
    this.code = code;
    this.sourceStatus = sourceStatus;
  }
}

function sourceUrl(value: string): URL {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new UploadError("invalid_source", "The attachment download URL is invalid.");
  }
  const host = url.hostname.toLowerCase().replace(/\.$/, "");
  // Reject all IP literals (including IPv6 and alternate IPv4 formats) and
  // non-public/single-label hostnames. Never accept a client-selected port.
  const ipLiteral = /^\[.*\]$/.test(host) || /^\d+\.\d+\.\d+\.\d+$/.test(host);
  const domain = /^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(host) &&
    host.split(".").every(part => part.length <= 63 && !part.startsWith("-") && !part.endsWith("-"));
  if (url.protocol !== "https:" || url.username || url.password || url.port ||
      !domain || ipLiteral || host === "localhost" ||
      BLOCKED_SUFFIXES.some(suffix => host.endsWith(suffix)) ||
      host === "metadata.google.internal" || host === "169.254.169.254.nip.io") {
    throw new UploadError("invalid_source", "Attachment URL must use a public HTTPS hostname.");
  }
  return url;
}

function cleanName(value = "upload.bin"): string {
  return value.replace(/[^A-Za-z0-9._-]/g, "_").replace(/^\.+/, "").slice(0, 100) || "upload.bin";
}

async function readFile(response: Response): Promise<Uint8Array> {
  const length = response.headers.get("content-length");
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > MAX_BYTES)) {
    await response.body?.cancel().catch(() => {});
    throw new UploadError("size_limit", "Attachment exceeds 25 MiB or has an invalid Content-Length.");
  }
  const reader = response.body!.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > MAX_BYTES) throw new UploadError("size_limit", "Attachment exceeds 25 MiB.");
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

// Log only bounded diagnostics. Never include attachment URLs, names, IDs or raw exceptions.
export async function uploadFile(file: z.infer<typeof fileSchema>, bucket: Pick<R2Bucket, "put">, requestId: string) {
  let stage = "validate_source";
  console.info({ event: "upload_started", request_id: requestId });
  try {
    const url = sourceUrl(file.download_url);
    stage = "download";
    const response = await fetch(url.toString(), {
      redirect: "manual", signal: AbortSignal.timeout(30_000),
    });
    if (response.status >= 300 && response.status < 400) {
      await response.body?.cancel().catch(() => {});
      throw new UploadError("source_redirect_blocked", "Attachment source returned a redirect; no redirect was followed.", response.status);
    }
    if (!response.ok || !response.body) {
      await response.body?.cancel().catch(() => {});
      throw new UploadError("source_http_error", "Attachment source is unavailable; select the file again for a fresh URL.", response.status);
    }
    stage = "read_source";
    const bytes = await readFile(response);
    console.info({ event: "upload_downloaded", request_id: requestId, size: bytes.byteLength });
    stage = "checksum";
    const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes.buffer as ArrayBuffer));
    const sha256 = [...hash].map(byte => byte.toString(16).padStart(2, "0")).join("");
    const key = `chatgpt/${crypto.randomUUID()}/${cleanName(file.file_name)}`;
    stage = "storage";
    const object = await bucket.put(key, bytes, {
      httpMetadata: { contentType: file.mime_type || "application/octet-stream" },
      customMetadata: { sha256 },
    });
    if (!object) throw new UploadError("storage_write_failed", "R2 did not confirm the write.");
    console.info({ event: "upload_stored", request_id: requestId, size: bytes.byteLength });
    return { key, size: bytes.byteLength, sha256, request_id: requestId };
  } catch (error) {
    const failure = error instanceof UploadError ? error :
      ["download", "read_source"].includes(stage) ? new UploadError(
        error instanceof Error && ["TimeoutError", "AbortError"].includes(error.name) ? "source_timeout" :
          stage === "read_source" ? "source_read_failed" : "source_fetch_failed",
        "Could not download the attachment (network, timeout or interrupted stream).",
      ) : new UploadError(stage === "storage" ? "storage_write_failed" : "upload_failed", "The Worker could not complete the upload.");
    console.error({ event: "upload_failed", request_id: requestId, stage,
      code: failure.code, source_status: failure.sourceStatus });
    throw failure;
  }
}
