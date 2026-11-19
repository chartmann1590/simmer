// node --test test/
import test from "node:test";
import assert from "node:assert/strict";
import { moderate, shape, blockedWord, looksLikeRecipe, readGuard, tidy, Rejected } from "../src/moderate.mjs";

const good = () => ({
  title: "Weeknight Tomato Soup", author: "Sam",
  ingredients: ["2 tbsp olive oil", "1 onion, chopped", "2 cloves garlic", "800 g canned tomatoes", "500 ml stock", "salt"],
  steps: ["Heat the oil in a pot and cook the onion for 8 minutes.", "Add the garlic, then the tomatoes and stock.",
          "Simmer for 20 minutes, then blend until smooth and season."],
  notes: "Great with grilled cheese.", tags: ["Soup", "Vegetarian"], servings: 4, total: 35,
});
const safe = async () => ({ response: "safe" });

test("a normal recipe is published, tidied", async () => {
  const r = await moderate(good(), safe);
  assert.equal(r.title, "Weeknight Tomato Soup");
  assert.deepEqual(r.tags, ["soup", "vegetarian"]);
  assert.equal(r.ingredients.length, 6);
});

test("links, emails and phone numbers are taken out", () => {
  assert.equal(tidy("Visit https://spam.example/x now", 200), "Visit now");
  assert.equal(tidy("mail me at a.b@c.com", 200), "mail me at");
  assert.equal(tidy("call +1 (555) 123-4567 today", 200), "call today");
  assert.equal(tidy("see example.ru for more", 200), "see for more");
  assert.equal(tidy("<script>x</script>bold", 200), "xbold");
});

test("sizes and shape are enforced", () => {
  assert.throws(() => shape({ ...good(), title: "x" }), Rejected);
  assert.throws(() => shape({ ...good(), ingredients: [] }), Rejected);
  assert.throws(() => shape({ ...good(), steps: Array(61).fill("Stir it.") }), Rejected);
  assert.throws(() => shape(null), Rejected);
  const r = shape({ ...good(), servings: 9999, tags: Array(20).fill(0).map((_, i) => `t${i}`) });
  assert.equal(r.servings, 100);
  assert.equal(r.tags.length, 6);
});

test("the word filter catches the obvious, even disguised, but not cooking words", () => {
  assert.ok(blockedWord("buy v1agra here"));
  assert.ok(blockedWord("p o r n h u b"));
  assert.ok(blockedWord("free b1tc0in"));
  for (const ok of ["Retard the dough overnight in the fridge", "Cock-a-leekie soup", "Spicy analysis of sauces",
                    "Light the gas, the pan should be hot", "Add the raccoon-shaped cookies", "Cocktail sauce",
                    "Scunthorpe sausages", "Heroine of the dinner party", "a dash of grated nutmeg"]) {
    assert.equal(blockedWord(ok), "", ok);
  }
});

test("it has to be a recipe", () => {
  assert.ok(looksLikeRecipe(shape(good())));
  assert.equal(looksLikeRecipe(shape({ ...good(), ingredients: ["my thoughts", "on politics", "today"],
    steps: ["I think that people should vote.", "Share this message."] })), false);
});

test("Llama Guard's answers are read correctly", () => {
  assert.deepEqual(readGuard({ response: "safe" }), { safe: true, categories: [] });
  assert.deepEqual(readGuard({ response: "unsafe\nS1,S10" }), { safe: false, categories: ["S1", "S10"] });
  assert.deepEqual(readGuard({ response: { safe: false, categories: ["S2"] } }).safe, null);
  assert.deepEqual(readGuard({ safe: false, categories: ["S2"] }), { safe: false, categories: ["S2"] });
  assert.equal(readGuard("???").safe, null);
});

test("unsafe, unreadable or unavailable checks never publish", async () => {
  await assert.rejects(moderate(good(), async () => ({ response: "unsafe\nS9" })), { code: "unsafe" });
  await assert.rejects(moderate(good(), async () => ({ response: "maybe" })), { code: "unavailable" });
  await assert.rejects(moderate(good(), async () => { throw new Error("quota"); }), { code: "unavailable" });
  await assert.rejects(moderate({ ...good(), notes: "dm me on telegram" }, safe), { code: "blocked" });
});
