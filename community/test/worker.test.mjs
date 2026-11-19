// The whole Worker against a local SQLite database that answers like D1, with a fake AI check.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import worker from "../src/index.mjs";

function fakeD1() {
  const db = new DatabaseSync(":memory:");
  db.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  const prep = (sql) => {
    let args = [];
    const s = {
      bind: (...a) => { args = a; return s; },
      first: async () => db.prepare(sql).get(...args) ?? null,
      all: async () => ({ results: db.prepare(sql).all(...args) }),
      run: async () => { const r = db.prepare(sql).run(...args); return { meta: { changes: r.changes } }; },
    };
    return s;
  };
  return { prepare: prep, batch: async (stmts) => Promise.all(stmts.map((s) => s.run())), raw: db };
}

function env(verdict = "safe") {
  return { DB: fakeD1(), HASH_SALT: "t", AI: { run: async () => ({ response: verdict }) } };
}

const recipe = {
  title: "Lemon Garlic Pasta", author: "Alex", servings: 2, total: 20, tags: ["pasta"],
  ingredients: ["200 g spaghetti", "2 tbsp butter", "2 cloves garlic", "1 lemon", "salt"],
  steps: ["Boil the pasta for 9 minutes.", "Melt the butter with the garlic.", "Toss the pasta with lemon and serve."],
};
const DEV = (n) => `device-0000000000-${n}`;
const call = (e, method, path, doc, ip = "1.1.1.1") => worker.fetch(new Request(`https://x${path}`, {
  method, headers: { "Content-Type": "application/json", "CF-Connecting-IP": ip },
  body: doc ? JSON.stringify(doc) : undefined,
}), e);

test("share, list, read, save, delete", async () => {
  const e = env();
  let res = await call(e, "POST", "/v1/recipes", { device: DEV(1), recipe });
  assert.equal(res.status, 201);
  const { id, key } = await res.json();
  res = await call(e, "GET", "/v1/recipes");
  const list = await res.json();
  assert.equal(list.recipes.length, 1);
  assert.equal(list.recipes[0].ingredients, 5);
  res = await call(e, "GET", `/v1/recipes/${id}`);
  const got = (await res.json()).recipe;
  assert.equal(got.steps.length, 3);
  assert.equal(got.device, undefined, "device hash never leaves the server");
  assert.equal(got.delete_key, undefined);
  await call(e, "POST", `/v1/recipes/${id}/save`, { device: DEV(2) });
  await call(e, "POST", `/v1/recipes/${id}/save`, { device: DEV(2) });
  assert.equal((await (await call(e, "GET", `/v1/recipes/${id}`)).json()).recipe.saves, 1);
  res = await call(e, "GET", "/v1/recipes?q=lemon&sort=popular");
  assert.equal((await res.json()).recipes.length, 1);
  res = await call(e, "GET", "/v1/recipes?q=chocolate");
  assert.equal((await res.json()).recipes.length, 0);
  res = await call(e, "DELETE", `/v1/recipes/${id}`, { device: DEV(2), key });
  assert.equal(res.status, 403, "someone else can't delete it");
  res = await call(e, "DELETE", `/v1/recipes/${id}`, { device: DEV(1), key });
  assert.equal(res.status, 200);
  assert.equal((await call(e, "GET", `/v1/recipes/${id}`)).status, 404);
});

test("unsafe recipes are refused and stored nowhere", async () => {
  const e = env("unsafe\nS1");
  const res = await call(e, "POST", "/v1/recipes", { device: DEV(1), recipe });
  assert.equal(res.status, 422);
  assert.equal((await res.json()).code, "unsafe");
  assert.equal(e.DB.raw.prepare("SELECT COUNT(*) AS n FROM recipes").get().n, 0);
});

test("AI unavailable: nothing published (503)", async () => {
  const e = env();
  e.AI.run = async () => { throw new Error("quota"); };
  const res = await call(e, "POST", "/v1/recipes", { device: DEV(1), recipe });
  assert.equal(res.status, 503);
  assert.equal(e.DB.raw.prepare("SELECT COUNT(*) AS n FROM recipes").get().n, 0);
});

test("three reports from different devices hide a recipe; one device counts once", async () => {
  const e = env();
  const { id } = await (await call(e, "POST", "/v1/recipes", { device: DEV(1), recipe })).json();
  for (let i = 0; i < 5; i++) await call(e, "POST", `/v1/recipes/${id}/report`, { device: DEV(9), reason: "spam" });
  assert.equal((await call(e, "GET", `/v1/recipes/${id}`)).status, 200);
  await call(e, "POST", `/v1/recipes/${id}/report`, { device: DEV(10), reason: "spam" }, "2.2.2.2");
  await call(e, "POST", `/v1/recipes/${id}/report`, { device: DEV(11), reason: "spam" }, "3.3.3.3");
  assert.equal((await call(e, "GET", `/v1/recipes/${id}`)).status, 404);
  assert.equal((await (await call(e, "GET", "/v1/recipes")).json()).recipes.length, 0);
});

test("rate limits per device and per IP, duplicates, bad input", async () => {
  const e = env();
  e.DB.raw.prepare("INSERT INTO limits (day, kind, who, n) VALUES ('2000-01-01', 'share', 'old', 3)").run();
  for (let i = 0; i < 5; i++) {
    const r = await call(e, "POST", "/v1/recipes", { device: DEV(1), recipe: { ...recipe, title: `Pasta ${i}` } });
    assert.equal(r.status, 201);
  }
  assert.equal((await call(e, "POST", "/v1/recipes", { device: DEV(1), recipe: { ...recipe, title: "Pasta 6" } })).status, 429);
  // a new device id from the same IP is limited too
  assert.equal((await call(e, "POST", "/v1/recipes", { device: DEV(2), recipe: { ...recipe, title: "Pasta 7" } })).status, 429);
  assert.equal(e.DB.raw.prepare("SELECT COUNT(*) AS n FROM limits WHERE day = '2000-01-01'").get().n, 0, "old counters deleted");
  const e2 = env();
  await call(e2, "POST", "/v1/recipes", { device: DEV(3), recipe });
  assert.equal((await call(e2, "POST", "/v1/recipes", { device: DEV(3), recipe })).status, 409);
  assert.equal((await call(e2, "POST", "/v1/recipes", { device: "x", recipe })).status, 422);
  assert.equal((await call(e2, "GET", "/v1/recipes/../../etc")).status, 404);
  assert.equal((await call(e2, "GET", "/nope")).status, 404);
});
