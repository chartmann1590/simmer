// SPDX-License-Identifier: GPL-3.0-or-later
// Every shared recipe passes these checks before anyone else can see it. Pure functions (no Cloudflare APIs),
// so they run under `node --test`. The AI check is passed in, so tests use a fake.
//
// Order: shape and size → links, emails and phone numbers taken out → a word filter → "is this actually a
// recipe?" → Llama Guard (Cloudflare Workers AI, free tier). Anything that fails is rejected with a reason the
// app can show. If the AI check can't run, the recipe is NOT published (fail closed).

import { BLOCKED } from "./blocklist.mjs";

export const LIMITS = {
  title: [3, 120], author: [0, 40], ingredient: 200, ingredients: [1, 60], step: 1000, steps: [1, 60],
  notes: 2000, tags: 6, tag: 24, servings: 100, minutes: 10000,
};

export class Rejected extends Error {
  constructor(message, code = "rejected") {
    super(message);
    this.code = code;
  }
}

const URL_RE = /\b(?:https?:\/\/|www\.)\S+|\b[a-z0-9-]+\.(?:com|net|org|io|co|ru|xyz|top|info|biz|link|click|shop|app|me|tk|ly)\b(?:\/\S*)?/gi;
const EMAIL_RE = /\b[\w.+-]+@[\w-]+\.[\w.-]+\b/g;
const PHONE_RE = /\+?(?:\d[\s().-]{0,2}){9,}\d/g;
const CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f​-‏‪-‮⁦-⁩]/g;

export function tidy(text, max) {
  let t = String(text ?? "").replace(CONTROL_RE, "").replace(/<[^>]*>/g, "");
  t = t.replace(EMAIL_RE, "").replace(URL_RE, "").replace(PHONE_RE, "");
  t = t.split("\n").map((l) => l.replace(/\s+/g, " ").trim()).filter(Boolean).join("\n");
  return t.slice(0, max);
}

function int(v, max) {
  const n = Math.floor(Number(v) || 0);
  return Math.max(0, Math.min(max, n));
}

/** The recipe as it will be stored: only known fields, trimmed, links taken out. Throws Rejected. */
export function shape(input) {
  if (!input || typeof input !== "object") throw new Rejected("Nothing to share.", "invalid");
  const L = LIMITS;
  const list = (v, each, [min, max], what) => {
    const items = (Array.isArray(v) ? v : []).map((x) => tidy(x, each)).filter(Boolean);
    if (items.length < min) throw new Rejected(`Add at least ${min} ${what}.`, "invalid");
    if (items.length > max) throw new Rejected(`Shared recipes can have up to ${max} ${what}.`, "invalid");
    return items;
  };
  const r = {
    title: tidy(input.title, L.title[1]).replace(/\n/g, " "),
    author: tidy(input.author, L.author[1]).replace(/\n/g, " "),
    servings: int(input.servings, L.servings),
    prep: int(input.prep, L.minutes),
    cook: int(input.cook, L.minutes),
    total: int(input.total, L.minutes),
    ingredients: list(input.ingredients, L.ingredient, L.ingredients, "ingredients"),
    steps: list(input.steps, L.step, L.steps, "steps"),
    notes: tidy(input.notes, L.notes),
    tags: [...new Set((Array.isArray(input.tags) ? input.tags : [])
      .map((t) => tidy(t, L.tag).toLowerCase().replace(/[^a-z0-9 &'-]/g, "").trim()).filter(Boolean))].slice(0, L.tags),
  };
  if (r.title.length < L.title[0]) throw new Rejected("Give the recipe a name.", "invalid");
  return r;
}

const LEET = { "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i" };

export function normalise(text) {
  return String(text).toLowerCase().replace(/[013457@$!]/g, (c) => LEET[c] ?? c)
    .normalize("NFKD").replace(/[̀-ͯ]/g, "").replace(/[^a-z\s]/g, " ").replace(/\s+/g, " ");
}

export function allText(r) {
  return [r.title, r.author, ...r.ingredients, ...r.steps, r.notes, ...r.tags].join("\n");
}

/** A blocked word or phrase, or "" when none. Matches whole words, and words with spaces or dots between letters. */
export function blockedWord(text) {
  const words = normalise(text);
  const squashed = words.replace(/\s+/g, "");
  for (const w of BLOCKED) {
    const re = new RegExp(`(^|\\s)${w.replace(/ /g, "\\s*")}(s|es|ed|ing)?(\\s|$)`);
    if (re.test(words)) return w;
    if (w.length >= 6 && !w.includes(" ") && squashed.includes(w) && !words.split(" ").some((x) => x.includes(w))) return w;
  }
  return "";
}

const FOOD = /\b(salt|pepper|oil|butter|flour|sugar|egg|eggs|milk|water|garlic|onion|onions|cheese|chicken|beef|pork|fish|rice|pasta|tomato|tomatoes|lemon|lime|cream|stock|broth|vinegar|honey|bread|potato|potatoes|carrot|carrots|beans|herbs|basil|parsley|cumin|paprika|yeast|chocolate|vanilla|cinnamon|ginger|soy|sauce|tofu|mushroom|mushrooms|spinach|apple|apples|banana|berries|nuts|oats|corn|pepper|chili|chilli|wine|bacon|sausage|shrimp|salmon|yogurt|mustard|lettuce|cucumber|avocado|cabbage|celery|leek|pea|peas|lentils|chickpeas|coconut|maple|syrup|baking|powder|soda|cocoa|parmesan|mozzarella|cheddar|thyme|rosemary|oregano|cilantro|coriander|turmeric|curry|noodles|tortillas|sesame|peanut|almond|walnut|zucchini|squash|pumpkin|kale|broccoli|cauliflower|shallot|scallion|scallions|jalapeno|fruit|vegetable|vegetables|meat|ham|turkey|lamb|steak|cod|tuna|feta|ricotta|ice|juice|tea|coffee)\b/i;
const AMOUNT = /^\s*(\d|½|¼|¾|⅓|⅔|⅛|a |an |one |two |three |pinch|handful|dash)/i;
const UNIT = /\b(cups?|tbsp|tsp|tablespoons?|teaspoons?|grams?|g|kg|ml|l|oz|ounces?|lbs?|pounds?|cloves?|cans?|pinch|slices?|sticks?)\b/i;
const COOK = /\b(add|bake|boil|simmer|stir|mix|whisk|chop|slice|dice|heat|cook|fry|roast|grill|serve|season|pour|combine|blend|knead|fold|preheat|oven|pan|pot|bowl|minutes?|until|drain|toss|cool|chill|sprinkle|spread|place|cover|reduce|saute|sauté|melt|beat|marinate|grate|peel|rinse|cut|transfer|garnish|set aside|refrigerate|freeze|steam|poach|toast|brush|layer|top)\b/i;

/** True when it reads like a recipe: most ingredients look like food or amounts, and the steps talk about cooking. */
export function looksLikeRecipe(r) {
  const ingOk = r.ingredients.filter((i) => FOOD.test(i) || AMOUNT.test(i) || UNIT.test(i)).length;
  const stepOk = r.steps.filter((s) => COOK.test(s)).length;
  return ingOk >= Math.max(1, Math.ceil(r.ingredients.length * 0.6)) && stepOk >= Math.max(1, Math.ceil(r.steps.length * 0.4));
}

/** Llama Guard's answer → {safe, categories}. It answers "safe", or "unsafe" and the categories (S1…S14). */
export function readGuard(answer) {
  const text = typeof answer === "string" ? answer
    : answer?.response ?? answer?.result?.response ?? (answer?.safe !== undefined ? (answer.safe ? "safe" : `unsafe\n${(answer.categories || []).join(",")}`) : "");
  const t = String(text).trim().toLowerCase();
  if (t.startsWith("safe")) return { safe: true, categories: [] };
  if (t.startsWith("unsafe")) return { safe: false, categories: (t.match(/s\d+/g) || []).map((s) => s.toUpperCase()) };
  return { safe: null, categories: [] };
}

/**
 * The whole check. `guard(text)` asks Llama Guard and returns its answer (or throws).
 * Resolves to the stored shape of the recipe, or throws Rejected.
 */
export async function moderate(input, guard) {
  const r = shape(input);
  const text = allText(r);
  const word = blockedWord(text);
  if (word) throw new Rejected("Simmer's community keeps it about food. Something in this recipe isn't allowed.", "blocked");
  if (!looksLikeRecipe(r)) throw new Rejected("This doesn't look like a recipe yet. Shared recipes need ingredients and cooking steps.", "not_recipe");
  let verdict;
  try {
    verdict = readGuard(await guard(text));
  } catch {
    throw new Rejected("The safety check isn't available right now, so nothing was shared. Try again later.", "unavailable");
  }
  if (verdict.safe === null) throw new Rejected("The safety check couldn't decide, so nothing was shared. Try again later.", "unavailable");
  if (!verdict.safe) throw new Rejected("The automatic safety check didn't pass this recipe, so it wasn't shared.", "unsafe");
  return r;
}
