# Privacy policy

Simmer is a recipe app with no account, no analytics and no advertising.

## What stays on your device

Your recipes, favorites, tags, meal plan, shopping list, cooking log, settings and the pictures of your recipes are
kept in the app's own folder on your device. Removing the app with its data removes them.

## Who Simmer talks to

- **Recipe sites you import from.** When you import a link, Simmer downloads that page and the recipe's picture
  from the site. The site sees your IP address and that the request came from Simmer.
- **The Simmer community, run by Hartmann Studios, only when you open Community, share, save or report.**
  - Browsing sends your search words. Nothing about your own recipes is sent.
  - **Sharing** sends the recipe you chose: its name, ingredients, steps, notes, tags, times and servings, and the
    display name you typed (optional). Pictures and links are never sent; links typed into the text are removed by
    the server. The recipe is public to every Simmer user until you take it down.
  - Every shared recipe is checked automatically before it's published: a word filter, a check that it's really a
    recipe, and Llama Guard, an AI safety model run by Cloudflare Workers AI. The text goes to Cloudflare for that
    check and isn't used to train anything.
  - Simmer makes a random device ID when you first use the community. The server stores only a salted hash of it,
    and a salted hash of your IP address, to stop spam (5 shares and 20 reports a day), to let you take your own
    recipes down, and to count each save and report once. Daily counters are deleted after the day.
  - **Reporting** a recipe sends the reason you chose. A recipe that three different people report is hidden.
- **Simmer Plus, only if you buy or restore it.** The email you type goes to the Hartmann Studios license server and
  to Stripe, which runs the checkout in your browser and sends your receipt. The license server keeps your email
  and what you bought so you can restore it on another device; Stripe keeps your payment details under
  [its own privacy policy](https://stripe.com/privacy). Simmer itself never sees your card.
- **GitHub, to check for updates.** About twice a day at most, Simmer asks GitHub for its latest release so it can
  tell you about a new version. Turn automatic checks off in Settings or the update panel.

Nothing else. Simmer sends no usage data, crash reports or tracking identifiers anywhere.

## Your files

Simmer reads a backup only when you pick one to restore, and writes one only where you choose to save it.

## Taking a shared recipe down

Open it in Community and choose **Take it down**, or open Settings › Recipes you've shared. It's deleted from the
server at once, with its saves and reports. To have a recipe removed that you shared from a device you no longer
have, [open an issue](https://github.com/chartmann1590/simmer/issues) with its name.

## Contact

Questions: [open an issue](https://github.com/chartmann1590/simmer/issues).
