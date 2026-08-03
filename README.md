# PC Flip Tracker

Small self-hosted app to track computers/parts you buy and sell on FINN.no —
who you bought from, what you paid, serial number, how it was delivered,
who you sold it to, for how much, photos, and profit — all in one place.

## What it tracks per item

- **Item**: category, title, serial number, condition, status (in stock / listed / sold)
- **Purchase**: seller name + contact, price, date, payment method (Vipps, cash, bank
  transfer, FINN Trygg handel...), delivery (face to face / shipped), shipping company + tracking
- **Sale**: same fields but for the buyer
- **Photos**: upload as many as you want per item (multi-select on the item page).
  Stored as a resized full copy (max 1600px) + a thumbnail for fast loading —
  matters once you've got 10+ photos per item.
- **Receipts**: separate from photos — upload a PDF *or* a photo of a seller's
  receipt/proof of purchase. Multiple receipts per item supported.
- **Notes**: free text for anything else
- Automatic profit calculation per item and totals on the dashboard
- **Search**: search box on the Items page matches title, serial number, seller
  name, and buyer name at once — combine it with the status filter too
- CSV export of everything (useful for bookkeeping/tax purposes)

## Customizing the app name and colors

Edit `app/config.py` — everything is explained inline. You can change:
- `APP_NAME` — shown in the header and browser tab
- `APP_TAGLINE` — currently unused in the UI but available if you want to add it
- `ACCENT` / `ACCENT_HOVER` — the highlight color used for buttons, active
  filters, and links (a few ready-made hex values for blue/purple/orange/red
  are listed in the file as comments)
- `BG_COLOR` / `CARD_COLOR` — page background and card background

After editing, rebuild:
```bash
docker compose up -d --build
```

If you want to change more than colors — layout, fonts, wording — the
templates are plain HTML + Tailwind CSS classes in `app/templates/`. No
build step needed for markup changes, just edit and rebuild the container.
If you want deeper visual changes later, just tell me what you have in mind
and I'll make the edits for you.

## Login (required)

The app now requires a username/password login — nobody can view or edit
your inventory (financials, seller/buyer names, receipts) without it,
which matters since it's reachable through your Cloudflare tunnel.

**Credentials live in a file, not in `docker-compose.yml` or a `.env`.**
Docker Compose treats `$` as the start of a variable substitution
anywhere it reads config (including `.env` in some setups), which
silently corrupts a bcrypt hash like `$2b$12$...`. To make that bug
impossible rather than just avoiding it, the app instead reads
`data/auth_config.json` — a plain JSON file inside the `./data` folder
that's already bind-mounted into the container. Compose never parses
this file's contents, so there's nothing left for it to mangle.

**One-time setup, before the first `docker compose up`, from inside this folder:**

```bash
pip install bcrypt --break-system-packages   # if not already installed
python3 generate_password_hash.py
```

It asks for a username and password, then writes:

```
data/auth_config.json
```

containing your username, the bcrypt hash of your password, and a random
session-signing key. `docker-compose.yml` needs zero edits — it doesn't
reference your credentials at all. Your plaintext password is never
stored anywhere, only the hash is. The file is created with `chmod 600`
(owner-read-only). If you ever back up or version-control this folder,
exclude `data/auth_config.json` the same way you'd exclude any secret.

To change the password later, just re-run the script — it'll ask before
overwriting the existing file.

**`COOKIE_SECURE`**: leave this `true` (the default) whenever you're
behind the Cloudflare tunnel or any https reverse proxy — that's how
you'll be using it. Only set it to `false` if you're testing over plain
`http://10.0.0.x:8420` on your LAN with no tunnel involved; browsers won't
send a `Secure` cookie over plain http, so login would silently fail to
persist otherwise.

Sessions last 14 days by default (`SESSION_MAX_AGE_SECONDS` env var if you
want to change that), and 5 wrong password attempts from the same IP
trigger a 60-second lockout.

## Deploy on pcmedia

1. Copy this folder to your `pcmedia` box, e.g.:
   ```bash
   scp -r pcflip human1zer@10.0.0.x:/home/human1zer/pcflip
   ```

2. SSH in and start it:
   ```bash
   cd /home/human1zer/pcflip
   docker compose up -d --build
   ```

3. Open it locally: `http://10.0.0.x:8420` (replace with pcmedia's LAN IP)

Everything — the SQLite database **and** all photos — lives in `./data/` on
the host (bind-mounted, not just inside the container), so it survives
container rebuilds.

## Backups (important — protects against the SSD dying)

`./data/` is the only thing that matters. If `pcmedia`'s disk fails, you
lose it unless it's backed up elsewhere — same situation you already hit
with the Jellyfin container.

`backup.sh` takes a **dated, versioned snapshot** each run (not just a
mirror) — a consistent `sqlite3 .backup` of the database plus the
photos/attachments folders, hardlinked against the previous snapshot so
unchanged files don't use extra disk space. That way, if a DB gets
corrupted or you delete an item by mistake, you can still recover an
earlier day's snapshot instead of the backup just mirroring the mistake.
Snapshots older than 30 days are pruned automatically (`RETENTION_DAYS`
at the top of the script).

Edit `SOURCE_DIR` and `DEST_ROOT` inside `backup.sh` to match your paths
(you already mount NAS shares elsewhere, e.g. `/mnt/nas-movies`), then set
up a cron job:

```bash
chmod +x backup.sh
crontab -e
# add this line to back up every night at 3am:
0 3 * * * /home/human1zer/pcflip/backup.sh
```

To restore: pick a folder under `$DEST_ROOT/snapshots/`, copy `pcflip.db`
and the `photos`/`attachments` folders from it back into `./data/`, then
`docker compose up -d`. `$DEST_ROOT/latest` always symlinks to the most
recent successful snapshot.

## Moving to another PC

Because everything is Dockerized and self-contained in one folder:
```bash
scp -r pcflip human1zer@<new-pc-ip>:/home/human1zer/
ssh human1zer@<new-pc-ip>
cd pcflip && docker compose up -d --build
```
The `data/` folder (DB + photos) travels with the project folder — nothing
else to configure. This is also your recovery procedure if you're restoring
from a NAS backup onto a fresh machine.

## Access from your phone

Two options:

- **On your home network**: just use `http://10.0.0.x:8420` — works from
  any device on the `10.0.0.0/24` subnet.
- **Away from home**: you already have a Cloudflare tunnel running for
  Jellyfin (PCMEDIA tunnel). Add a second public hostname in that same
  tunnel pointing at `http://localhost:8420`, then you can reach it from
  anywhere at something like `pcflip.yourdomain.com`. This avoids opening
  any ports.

## Updating later

If you want new fields or features, edit the files in `app/` and rebuild:
```bash
docker compose up -d --build
```

## Notes on the "payment method" field

FINN doesn't force a single settlement method, so the dropdown covers the
common ones people actually use here: **Vipps**, **cash**, **bank transfer**,
and **FINN Trygg handel** (their escrow/safe-trade option). Pick "Other" and
put details in Notes if it was something else.

## Ideas for further improvements

- **Multi-currency** if you ever buy/sell across borders (EUR/USD alongside NOK)
- **Margin/VAT view** for actual bookkeeping if you're reporting this as income
- **Listing price vs sold price** tracking, to see how much you're discounting off FINN asking price
- **Barcode/serial scanning** from phone camera to speed up data entry
- **Reminders** for items sitting unsold too long
- **Photo drag-to-reorder** so the best photo is always the cover image

Happy to build any of these in — just ask.

