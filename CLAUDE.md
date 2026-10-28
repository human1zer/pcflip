# pcflip

Personal buy/sell tracker for computers and parts flipped on FINN.no.
Single user, self-hosted on a home server, reachable via a Cloudflare
tunnel. Full feature list: README.md.

## Stack

FastAPI + Jinja2 + raw `sqlite3` (no ORM, hand-written schema/queries) +
Tailwind CSS classes in server-rendered templates (Tailwind loaded via
CDN, no build step). Deployed with Docker Compose.

## File layout

- `app/main.py` — all routes: items list (`/`, with the dashboard stats on
  top; `/items` is an alias), items CRUD, inline quick-edit
  (`POST /items/{id}/quick`, JSON), consume/undo (installing parts into a
  PC), batch-purchase creation, photo/attachment upload+delete, FINN
  import, CSV export, login/logout.
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
- `app/templates/` — plain HTML + Tailwind classes. Still no JS build
  step: the little JS there is lives in inline `<script>` tags (search +
  shortcuts in `base.html`, inline editor in `items_list.html`, parts
  filter in `item_detail.html`). `_item_row.html` / `_stats.html` are
  partials rendered both by the list page and by `/quick`.

## UI behaviour

- **Search** (header, every page): on `/` it filters rows in place as you
  type; elsewhere Enter goes to `/?q=`. Every word must appear somewhere
  in `SEARCH_FIELDS` (title, serial, category, notes, seller, buyer, FINN
  IDs) or the status/label.
- **Category chips** on `/`: one per category present in the current status
  tab (+ "Uncategorized" for no category), with live counts of
  search-matching rows. Clicking switches in place (no reload) and keeps
  `?category=` in the URL, the status tabs and the sort form. Works
  together with the status tab and the search.
- **Parts panel** on Desktop PC / Laptop pages (`PC_CATEGORIES`): lists
  in-stock/listed non-PC items; "+ Add" posts to the existing
  `/items/{part}/consume`, "Remove" to `/consume/undo`, both with
  `next=` to come back to the PC. The mirror image on a part's page
  ("Install this part in a PC", in-stock/listed parts only) lists
  in-stock/listed PCs/laptops and redirects back to the part. Both lists
  share one `[data-picker]` script in `item_detail.html`, and
  `/consume` itself refuses any target that isn't a PC/laptop (and any
  PC/laptop as the source).
- **Inline edit** on the list: status, purchase price, sale price/date,
  notes. Mouse-picking a non-sold status saves immediately; "Sold" asks
  for price/date first (date defaults to today).
- **Shortcuts:** `/` focus search, `n` new item (both ignored while typing
  in a field), Enter saves / opens the single search match, Esc
  cancels/clears. Only deletes ask for `confirm()`; undo/remove don't.

## Rebuild and logs

Code is baked into the image at build time — only `./data` is
bind-mounted. A template or Python change needs a rebuild, **a restart is
not enough**:

```bash
docker compose up -d --build
docker compose logs -f
```

## Tests

`tests/` (pytest + FastAPI TestClient) builds its own temp DB and auth
config and never touches `./data`. The host has no venv, so run them in
the app image with the working tree mounted:

```bash
docker run --rm -w /app -v "$PWD/app:/app/app:ro" -v "$PWD/tests:/app/tests:ro" pcflip-pcflip \
  sh -c "pip -q install httpx==0.27.2 pytest && python -m pytest -q -p no:cacheprovider tests"
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

- Don't send a partial form to `POST /items/{id}/edit`: `_parse_item_form`
  rewrites every column in `FORM_FIELDS` and sets missing ones to NULL.
  For single-field changes use `/quick`, which only touches
  `QUICK_FIELDS` that are actually sent.
- The search + category rule exists twice — `items_list()` in `main.py`
  and `filter()` in `base.html` — keep them in sync. The server renders
  non-matching rows `hidden` rather than dropping them (category too, not
  a SQL `WHERE`), so the in-page filter can widen the search again; rows
  use `[data-search]` / `data-category` / `data-href`.
- `[hidden]` needs the `display: none !important` rule in `base.html`;
  Tailwind's `.block`/`.flex` would otherwise win and show hidden rows.
- `consumed` status is only set/cleared by the consume/undo routes (they
  keep the target's cost row in sync). `/quick` and the edit form both
  refuse to change it.
- Any redirect target taken from a form must go through `_safe_next()`
  (local paths only — no `//host` or `/\host`).
- JS `fetch` POSTs still need CSRF: send `csrf_token` in the form body
  (the list exposes it as `data-csrf` on `#items-list`). An expired
  session comes back as a redirect to `/login` (HTML, not JSON).

## Data safety

`data/pcflip.db` is not in git. Snapshot it before running any migration:

```bash
cp data/pcflip.db data/pcflip.db.bak
```
