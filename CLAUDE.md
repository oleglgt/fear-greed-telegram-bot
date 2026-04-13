# Project rules

## Versioning

Always bump `BOT_VERSION` in `bot.py` before every commit and push. Use semantic
versioning `vMAJOR.MINOR.PATCH`:

- **PATCH** (`v2.2.0` → `v2.2.1`): small changes — bug fixes, copy tweaks,
  tiny refactors, config adjustments, cleanup.
- **MINOR** (`v2.2.0` → `v2.3.0`): medium changes — new sources, new data
  fields, behavior changes, notable refactors that touch multiple areas.
- **MAJOR** (`v2.2.0` → `v3.0.0`): new global features — new commands, new
  top-level functionality (new news category system, new integration,
  scheduler redesign, etc.).

Apply the bump in the same commit as the code change. When in doubt between
two levels, pick the higher one.
