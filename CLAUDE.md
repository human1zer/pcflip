# pcflip

Personal buy/sell tracker for computers and parts flipped on FINN.no.
Single user, self-hosted on a home server, reachable via a Cloudflare
tunnel. Full feature list: README.md.

## Stack

FastAPI + Jinja2 + raw `sqlite3` (no ORM, hand-written schema/queries) +
Tailwind CSS classes in server-rendered templates (Tailwind loaded via
CDN, no build step). Deployed with Docker Compose.

## File layout

- `app/main.py` — all routes: dashboard, items CRUD, batch-purchase
  creation, photo/attachment upload+delete, FINN import, CSV export,
  login/logout.
- `app/auth.py` — session auth: bcrypt passwords, itsdangerous signed
  cookies, per-IP brute-force lockout, CSRF tokens, `AuthMiddleware`
  gating everything except `/login`, `/health`, `/static`.
- `app/database.py` — schema + migrations (see Gotchas) + `PHOTOS_DIR` /
  `ATTACHMENTS_DIR` paths.
- `app/finn_import.py` — scrapes a FINN.no listing to prefill the new-item
  form (JSON-LD + description parsing, condition/category mapping,
  on-disk cache by finnkode).
- `app/config.py` — branding/colors, reads `data/auth_config.json`.
- `app/constants.py` — dropdown values (categories, conditions, etc).
- `app/templates/` — plain HTML + Tailwind classes, no JS build step.

## Rebuild and logs

Code is baked into the image at build time — only `./data` is
bind-mounted. A template or Python change needs a rebuild, **a restart is
not enough**:

```bash
docker compose up -d --build
docker compose logs -f
```

## Conventions

- Never bypass bcrypt password hashing or CSRF checks on a state-changing
  POST, even for a "just testing" change.
- Credentials live in `data/auth_config.json`, deliberately not in
  `docker-compose.yml` or `.env` — Compose treats `$` as a variable
  substitution anywhere it parses config, which silently corrupts a
  bcrypt hash like `$2b$12$...`. Don't move credentials back into Compose
  config to "simplify" it.

## Gotchas

- `init_db()` runs `SCHEMA` then `_migrate()`. `CREATE TABLE IF NOT
  EXISTS` is a no-op on an existing DB, so anything referencing a new
  column must live in `_migrate()`, never in `SCHEMA` — otherwise it only
  works on a fresh install and throws `no such column` against a real,
  already-created database.
- `item_form.html` must keep an explicit `action` attribute on the
  `<form>`. Without one the form posts to the current URL, which breaks
  after any in-place re-render from a different route (e.g. the
  FINN-import endpoint re-rendering the new-item form at its own URL) —
  the next submit silently goes wherever the page happens to be instead
  of the create/update route.
- FINN wraps its page in `<template shadowrootmode>`, so BeautifulSoup's
  `.get_text()` / `.strings` / `.text` silently return `''` for
  everything inside it. Use `tag.find_all(string=True, recursive=True)`
  instead when parsing a FINN listing page.

## Data safety

`data/pcflip.db` is not in git. Snapshot it before running any migration:

```bash
cp data/pcflip.db data/pcflip.db.bak
```
