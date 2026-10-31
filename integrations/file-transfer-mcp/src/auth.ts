import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";

interface AuthEnv {
  OAUTH_PROVIDER: OAuthHelpers;
  OWNER_PASSWORD?: string;
}

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, ch =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]!
  );
}

async function passwordMatches(provided: string, configured: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(provided)),
    crypto.subtle.digest("SHA-256", encoder.encode(configured)),
  ]);
  const left = new Uint8Array(a), right = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < left.length; i++) diff |= left[i] ^ right[i];
  return diff === 0;
}

export const authHandler = {
  async fetch(request: Request, env: AuthEnv): Promise<Response> {
    if (new URL(request.url).pathname !== "/authorize") {
      return new Response("Not found", { status: 404 });
    }
    if (!env.OWNER_PASSWORD || env.OWNER_PASSWORD.length < 24) {
      return new Response("Owner passphrase not configured", { status: 503 });
    }

    const oauth = env.OAUTH_PROVIDER;
    if (request.method === "GET") {
      const parsed = await oauth.parseAuthRequest(request);
      const details = await oauth.describeConsent(parsed);
      const consent = await oauth.beginConsent(parsed);
      consent.headers.set("Content-Type", "text/html; charset=utf-8");
      consent.headers.set("Cache-Control", "no-store");
      const page = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Authorize file transfer</title></head>
<body><main style="max-width:30rem;margin:5rem auto;font:16px system-ui">
<h1>Authorize file transfer</h1>
<p>Client: <strong>${escapeHtml(details.clientName)}</strong></p>
<p>${details.clientDomain ? `Published by <strong>${escapeHtml(details.clientDomain)}</strong>` : "Client name is self-asserted and unverified."}</p>
<p>Redirect: <strong>${escapeHtml(details.redirectHost)}</strong></p>
${details.redirectIsLoopback ? "<p><strong>This sends access to an app on your computer. Continue only if you started signing in from it.</strong></p>" : ""}
<p>Scopes: ${escapeHtml(details.scope.join(" "))}</p>
<form method="POST" action="/authorize">
<input type="hidden" name="handle" value="${escapeHtml(consent.handle)}">
<label>Owner passphrase <input type="password" name="password" required minlength="24" autocomplete="off"></label>
<p><button type="submit" name="decision" value="approve">Approve</button>
<button type="submit" name="decision" value="deny" formnovalidate>Deny</button></p>
</form></main></body></html>`;
      return new Response(page, { headers: consent.headers });
    }
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
    const form = await request.formData();
    const handle = String(form.get("handle") ?? "");
    if (form.get("decision") !== "approve") {
      const denied = await oauth.denyConsent(request, handle);
      return new Response(null, { status: 302, headers: denied.headers });
    }
    if (!await passwordMatches(String(form.get("password") ?? ""), env.OWNER_PASSWORD)) {
      return new Response("Incorrect passphrase", { status: 403 });
    }
    const approved = await oauth.approveConsent(request, handle);
    const { redirectTo } = await oauth.completeAuthorization({
      request: approved.request,
      userId: "owner",
      metadata: {},
      scope: approved.request.scope,
      props: { userId: "owner" },
    });
    approved.headers.set("Location", redirectTo);
    return new Response(null, { status: 302, headers: approved.headers });
  },
};
