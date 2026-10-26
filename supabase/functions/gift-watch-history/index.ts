const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const WRITER_HASH = "7a80cff5670c13ae5477de4ef9c861dc5d3b8c9fb397d80e9c2169a373aed0eb";
const ALLOWED_ORIGIN = "https://topcvetok.github.io";

function cors(req: Request) {
  const origin = req.headers.get("origin") || "";
  return {
    "access-control-allow-origin": origin === ALLOWED_ORIGIN ? origin : ALLOWED_ORIGIN,
    "access-control-allow-headers": "content-type,x-writer-token",
    "access-control-allow-methods": "POST,OPTIONS",
    "content-type": "application/json; charset=utf-8",
    "vary": "Origin",
  };
}

function response(req: Request, value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: cors(req) });
}

async function sha256(value: string) {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(
    new Uint8Array(digest),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
}

function clean(value: unknown, limit = 300) {
  return typeof value === "string" ? value.trim().slice(0, limit) : "";
}

function validSettings(value: unknown) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return JSON.stringify(value).length <= 50000;
}

async function rest(url: URL, init: RequestInit = {}) {
  const result = await fetch(url, {
    ...init,
    headers: {
      "apikey": SERVICE_KEY,
      "authorization": `Bearer ${SERVICE_KEY}`,
      ...(init.headers || {}),
    },
  });
  if (!result.ok) {
    const detail = (await result.text()).slice(0, 500);
    throw new Error(`database ${result.status}: ${detail}`);
  }
  const body = await result.text();
  return body ? JSON.parse(body) : null;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: cors(req) });
  }
  try {
    if (req.method === "POST") {
      const input = await req.json();
      if (input.action === "settings_read" || input.action === "settings_save") {
        const token = clean(input.token, 200);
        if (token.length < 32) {
          return response(req, { error: "invalid token" }, 400);
        }
        const url = new URL(`${SUPABASE_URL}/rest/v1/gift_watch_settings`);
        url.searchParams.set("access_hash", `eq.${await sha256(token)}`);
        if (input.action === "settings_read") {
          url.searchParams.set("select", "settings,updated_at");
          url.searchParams.set("limit", "1");
          const rows = await rest(url);
          if (!Array.isArray(rows) || !rows.length) {
            return response(req, { error: "unknown profile" }, 403);
          }
          return response(req, {
            settings: rows[0].settings || {},
            updated_at: rows[0].updated_at,
          });
        }
        if (!validSettings(input.settings)) {
          return response(req, { error: "invalid settings" }, 400);
        }
        const rows = await rest(url, {
          method: "PATCH",
          headers: {
            "content-type": "application/json",
            "prefer": "return=representation",
          },
          body: JSON.stringify({
            settings: input.settings,
            updated_at: new Date().toISOString(),
          }),
        });
        if (!Array.isArray(rows) || !rows.length) {
          return response(req, { error: "unknown profile" }, 403);
        }
        return response(req, { ok: true, updated_at: rows[0].updated_at });
      }
      if (input.action === "read") {
        const token = clean(input.token, 200);
        const kind = clean(input.kind, 20);
        const offset = Number(input.offset ?? 0);
        if (
          token.length < 32 || !["offer", "upgrade"].includes(kind) ||
          !Number.isInteger(offset) || offset < 0 || offset > 1000000
        ) {
          return response(req, { error: "invalid request" }, 400);
        }
        const url = new URL(`${SUPABASE_URL}/rest/v1/gift_watch_history`);
        url.searchParams.set("access_hash", `eq.${await sha256(token)}`);
        url.searchParams.set("kind", `eq.${kind}`);
        url.searchParams.set(
          "select",
          "kind,event_key,title,market,price_ton,floor_ton,model,backdrop," +
            "owner_username,basis,target_url,preview_url,extra,sold,created_at,updated_at",
        );
        url.searchParams.set("order", "created_at.desc");
        url.searchParams.set("offset", String(offset));
        url.searchParams.set("limit", "101");
        const rows = await rest(url);
        return response(req, {
          items: Array.isArray(rows) ? rows.slice(0, 100) : [],
          has_more: Array.isArray(rows) && rows.length > 100,
        });
      }

      const writer = req.headers.get("x-writer-token") || "";
      if (writer.length < 32 || await sha256(writer) !== WRITER_HASH) {
        return response(req, { error: "unauthorized" }, 401);
      }
      if (input.action === "settings_register") {
        const chatId = clean(input.chat_id, 100);
        const accessToken = clean(input.access_token, 200);
        if (!chatId || accessToken.length < 32) {
          return response(req, { error: "invalid profile" }, 400);
        }
        const url = new URL(`${SUPABASE_URL}/rest/v1/gift_watch_settings`);
        url.searchParams.set("on_conflict", "chat_id");
        await rest(url, {
          method: "POST",
          headers: {
            "content-type": "application/json",
            "prefer": "resolution=merge-duplicates,return=minimal",
          },
          body: JSON.stringify({
            chat_id: chatId,
            access_hash: await sha256(accessToken),
          }),
        });
        return response(req, { ok: true });
      }
      if (input.action === "settings_sync") {
        const url = new URL(`${SUPABASE_URL}/rest/v1/gift_watch_settings`);
        url.searchParams.set("select", "chat_id,settings,updated_at");
        url.searchParams.set("order", "updated_at.asc");
        url.searchParams.set("limit", "1000");
        const rows = await rest(url);
        return response(req, { profiles: Array.isArray(rows) ? rows : [] });
      }
      const kind = clean(input.kind, 20);
      const chatId = clean(input.chat_id, 100);
      const accessToken = clean(input.access_token, 200);
      const eventKey = clean(input.event_key, 200);
      if (input.action === "sold") {
        if (kind !== "offer" || !chatId || accessToken.length < 32 || !eventKey) {
          return response(req, { error: "invalid sold record" }, 400);
        }
        const url = new URL(`${SUPABASE_URL}/rest/v1/gift_watch_history`);
        url.searchParams.set("chat_id", `eq.${chatId}`);
        url.searchParams.set("access_hash", `eq.${await sha256(accessToken)}`);
        url.searchParams.set("kind", "eq.offer");
        url.searchParams.set("event_key", `eq.${eventKey}`);
        await rest(url, {
          method: "PATCH",
          headers: { "content-type": "application/json", "prefer": "return=minimal" },
          body: JSON.stringify({ sold: true, updated_at: new Date().toISOString() }),
        });
        return response(req, { ok: true });
      }
      const title = clean(input.title, 300);
      if (
        !["offer", "upgrade"].includes(kind) || !chatId ||
        accessToken.length < 32 || !eventKey || !title
      ) {
        return response(req, { error: "invalid record" }, 400);
      }
      const row = {
        chat_id: chatId,
        access_hash: await sha256(accessToken),
        kind,
        event_key: eventKey,
        title,
        market: clean(input.market, 50) || null,
        price_ton: input.price_ton ?? null,
        floor_ton: input.floor_ton ?? null,
        model: clean(input.model) || null,
        backdrop: clean(input.backdrop) || null,
        owner_username: clean(input.owner_username, 100) || null,
        basis: clean(input.basis, 500) || null,
        target_url: clean(input.target_url, 1000) || null,
        preview_url: clean(input.preview_url, 1000) || null,
        extra: typeof input.extra === "object" && input.extra ? input.extra : {},
        sold: input.sold === true,
        created_at: clean(input.created_at, 100) || new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      const url = new URL(`${SUPABASE_URL}/rest/v1/gift_watch_history`);
      url.searchParams.set("on_conflict", "chat_id,kind,event_key");
      const result = await rest(url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "prefer": "resolution=merge-duplicates,return=representation",
        },
        body: JSON.stringify(row),
      });
      return response(req, {
        ok: true,
        item: Array.isArray(result) ? result[0] : result,
      });
    }
    return response(req, { error: "method not allowed" }, 405);
  } catch (error) {
    console.error(error);
    return response(req, { error: "temporary failure" }, 500);
  }
});
