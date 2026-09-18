// ═══════════════════════════════════════════════════════════════
// email-open — tracking pixel for MicrodyneHunter
//
// Returns a 1x1 transparent GIF and records the open against the
// email_tracking row identified by ?id=<tracking_id>:
//   · flips opened = true, stamps opened_at (first open) and last_opened_at
//   · increments open_count
//   · stores device, OS, mail client, user agent and IP
//   · appends a row to email_open_events (one per open)
//   · flips leads.email_opened so the dashboard funnel picks it up
//
// Deploy with JWT verification OFF — mail clients send no auth header.
//   supabase functions deploy email-open --no-verify-jwt
// ═══════════════════════════════════════════════════════════════

const SUPABASE_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";

// 1x1 transparent GIF
const PIXEL = Uint8Array.from(
  atob("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"),
  (c) => c.charCodeAt(0),
);

const PIXEL_HEADERS = {
  "Content-Type": "image/gif",
  "Content-Length": String(PIXEL.byteLength),
  // never let a proxy serve this from cache — every open must hit us
  "Cache-Control": "no-store, no-cache, must-revalidate, proxy-revalidate, max-age=0",
  "Pragma": "no-cache",
  "Expires": "0",
};

function classify(ua: string) {
  const s = ua.toLowerCase();
  let email_client = "Unknown";
  if (s.includes("googleimageproxy")) email_client = "Gmail";
  else if (s.includes("yahoomailproxy")) email_client = "Yahoo Mail";
  else if (s.includes("outlook") || s.includes("microsoft office")) email_client = "Outlook";
  else if (s.includes("thunderbird")) email_client = "Thunderbird";
  else if (s.includes("appleWebKit".toLowerCase()) && s.includes("mobile/")) email_client = "Apple Mail";
  else if (s.includes("zoho")) email_client = "Zoho Mail";
  else if (s.includes("superhuman")) email_client = "Superhuman";
  else if (s.includes("proofpoint") || s.includes("barracuda") || s.includes("mimecast")) email_client = "Security scanner";

  let os = "Unknown";
  if (s.includes("windows")) os = "Windows";
  else if (s.includes("iphone") || s.includes("ipad") || s.includes("ios")) os = "iOS";
  else if (s.includes("android")) os = "Android";
  else if (s.includes("mac os") || s.includes("macintosh")) os = "macOS";
  else if (s.includes("linux")) os = "Linux";

  let device_type = "Unknown";
  if (s.includes("ipad") || s.includes("tablet")) device_type = "Tablet";
  else if (s.includes("mobile") || s.includes("iphone") || s.includes("android")) device_type = "Mobile";
  else if (os === "Windows" || os === "macOS" || os === "Linux") device_type = "Desktop";
  // Gmail/Yahoo fetch through a proxy, so the real device is hidden
  if (email_client === "Gmail" || email_client === "Yahoo Mail") device_type = "Proxy";

  return { device_type, os, email_client };
}

async function rest(path: string, init: RequestInit = {}) {
  return await fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: SERVICE_KEY,
      Authorization: `Bearer ${SERVICE_KEY}`,
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
}

async function recordOpen(trackingId: string, ua: string, ip: string) {
  const res = await rest(
    `email_tracking?tracking_id=eq.${encodeURIComponent(trackingId)}` +
      `&select=id,lead_id,open_count,opened_at&limit=1`,
  );
  if (!res.ok) throw new Error(`lookup failed: ${res.status} ${await res.text()}`);
  const rows = await res.json();
  if (!rows.length) return; // unknown pixel id — still serve the image

  const row = rows[0];
  const now = new Date().toISOString();
  const { device_type, os, email_client } = classify(ua);

  const update = await rest(`email_tracking?id=eq.${row.id}`, {
    method: "PATCH",
    headers: { Prefer: "return=minimal" },
    body: JSON.stringify({
      opened: true,
      opened_at: row.opened_at ?? now, // keep the first open
      last_opened_at: now,
      open_count: (row.open_count ?? 0) + 1,
      user_agent: ua.slice(0, 500),
      ip_address: ip,
      device_type,
      os,
      email_client,
    }),
  });
  if (!update.ok) throw new Error(`update failed: ${update.status} ${await update.text()}`);

  // one row per open, so repeat opens are visible as history
  await rest("email_open_events", {
    method: "POST",
    headers: { Prefer: "return=minimal" },
    body: JSON.stringify({
      tracking_id: trackingId,
      lead_id: row.lead_id,
      opened_at: now,
      user_agent: ua.slice(0, 500),
      ip_address: ip,
      device_type,
      os,
      email_client,
    }),
  });

  if (row.lead_id) {
    await rest(`leads?id=eq.${row.lead_id}`, {
      method: "PATCH",
      headers: { Prefer: "return=minimal" },
      body: JSON.stringify({ email_opened: true, email_opened_at: now }),
    });
  }
}

Deno.serve(async (req) => {
  const id = new URL(req.url).searchParams.get("id");
  const ua = req.headers.get("user-agent") ?? "";
  const ip = (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim();

  if (id) {
    const work = recordOpen(id, ua, ip).catch((e) => console.error("[email-open]", e.message));
    // @ts-ignore EdgeRuntime is provided by Supabase; fall back to awaiting locally
    if (typeof EdgeRuntime !== "undefined" && EdgeRuntime.waitUntil) EdgeRuntime.waitUntil(work);
    else await work;
  }

  // the image always returns, whatever happens above — never break the email
  return new Response(PIXEL, { headers: PIXEL_HEADERS });
});
