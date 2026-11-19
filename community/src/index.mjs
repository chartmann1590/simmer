// SPDX-License-Identifier: GPL-3.0-or-later
// Simmer community: a free, text-only recipe exchange on Cloudflare's free tier (Workers + D1 + Workers AI).
//
//   GET    /v1/recipes?q=&tag=&sort=new|popular&before=   list (newest first, 30 a page)
//   GET    /v1/recipes/:id                                one recipe
//   POST   /v1/recipes        {device, recipe}            share: checked by moderate() before it is visible
//   POST   /v1/recipes/:id/save    {device}               counts a save (once per device)
//   POST   /v1/recipes/:id/report  {device, reason}       3 reports from different devices hide it
//   DELETE /v1/recipes/:id    {device, key}               the person who shared it takes it down
//   GET    /v1/health
//
// No accounts, no emails, no pictures, no links. A device is a random id the app makes; only a salted hash of it
// (and of the IP, for rate limits) is stored. Rate limits: 5 shares and 20 reports a day per device and per IP.

import { moderate, Rejected } from "./moderate.mjs";

const GUARD_MODEL = "@cf/meta/llama-guard-3-8b";
const PAGE = 30;
const HIDE_AFTER = 3;
const DAILY = { share: 5, report: 20, save: 200 };

const json = (doc, status = 200) => new Response(JSON.stringify(doc), {
  status,
  headers: { "Content-Type": "application/json", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
});
const fail = (message, status = 400, code = "error") => json({ error: message, code }, status);

async function sha(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function newId() {
  const b = crypto.getRandomValues(new Uint8Array(9));
  return [...b].map((x) => "abcdefghijkmnopqrstuvwxyz23456789"[x % 33]).join("");
}

async function body(request) {
  const len = Number(request.headers.get("Content-Length") || 0);
  if (len > 128 * 1024) throw new Rejected("That recipe is too long to share.", "too_large");
  const text = await request.text();
  if (text.length > 128 * 1024) throw new Rejected("That recipe is too long to share.", "too_large");
  try {
    return JSON.parse(text || "{}");
  } catch {
    throw new Rejected("That request wasn't readable.", "invalid");
  }
}

async function who(request, env, doc) {
  const device = String(doc.device || "");
  if (!/^[A-Za-z0-9-]{16,64}$/.test(device)) throw new Rejected("Update Simmer to share and report.", "invalid");
  const salt = env.HASH_SALT || "simmer";
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  return { device: await sha(`${salt}:d:${device}`), ip: await sha(`${salt}:i:${ip}`) };
}

async function limited(env, kind, ids) {
  const day = new Date().toISOString().slice(0, 10);
  for (const id of [ids.device, ids.ip]) {
    const row = await env.DB.prepare("SELECT n FROM limits WHERE day = ? AND kind = ? AND who = ?").bind(day, kind, id).first();
    if (row && row.n >= DAILY[kind]) return true;
  }
  const stmt = "INSERT INTO limits (day, kind, who, n) VALUES (?, ?, ?, 1) ON CONFLICT (day, kind, who) DO UPDATE SET n = n + 1";
  await env.DB.batch([
    env.DB.prepare(stmt).bind(day, kind, ids.device),
    env.DB.prepare(stmt).bind(day, kind, ids.ip),
    env.DB.prepare("DELETE FROM limits WHERE day < ?").bind(day),   // yesterday's counters are not kept
  ]);
  return false;
}

function summary(row) {
  return {
    id: row.id, title: row.title, author: row.author, servings: row.servings, total: row.total,
    tags: JSON.parse(row.tags || "[]"), saves: row.saves, created: row.created,
    ingredients: row.ingredient_count ?? undefined,
  };
}

function full(row) {
  return {
    ...summary(row), prep: row.prep, cook: row.cook, notes: row.notes,
    ingredients: JSON.parse(row.ingredients || "[]"), steps: JSON.parse(row.steps || "[]"),
  };
}

async function list(env, url) {
  const q = (url.searchParams.get("q") || "").trim().toLowerCase().slice(0, 60);
  const tag = (url.searchParams.get("tag") || "").trim().toLowerCase().slice(0, 24);
  const sort = url.searchParams.get("sort") === "popular" ? "popular" : "new";
  const before = Number(url.searchParams.get("before") || 0);
  const where = ["hidden = 0"];
  const args = [];
  for (const word of q.split(/\s+/).filter(Boolean).slice(0, 5)) {
    where.push("(lower(title) LIKE ? OR lower(ingredients) LIKE ? OR lower(tags) LIKE ?)");
    args.push(`%${word}%`, `%${word}%`, `%${word}%`);
  }
  if (tag) {
    where.push("lower(tags) LIKE ?");
    args.push(`%"${tag}"%`);
  }
  if (before && sort === "new") {
    where.push("created < ?");
    args.push(before);
  }
  const order = sort === "popular" ? "saves DESC, created DESC" : "created DESC";
  const rows = await env.DB.prepare(`SELECT id, title, author, servings, total, tags, saves, created,
      json_array_length(ingredients) AS ingredient_count FROM recipes WHERE ${where.join(" AND ")}
      ORDER BY ${order} LIMIT ${PAGE}`).bind(...args).all();
  const recipes = (rows.results || []).map(summary);
  return json({ recipes, more: recipes.length === PAGE });
}

async function share(request, env) {
  const doc = await body(request);
  const ids = await who(request, env, doc);
  if (await limited(env, "share", ids)) return fail("You've shared a lot today. Try again tomorrow.", 429, "rate_limited");
  const guard = async (text) => env.AI.run(GUARD_MODEL, { messages: [{ role: "user", content: text }] });
  const r = await moderate(doc.recipe, guard);
  const dupe = await env.DB.prepare("SELECT id FROM recipes WHERE device = ? AND lower(title) = ? AND hidden = 0")
    .bind(ids.device, r.title.toLowerCase()).first();
  if (dupe) return fail("You've already shared a recipe with this name.", 409, "duplicate");
  const id = newId();
  const key = newId() + newId();
  const now = Date.now();
  await env.DB.prepare(`INSERT INTO recipes (id, title, author, servings, prep, cook, total, ingredients, steps, notes,
      tags, saves, reports, hidden, created, device, delete_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)`)
    .bind(id, r.title, r.author, r.servings, r.prep, r.cook, r.total, JSON.stringify(r.ingredients),
      JSON.stringify(r.steps), r.notes, JSON.stringify(r.tags), now, ids.device, await sha(key)).run();
  return json({ id, key, recipe: { id, ...r, saves: 0, created: now } }, 201);
}

async function save(request, env, id) {
  const doc = await body(request);
  const ids = await who(request, env, doc);
  if (await limited(env, "save", ids)) return fail("Too many saves today.", 429, "rate_limited");
  const res = await env.DB.prepare("INSERT OR IGNORE INTO saves (recipe, device) VALUES (?, ?)").bind(id, ids.device).run();
  if (res.meta?.changes) await env.DB.prepare("UPDATE recipes SET saves = saves + 1 WHERE id = ?").bind(id).run();
  return json({ ok: true });
}

async function report(request, env, id) {
  const doc = await body(request);
  const ids = await who(request, env, doc);
  if (await limited(env, "report", ids)) return fail("You've sent a lot of reports today.", 429, "rate_limited");
  const reason = String(doc.reason || "").slice(0, 40);
  const res = await env.DB.prepare("INSERT OR IGNORE INTO reports (recipe, device, reason, at) VALUES (?, ?, ?, ?)")
    .bind(id, ids.device, reason, Date.now()).run();
  if (res.meta?.changes) {
    await env.DB.prepare(`UPDATE recipes SET reports = reports + 1,
        hidden = CASE WHEN reports + 1 >= ? THEN 1 ELSE hidden END WHERE id = ?`).bind(HIDE_AFTER, id).run();
  }
  return json({ ok: true });
}

async function remove(request, env, id) {
  const doc = await body(request);
  const ids = await who(request, env, doc);
  const row = await env.DB.prepare("SELECT device, delete_key FROM recipes WHERE id = ?").bind(id).first();
  if (!row) return fail("That recipe isn't there.", 404, "missing");
  if (row.device !== ids.device || row.delete_key !== await sha(String(doc.key || ""))) {
    return fail("Only the person who shared a recipe can take it down.", 403, "forbidden");
  }
  await env.DB.batch([
    env.DB.prepare("DELETE FROM recipes WHERE id = ?").bind(id),
    env.DB.prepare("DELETE FROM saves WHERE recipe = ?").bind(id),
    env.DB.prepare("DELETE FROM reports WHERE recipe = ?").bind(id),
  ]);
  return json({ ok: true });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.replace(/\/+$/, "").split("/").filter(Boolean);
    try {
      if (parts[0] !== "v1") return fail("Not found.", 404, "missing");
      if (parts[1] === "health") return json({ ok: true });
      if (parts[1] !== "recipes") return fail("Not found.", 404, "missing");
      const id = parts[2];
      if (id && !/^[a-z2-9]{6,24}$/.test(id)) return fail("That recipe isn't there.", 404, "missing");
      if (request.method === "GET" && !id) return await list(env, url);
      if (request.method === "GET" && id && !parts[3]) {
        const row = await env.DB.prepare("SELECT * FROM recipes WHERE id = ? AND hidden = 0").bind(id).first();
        return row ? json({ recipe: full(row) }) : fail("That recipe isn't there any more.", 404, "missing");
      }
      if (request.method === "POST" && !id) return await share(request, env);
      if (request.method === "POST" && parts[3] === "save") return await save(request, env, id);
      if (request.method === "POST" && parts[3] === "report") return await report(request, env, id);
      if (request.method === "DELETE" && id && !parts[3]) return await remove(request, env, id);
      return fail("Not found.", 404, "missing");
    } catch (ex) {
      if (ex instanceof Rejected) return fail(ex.message, ex.code === "unavailable" ? 503 : 422, ex.code);
      console.error(ex);
      return fail("Something went wrong on the community server.", 500, "error");
    }
  },
};
