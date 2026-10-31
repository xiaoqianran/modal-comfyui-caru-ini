import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { afterEach, beforeEach, mock, test } from "node:test";
import { z } from "zod";
import { fileSchema, uploadFile } from "../src/upload.ts";

const file = {
  download_url: "https://files.oaiusercontent.com/attachment?token=do-not-log",
  file_id: "file-private-id",
  file_name: "private-name.zip",
  mime_type: "application/zip",
};
const requestId = "test-request-id";
let logs;

beforeEach(() => {
  logs = [];
  mock.method(console, "info", value => logs.push(value));
  mock.method(console, "error", value => logs.push(value));
});
afterEach(() => mock.restoreAll());

test("file contract requires URL and ID, declares optional name and MIME, rejects string input", () => {
  const schema = z.toJSONSchema(fileSchema);
  assert.deepEqual(schema.required, ["download_url", "file_id"]);
  assert.deepEqual(Object.keys(schema.properties).sort(), ["download_url", "file_id", "file_name", "mime_type"]);
  assert.equal(schema.additionalProperties, false);
  assert.equal(fileSchema.safeParse(JSON.stringify(file)).success, false);
  assert.equal(fileSchema.safeParse({ download_url: file.download_url, file_id: file.file_id }).success, true);
});

test("upload stores exact bytes and matching digest; logs contain no attachment secrets", async () => {
  const bytes = new TextEncoder().encode("small authorized test file");
  const fetch = mock.method(globalThis, "fetch", async (_url, options) => {
    assert.equal(options.redirect, "manual");
    assert.ok(options.signal instanceof AbortSignal);
    return new Response(bytes);
  });
  const put = mock.fn(async (key, stored, options) => {
    assert.match(key, /^chatgpt\/[0-9a-f-]{36}\/private-name.zip$/);
    assert.deepEqual(stored, bytes);
    assert.equal(options.httpMetadata.contentType, "application/zip");
    assert.equal(options.customMetadata.sha256, createHash("sha256").update(bytes).digest("hex"));
    return {};
  });
  const result = await uploadFile(file, { put }, requestId);
  assert.equal(fetch.mock.callCount(), 1);
  assert.equal(put.mock.callCount(), 1);
  assert.equal(result.size, bytes.byteLength);
  assert.equal(result.request_id, requestId);
  assert.deepEqual(logs.map(log => log.event), ["upload_started", "upload_downloaded", "upload_stored"]);
  for (const secret of Object.values(file)) assert.ok(!JSON.stringify(logs).includes(secret));
});

test("untrusted URLs fail before any outbound call or write", async () => {
  const fetch = mock.method(globalThis, "fetch", async () => assert.fail("must not fetch"));
  const put = mock.fn(async () => assert.fail("must not write"));
  for (const url of ["invalid", "http://chatgpt.com/file", "https://chatgpt.com.evil.test/file",
    "https://evilchatgpt.com/file", "https://user:password@chatgpt.com/file", "https://127.0.0.1/file"]) {
    await assert.rejects(uploadFile({ ...file, download_url: url }, { put }, requestId), { code: "invalid_source" });
  }
  assert.equal(fetch.mock.callCount(), 0);
  assert.equal(put.mock.callCount(), 0);
});

test("expired source is distinguished from R2 failure without exposing the URL", async () => {
  mock.method(globalThis, "fetch", async () => new Response("expired", { status: 403 }));
  const put = mock.fn(async () => assert.fail("must not write"));
  await assert.rejects(uploadFile(file, { put }, requestId), { code: "source_http_error", sourceStatus: 403 });
  assert.equal(put.mock.callCount(), 0);
  assert.equal(logs.at(-1).stage, "download");
  assert.equal(logs.at(-1).source_status, 403);
  assert.ok(!JSON.stringify(logs).includes("do-not-log"));
});

test("download timeout returns its own code and never writes", async () => {
  mock.method(globalThis, "fetch", async () => { throw new DOMException(file.download_url, "TimeoutError"); });
  await assert.rejects(uploadFile(file, { put: () => assert.fail("must not write") }, requestId), { code: "source_timeout" });
  assert.ok(!JSON.stringify(logs).includes("do-not-log"));
});

test("redirects are identified without fetching their target", async () => {
  const fetch = mock.method(globalThis, "fetch", async () => new Response(null, {
    status: 302, headers: { Location: "http://127.0.0.1/private" },
  }));
  await assert.rejects(uploadFile(file, { put: () => assert.fail("must not write") }, requestId),
    { code: "source_redirect_blocked", sourceStatus: 302 });
  assert.equal(fetch.mock.callCount(), 1);
});

test("large declared content cancels the source without writing", async () => {
  let cancelled = false;
  mock.method(globalThis, "fetch", async () => new Response(new ReadableStream({
    cancel() { cancelled = true; },
  }), { headers: { "Content-Length": String(26 * 1024 * 1024) } }));
  await assert.rejects(uploadFile(file, { put: () => assert.fail("must not write") }, requestId), { code: "size_limit" });
  assert.equal(cancelled, true);
});

test("streaming size cap works without Content-Length and cancels oversized body", async () => {
  let cancelled = false;
  mock.method(globalThis, "fetch", async () => new Response(new ReadableStream({
    pull(controller) { controller.enqueue(new Uint8Array(1024 * 1024)); },
    cancel() { cancelled = true; },
  })));
  await assert.rejects(uploadFile(file, { put: () => assert.fail("must not write") }, requestId), { code: "size_limit" });
  assert.equal(cancelled, true);
});

test("interrupted stream never produces a partial R2 object", async () => {
  mock.method(globalThis, "fetch", async () => new Response(new ReadableStream({
    start(controller) { controller.error(new Error(file.download_url)); },
  })));
  await assert.rejects(uploadFile(file, { put: () => assert.fail("must not write") }, requestId), { code: "source_read_failed" });
  assert.ok(!JSON.stringify(logs).includes("do-not-log"));
});

test("R2 failure is identifiable and raw storage exceptions are not logged", async () => {
  mock.method(globalThis, "fetch", async () => new Response("test"));
  await assert.rejects(uploadFile(file, { put: async () => { throw new Error("private R2 exception"); } }, requestId), { code: "storage_write_failed" });
  assert.equal(logs.at(-1).stage, "storage");
  assert.ok(!JSON.stringify(logs).includes("private R2 exception"));
  await assert.rejects(uploadFile(file, { put: async () => null }, requestId), { code: "storage_write_failed" });
});
