# Contributing

Thanks for helping make Simmer better.

## Reporting a problem

[Open an issue](https://github.com/chartmann1590/simmer/issues/new/choose) and say what you did and what happened.
If a recipe didn't import right, include the page's address.

## Sending a change

1. Fork the repository and make a branch.
2. Keep each pull request to one change.
3. Run the checks:
   ```sh
   python3 -m pytest tests/test_core.py tests/test_billing.py tests/test_promo.py tests/test_updates.py
   xvfb-run flatpak run --command=python3 org.projectluma.Sdk//44 tests/test_ui.py
   (cd community && npm test)
   luma lint && luma test
   ```
4. For anything you can see, try it at phone width (360 px), on a computer and in TV posture, in light and dark.
5. Say in the pull request what you tested.

## Style

- Use LumaUI parts (`luma_appkit`) before writing your own widget, and Luma's colour tokens, never fixed colours.
- Words in the interface are short and plain, in sentence case.
- Recipe pages and shared recipes come from strangers: treat everything in them as untrusted and bound every read.
- `ingredients.py`, `timers.py`, `importer.py`, `store.py` and `community.py` don't import GTK, so they can be tested
  anywhere. Keep it that way.
- The community's safety checks live in `community/src/moderate.mjs`. A change there needs a test in
  `community/test/`.

By contributing you agree your work is licensed under GPL-3.0-or-later.
