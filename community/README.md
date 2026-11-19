# Simmer community server

A small, free, text-only recipe exchange for the Simmer app, on Cloudflare's free tier:

- **Workers** runs the API (`src/index.mjs`).
- **D1** (SQLite) stores shared recipes (`schema.sql`).
- **Workers AI** runs [Llama Guard 3](https://developers.cloudflare.com/workers-ai/models/llama-guard-3-8b/), the safety model.

Free-tier limits at the time of writing: 100,000 requests a day, 5 GB in D1, and 10,000 AI "neurons" a day. That's
roughly a few thousand safety checks a day. When the AI quota runs out, sharing pauses with a clear message until the
next day. Browsing keeps working. Nothing here costs money unless you choose a paid plan.

## What keeps it safe

Every shared recipe goes through `moderate()` (`src/moderate.mjs`) **before** anyone else can see it:

1. **Shape and size.** Only known fields are kept: name, ingredients, steps, notes, tags, times, servings and an
   optional display name. Each has a length limit, with at most 60 ingredients and 60 steps.
2. **No links, emails or phone numbers.** They're removed from all text, and so is HTML. There are no pictures at all.
3. **A word filter** for slurs, explicit words and the usual spam. It sees through letter swaps like `v1agra`, and
   cooking words like "retard the dough" or "cock-a-leekie" are tested so they stay allowed.
4. **"Is this a recipe?"** Most ingredients have to look like food or amounts, and the steps have to talk about
   cooking. The community stays about food rather than becoming a message board.
5. **Llama Guard 3** judges the whole text against its 14 hazard categories (hate, harassment, sexual content,
   violence, self-harm, dangerous activities and so on). If it says unsafe, the recipe isn't published. If the
   check can't run (quota, outage), nothing is published either: it **fails closed**.

After a recipe is published:

- **Reports.** Anyone can report a recipe with a reason. Three reports from three different devices hide it at once.
- **Rate limits.** Each device and each IP gets 5 shares, 20 reports and 200 saves a day. Only salted hashes are
  stored, never raw IPs or device IDs, and yesterday's counters are deleted.
- **Taking a recipe down.** The person who shared a recipe can delete it. Their device holds a secret delete key,
  and the server stores only its hash.
- **No accounts and no emails.** A display name is optional.

The app adds its own rule: recipes **imported from a website can't be shared**, because they belong to their
author. Only recipes written in Simmer can be shared.

### Reviewing by hand

```sh
# what's been hidden by reports
npx wrangler d1 execute simmer-community --remote --command "SELECT id, title, reports FROM recipes WHERE hidden = 1"
# put one back after a mistaken report pile-on
npx wrangler d1 execute simmer-community --remote --command "UPDATE recipes SET hidden = 0, reports = 0 WHERE id = '...'"
# remove one for good
npx wrangler d1 execute simmer-community --remote --command "DELETE FROM recipes WHERE id = '...'"
```

## Deploy (once)

You need a Cloudflare account. The free plan is enough.

```sh
bash community/deploy.sh
```

The script:

1. Signs you in to Cloudflare if you aren't already.
2. Creates the `simmer-community` D1 database and writes its ID into `wrangler.toml`.
3. Applies `schema.sql`.
4. Sets a random `HASH_SALT` secret.
5. Deploys the Worker.

The app expects the server at `https://simmer-community.<your-workers-subdomain>.workers.dev`. If your subdomain
differs from the one in `src/simmer/community.py` (`BASE_URL`), change it there, or set `SIMMER_COMMUNITY_URL` when
testing.

## API

| Method | Path | Body | Does |
| --- | --- | --- | --- |
| GET | `/v1/recipes?q=&tag=&sort=new\|popular&before=` | | List (30 a page) |
| GET | `/v1/recipes/:id` | | One recipe |
| POST | `/v1/recipes` | `{device, recipe}` | Share (moderated) → `{id, key}` |
| POST | `/v1/recipes/:id/save` | `{device}` | Count a save |
| POST | `/v1/recipes/:id/report` | `{device, reason}` | Report |
| DELETE | `/v1/recipes/:id` | `{device, key}` | Take it down |
| GET | `/v1/health` | | `{ok: true}` |

Errors are `{error, code}`, where `error` is a sentence the app shows as it is.

## Tests

```sh
cd community && npm test
```

These run the moderation rules and the whole Worker against a local SQLite database that answers like D1, with a
fake AI check. They cover sharing, listing, search, saves, reports hiding a recipe, rate limits, duplicates,
deleting, and failing closed.
