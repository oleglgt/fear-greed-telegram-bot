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

## Pull request workflow

Single-user project — pre-merge review is not required. When a PR for this
repository has been opened and CI (if any) passes, **merge it immediately
with `squash` method**; do not ask the user to confirm each merge. The rest
of the workflow stays the same:

- Create a feature branch from `main`.
- Bump `BOT_VERSION` and commit.
- Push, open the PR.
- Squash-merge it right away without waiting for a `merge` command.
- If subscribed to PR activity, unsubscribe after merging (automatic on
  merge events).

Do not squash-merge if there are open questions that need the user's
answer, or if the change is large/risky enough that the user asked to
review it explicitly. Those are exceptions — default is merge-on-open.
