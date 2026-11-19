# Submitting Simmer to Depot

A person submits, not an agent: these are the steps for the publisher (Hartmann Studios). Only Charles sends it for review.

## Packages

Depot takes one package per architecture, built with the official Flatpak builder from the commit the manifest
pins, and installed and tested on each architecture:

- `com.charleshartmann.Simmer-1.0.0-x86_64.flatpak` for computers and TVs
- `com.charleshartmann.Simmer-1.0.0-aarch64.flatpak` for phones, tablets and ARM computers

Both are attached to the GitHub release [v1.0.0](https://github.com/chartmann1590/simmer/releases/tag/v1.0.0),
with `SHA256SUMS`.

> The bundles on the GitHub release are **GitHub builds** (built from `flatpak/github/`), which offer updates from
> GitHub. For Depot, upload bundles built from `flatpak/com.charleshartmann.Simmer.json`, which update through Depot.

## Steps

1. **New app** `com.charleshartmann.Simmer`, name `Simmer`, in the `com.charleshartmann` namespace (the same as
   Hartmann Office and Earshot).
2. **Packages:** both Depot-channel bundles, version `1.0.0`.
3. **Listing:** from `data/com.charleshartmann.Simmer.metainfo.xml`.
   - The description says the app is free, with an optional Simmer Plus purchase.
   - Privacy policy: `PRIVACY.md`.
   - In-app purchases: yes. Sign-in: `none`. TV: supported.
   - User-generated content: yes (moderated community recipes).
4. **Permissions** Depot will show:
   - Uses the internet: imports, the community, purchases and update checks.
   - Plays audio: the timer bell. Luma's list shows this as "Records audio" because `--socket=pulseaudio` is the
     only way a Flatpak plays sound. No microphone is used.
5. **Screenshots:** `screenshots/` (computer, tablet, phone and TV).
6. **Review.** Only Charles sends it for review.

## Review notes (for the reviewer)

- **Timer bell and `--socket=pulseaudio`.** A kitchen timer has to be heard across the room, so Simmer plays a bell
  through GStreamer. Nothing records.
- **Keeping the screen on.** Cook mode asks the Inhibit portal to keep the screen awake (idle only), and only while
  cook mode is open. It can be turned off in Settings.
- **The community (user-generated content, 1.x and 2.x).** Shared recipes are text only, and the server removes
  links, emails and phone numbers. Each recipe is checked before it's published: a word filter, a check that it
  really is a recipe, and Llama Guard 3 on Cloudflare Workers AI. If the check is unavailable, nothing is
  published. People can report a recipe, and three reports from different devices hide it. People can delete
  what they shared. Shares are rate-limited per device and per IP. There are no accounts, no emails and no
  pictures. Server code and tests: `community/`.
- **Imported recipes can't be shared**, so other sites' content is never republished.
- **Purchases (3.4 and 3.6).**
  - Prices and trials come from the license server, never hard-coded.
  - Nothing is pre-selected or counted down.
  - Cancelling is one tap to Stripe's own page.
  - Plus features show as locked with a plain explanation, and nothing free became paid.
- **Updates (1.3).** The Depot build updates through Depot. The app shows release notes from GitHub but never
  downloads anything there.

## Billing (Simmer Plus)

Simmer uses the shared Hartmann Studios license server (`livecaptionn-premium`, `/v2` API) and Stripe Checkout,
the same system as Earshot and Hartmann Office. There is no Stripe key in the app.

Product keys, and the proposed prices (the same as Earshot Plus, pending Charles's approval):

| Product key | Label | Stripe mode | Price |
| --- | --- | --- | --- |
| `simmer_plus:monthly` | Simmer Plus (Monthly) | subscription | $1.99 / month |
| `simmer_plus:yearly` | Simmer Plus (Yearly) | subscription | $14.99 / year, 7-day free trial |
| `simmer_lifetime` | Simmer Plus (Lifetime) | payment | $29.99 once |

**Status:** registration is still needed. `/v2/catalog?app=simmer` answers `404 Unknown app` until the studio's
billing maintainer registers it. Until then, the upgrade panel says "Purchases aren't open for this app yet" and
nothing breaks. The registration request is in `TAB-SIMMER-STRIPE-20261119.md`, kept beside the repos rather
than in them.

## Community server

`community/` holds a Cloudflare Worker that runs on the free tier with D1 and Workers AI. Deploy it once with
`bash community/deploy.sh` (see `community/README.md`). Until it's deployed, the Community page says it isn't
reachable, and the rest of the app works.

## Listing text (also in the metainfo)

- **Name:** Simmer
- **Summary:** Your recipes, a hands-free cook mode and a shopping list
- **Developer:** Hartmann Studios
- **Category:** Utilities
- **Licence:** GPL-3.0-or-later
- **Content rating:** OARS 1.1, moderated user content (`social-chat: mild`), in-app purchases (`money-purchasing: mild`)
