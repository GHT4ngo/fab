# AGENTS.md

Instructions for coding agents (Codex, Claude Code, …) working in this repo.

- **`CLAUDE.md` is the source of truth**: architecture, data sources, price-matching
  tiers, conventions, run/verify commands and gotchas. Read it before changing anything.
- `README.md` is the public overview and local setup guide.
- `docs/DEVLOG.md` is the history of decisions, including approaches that were tried and
  abandoned, with the reasons. Add an entry (newest on top) for any non-trivial change.

Ground rules (details in `CLAUDE.md`):
- `setup_db.py` is the single source of schema; dbt models hold all matching logic.
- `price_sek` is Cardmarket EUR first, tcgcsv USD as fallback. Don't flip it.
- Pitch colour is display-only, never a match key.
- Never auto-pick between look-alike printings whose prices differ.
- `retro-data-display/` is a separate git repo; stage only the files you meant to change.
- Run `dbt test` after any pipeline change; all tests must pass.
