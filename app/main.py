import os
import time
import uuid
import shutil
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from typing import List
from PIL import Image, UnidentifiedImageError
import pillow_avif  # noqa: F401 -- registers the AVIF decoder with Pillow on import
import csv
import io
import sqlite3
import requests
from datetime import date

from . import database, config, auth, finn_import
from .constants import (
    CATEGORIES, CONDITIONS, STATUS_LABELS, EDITABLE_STATUSES,
    PAYMENT_METHODS, DELIVERY_METHODS,
)

app = FastAPI(title=config.APP_NAME)
app.add_middleware(auth.AuthMiddleware)

templates = Jinja2Templates(directory="app/templates")
templates.env.globals.update(
    app_name=config.APP_NAME,
    app_tagline=config.APP_TAGLINE,
    accent=config.ACCENT,
    accent_hover=config.ACCENT_HOVER,
    bg_color=config.BG_COLOR,
    card_color=config.CARD_COLOR,
)


def render(request: Request, name: str, status_code: int = 200, **context):
    """TemplateResponse wrapper that always injects the CSRF token for forms."""
    context["request"] = request
    context["csrf_token"] = auth.csrf_token_for(request)
    return templates.TemplateResponse(name, context, status_code=status_code)


database.init_db()
app.mount("/photos", StaticFiles(directory=database.PHOTOS_DIR), name="photos")
app.mount("/attachments", StaticFiles(directory=database.ATTACHMENTS_DIR), name="attachments")

# Guard against decompression-bomb style images (huge pixel dimensions that
# are cheap to upload but expensive/memory-hungry to decode).
Image.MAX_IMAGE_PIXELS = 40_000_000  # ~40MP, generous for phone photos

MAX_DIMENSION = 1600   # full-size photos, resized down if bigger
THUMB_DIMENSION = 400  # grid thumbnails


@app.get("/health")
def health():
    return PlainTextResponse("ok")


# --- Auth routes ---

@app.get("/login")
def login_form(request: Request, next: str = "/"):
    if auth.read_session(request):
        return RedirectResponse(next or "/", status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request, "error": None, "next": next,
    })


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    next_path = form.get("next") or "/"

    if auth.is_locked_out(request):
        return templates.TemplateResponse("login.html", {
            "request": request, "next": next_path,
            "error": "Too many failed attempts. Wait a minute and try again.",
        }, status_code=429)

    valid = auth.verify_username(username) and auth.verify_password(password, config.ADMIN_PASSWORD_HASH)
    if not valid:
        auth.record_failed_login(request)
        return templates.TemplateResponse("login.html", {
            "request": request, "next": next_path,
            "error": "Wrong username or password.",
        }, status_code=401)

    auth.clear_failed_logins(request)
    response = RedirectResponse(next_path if next_path.startswith("/") else "/", status_code=303)
    auth.set_session_cookie(response, auth.create_session_cookie_value(username))
    return response


@app.post("/logout")
async def logout(request: Request):
    form = await request.form()
    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response)
    return response


def _compute_profit(status, sale_price, purchase_price, cost_total):
    if status == "sold" and sale_price is not None and purchase_price is not None:
        return sale_price - purchase_price - cost_total
    return 0


def row_to_dict(row, cost_total=0.0):
    d = dict(row)
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    d["cost_total"] = cost_total
    d["profit"] = _compute_profit(d["status"], d.get("sale_price"), d.get("purchase_price"), cost_total)
    return d


def get_cost_totals(conn) -> dict:
    """{item_id: sum(amount)} for every item that has at least one linked cost."""
    rows = conn.execute(
        "SELECT item_id, SUM(amount) AS total FROM costs WHERE item_id IS NOT NULL GROUP BY item_id"
    ).fetchall()
    return {r["item_id"]: r["total"] for r in rows}


def get_costs(conn, item_id):
    """This item's own cost rows, each carrying the linked source item's
    title (if it's a consumption cost) so the template can link to it."""
    rows = conn.execute(
        """
        SELECT costs.*, source.title AS source_title
        FROM costs LEFT JOIN items AS source ON source.id = costs.source_item_id
        WHERE costs.item_id = ?
        ORDER BY costs.date DESC, costs.id DESC
        """,
        (item_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_consumed_into(conn, item_id):
    """If this item was consumed into another one, the cost row (+ target
    item id/title) recording that -- otherwise None."""
    row = conn.execute(
        """
        SELECT costs.id AS cost_id, costs.amount, target.id AS target_item_id, target.title AS target_title
        FROM costs JOIN items AS target ON target.id = costs.item_id
        WHERE costs.source_item_id = ?
        LIMIT 1
        """,
        (item_id,),
    ).fetchone()
    return dict(row) if row else None


def _batch_sibling_ids(conn, item_id: int) -> list[int]:
    """All item ids that share item_id's batch (including item_id itself),
    or just [item_id] if it isn't part of a batch."""
    row = conn.execute("SELECT batch_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row or not row["batch_id"]:
        return [item_id]
    rows = conn.execute("SELECT id FROM items WHERE batch_id = ?", (row["batch_id"],)).fetchall()
    return [r["id"] for r in rows]


def get_photos(conn, item_id):
    rows = conn.execute(
        "SELECT * FROM item_photos WHERE item_id = ? ORDER BY sort_order, id", (item_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def save_photo(item_id: int, upload: UploadFile) -> None:
    """Save an uploaded photo + generate a thumbnail. Skips non-image / oversized files silently."""
    try:
        img = Image.open(upload.file)
        img.verify()  # cheap header/structure check before we commit to decoding pixels
        upload.file.seek(0)
        img = Image.open(upload.file)
        img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        return

    item_dir = os.path.join(database.PHOTOS_DIR, str(item_id))
    os.makedirs(item_dir, exist_ok=True)

    photo_uuid = uuid.uuid4().hex
    filename = f"{photo_uuid}.jpg"
    thumb_filename = f"{photo_uuid}_thumb.jpg"

    full = img.copy()
    full.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    full.save(os.path.join(item_dir, filename), "JPEG", quality=85)

    thumb = img.copy()
    thumb.thumbnail((THUMB_DIMENSION, THUMB_DIMENSION))
    thumb.save(os.path.join(item_dir, thumb_filename), "JPEG", quality=80)

    with database.get_conn() as conn:
        for iid in _batch_sibling_ids(conn, item_id):
            max_order = conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM item_photos WHERE item_id = ?", (iid,)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO item_photos (item_id, filename, thumb_filename, sort_order) VALUES (?, ?, ?, ?)",
                (iid, f"{item_id}/{filename}", f"{item_id}/{thumb_filename}", max_order + 1),
            )


class _DownloadedFile:
    """Duck-types the one attribute save_photo() actually uses off UploadFile."""

    def __init__(self, file_obj):
        self.file = file_obj


def _attach_photo_from_url(item_id: int, url: str) -> None:
    """Best-effort: a broken image URL or network hiccup here must never
    block item creation, so failures are swallowed."""
    try:
        resp = requests.get(url, headers=finn_import.FINN_HEADERS, timeout=finn_import.FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException:
        return
    save_photo(item_id, _DownloadedFile(io.BytesIO(resp.content)))


def get_attachments(conn, item_id):
    rows = conn.execute(
        "SELECT * FROM item_attachments WHERE item_id = ? ORDER BY id", (item_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def save_attachment(item_id: int, upload: UploadFile) -> None:
    """Save a receipt as either a PDF (stored as-is) or an image (resized like a photo)."""
    content_type = (upload.content_type or "").lower()
    name_lower = (upload.filename or "").lower()
    is_pdf = content_type == "application/pdf" or name_lower.endswith(".pdf")
    is_image = content_type.startswith("image/") or name_lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".heic", ".avif"))

    if not (is_pdf or is_image):
        return

    item_dir = os.path.join(database.ATTACHMENTS_DIR, str(item_id))
    os.makedirs(item_dir, exist_ok=True)
    file_uuid = uuid.uuid4().hex

    if is_pdf:
        filename = f"{file_uuid}.pdf"
        with open(os.path.join(item_dir, filename), "wb") as out:
            shutil.copyfileobj(upload.file, out)
        file_type = "pdf"
    else:
        try:
            img = Image.open(upload.file)
            img.verify()
            upload.file.seek(0)
            img = Image.open(upload.file)
            img = img.convert("RGB") if img.mode not in ("RGB", "L") else img
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
            return
        filename = f"{file_uuid}.jpg"
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
        img.save(os.path.join(item_dir, filename), "JPEG", quality=90)
        file_type = "image"

    with database.get_conn() as conn:
        for iid in _batch_sibling_ids(conn, item_id):
            conn.execute(
                "INSERT INTO item_attachments (item_id, filename, original_name, file_type) VALUES (?, ?, ?, ?)",
                (iid, f"{item_id}/{filename}", upload.filename, file_type),
            )


FORM_FIELDS = [
    "category", "title", "serial_number", "condition", "status", "notes",
    "seller_name", "seller_contact", "purchase_price", "purchase_date",
    "purchase_method", "purchase_delivery", "purchase_shipping_company", "purchase_tracking",
    "buyer_name", "buyer_contact", "sale_price", "sale_date",
    "sale_method", "sale_delivery", "sale_shipping_company", "sale_tracking",
]


@app.get("/")
def dashboard(request: Request):
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM items").fetchall()
        cost_totals = get_cost_totals(conn)
        general_expenses = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM costs WHERE item_id IS NULL"
        ).fetchone()[0]
    items = [row_to_dict(r, cost_totals.get(r["id"], 0.0)) for r in rows]

    in_stock = [i for i in items if i["status"] in ("in_stock", "listed")]
    sold = [i for i in items if i["status"] == "sold"]
    # "consumed" items are excluded from both buckets above (their spend is
    # already represented via the cost row they created on their target) and
    # from total_invested below, so their purchase price is never counted twice.

    total_profit = sum(i["profit"] for i in sold)
    stats = {
        "in_stock_count": len(in_stock),
        "sold_count": len(sold),
        "total_invested": sum(i["purchase_price"] or 0 for i in items if i["status"] != "consumed"),
        "total_revenue": sum(i["sale_price"] or 0 for i in sold),
        "total_profit": total_profit,
        "general_expenses": general_expenses,
        "net_profit": total_profit - general_expenses,
    }

    recent_sales = sorted(sold, key=lambda i: i["sale_date"] or "", reverse=True)[:5]

    return render(request, "dashboard.html",
        stats=stats,
        in_stock_items=in_stock[:8],
        recent_sales=recent_sales,
    )


@app.get("/items")
def items_list(request: Request, status: str = None, q: str = None):
    query = "SELECT * FROM items WHERE 1=1"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if q:
        query += " AND (title LIKE ? OR serial_number LIKE ? OR seller_name LIKE ? OR buyer_name LIKE ?)"
        like = f"%{q}%"
        params += [like, like, like, like]
    query += " ORDER BY created_at DESC"

    with database.get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
        cost_totals = get_cost_totals(conn)
        items = [row_to_dict(r, cost_totals.get(r["id"], 0.0)) for r in rows]
        for item in items:
            cover = conn.execute(
                "SELECT thumb_filename FROM item_photos WHERE item_id = ? ORDER BY sort_order, id LIMIT 1",
                (item["id"],),
            ).fetchone()
            item["cover_photo"] = cover["thumb_filename"] if cover else None

    return render(request, "items_list.html", items=items, status=status, q=q or "")


def _parse_cost_form(form) -> tuple[dict | None, str | None]:
    """Shared by item-linked costs and general expenses. Returns (values, error)."""
    label = (form.get("label") or "").strip()
    if not label:
        return None, "Label is required."

    amount_raw = form.get("amount") or ""
    try:
        amount = float(str(amount_raw).replace(",", "."))
    except ValueError:
        return None, "Amount must be a number."
    if amount < 0:
        return None, "Amount can't be negative."

    cost_date = (form.get("date") or "").strip() or None
    return {"label": label, "amount": amount, "date": cost_date}, None


@app.get("/expenses")
def expenses_list(request: Request):
    with database.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM costs WHERE item_id IS NULL ORDER BY date DESC, id DESC"
        ).fetchall()
        total = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM costs WHERE item_id IS NULL").fetchone()[0]
    return render(request, "expenses.html", expenses=[dict(r) for r in rows], total=total,
                  today=date.today().isoformat(), error=None)


@app.post("/expenses")
async def expenses_create(request: Request):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)

    values, error = _parse_cost_form(form)
    if error:
        with database.get_conn() as conn:
            rows = conn.execute("SELECT * FROM costs WHERE item_id IS NULL ORDER BY date DESC, id DESC").fetchall()
            total = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM costs WHERE item_id IS NULL").fetchone()[0]
        return render(request, "expenses.html", expenses=[dict(r) for r in rows], total=total,
                      today=date.today().isoformat(), error=error, status_code=400)

    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO costs (item_id, label, amount, date) VALUES (NULL, ?, ?, ?)",
            (values["label"], values["amount"], values["date"]),
        )
    return RedirectResponse("/expenses", status_code=303)


@app.post("/expenses/{cost_id}/delete")
async def expenses_delete(request: Request, cost_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)
    with database.get_conn() as conn:
        # Scoped to item_id IS NULL so this route can never delete an item-linked cost.
        conn.execute("DELETE FROM costs WHERE id = ? AND item_id IS NULL", (cost_id,))
    return RedirectResponse("/expenses", status_code=303)


FORM_TEMPLATE_CTX = dict(
    categories=CATEGORIES, conditions=CONDITIONS,
    statuses=EDITABLE_STATUSES, status_labels=STATUS_LABELS,
    payment_methods=PAYMENT_METHODS, delivery_methods=DELIVERY_METHODS,
)


@app.get("/items/new")
def item_new_form(request: Request):
    return render(request, "item_form.html", item=None, error=None, is_edit=False, **FORM_TEMPLATE_CTX)


def _parse_item_form(form) -> tuple[dict | None, str | None]:
    """Returns (values, error). If error is set, values is None."""
    values = {f: (form.get(f) or None) for f in FORM_FIELDS}

    if not (values["title"] or "").strip():
        return None, "Title is required."

    for money_field, label in (("purchase_price", "Purchase price"), ("sale_price", "Sale price")):
        if values[money_field]:
            try:
                parsed = float(str(values[money_field]).replace(",", "."))
            except ValueError:
                return None, f"{label} must be a number."
            if parsed < 0:
                return None, f"{label} can't be negative."
            values[money_field] = parsed

    return values, None


MAX_BATCH_QUANTITY = 500


def _split_purchase_price(total: float | None, quantity: int) -> list[float | None]:
    """Split a total across `quantity` units so they sum exactly to `total`,
    using integer cents so floating point can't leave a stray fraction. The
    last unit absorbs whatever the split doesn't divide evenly."""
    if total is None:
        return [None] * quantity
    total_cents = round(total * 100)
    base_cents = total_cents // quantity
    remainder_cents = total_cents - base_cents * quantity
    prices_cents = [base_cents] * (quantity - 1) + [base_cents + remainder_cents]
    return [c / 100 for c in prices_cents]


@app.post("/items/new")
async def item_create(request: Request):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)

    values, error = _parse_item_form(form)
    if error:
        return render(request, "item_form.html", item=dict(form), error=error, is_edit=False,
                      status_code=400, **FORM_TEMPLATE_CTX)

    quantity_raw = form.get("quantity") or "1"
    try:
        quantity = int(quantity_raw)
    except ValueError:
        return render(request, "item_form.html", item=dict(form), error="Quantity must be a whole number.",
                      is_edit=False, status_code=400, **FORM_TEMPLATE_CTX)
    if quantity < 1 or quantity > MAX_BATCH_QUANTITY:
        return render(request, "item_form.html", item=dict(form),
                      error=f"Quantity must be between 1 and {MAX_BATCH_QUANTITY}.",
                      is_edit=False, status_code=400, **FORM_TEMPLATE_CTX)

    finn_image_url = form.get("finn_image_url") or None

    if quantity == 1:
        cols = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        try:
            with database.get_conn() as conn:
                cur = conn.execute(f"INSERT INTO items ({cols}) VALUES ({placeholders})", list(values.values()))
                new_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return render(request, "item_form.html", item=dict(form), error="Couldn't save item — check the required fields.",
                          is_edit=False, status_code=400, **FORM_TEMPLATE_CTX)
        if finn_image_url:
            _attach_photo_from_url(new_id, finn_image_url)
        return RedirectResponse(f"/items/{new_id}", status_code=303)

    batch_id = uuid.uuid4().hex
    base_title = (values["title"] or "").strip()
    unit_prices = _split_purchase_price(values["purchase_price"], quantity)

    cols = list(values.keys()) + ["batch_id"]
    placeholders = ", ".join("?" for _ in cols)
    try:
        with database.get_conn() as conn:
            first_id = None
            for n in range(1, quantity + 1):
                unit_values = dict(values)
                unit_values["title"] = f"{base_title} ({n}/{quantity})"
                unit_values["purchase_price"] = unit_prices[n - 1]
                row = list(unit_values.values()) + [batch_id]
                cur = conn.execute(f"INSERT INTO items ({', '.join(cols)}) VALUES ({placeholders})", row)
                if first_id is None:
                    first_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return render(request, "item_form.html", item=dict(form), error="Couldn't save items — check the required fields.",
                      is_edit=False, status_code=400, **FORM_TEMPLATE_CTX)

    if finn_image_url:
        # save_photo() fans a photo out to every batch sibling already, so
        # attaching it once to the first unit covers the whole batch.
        _attach_photo_from_url(first_id, finn_image_url)

    return RedirectResponse(f"/items/{first_id}", status_code=303)


@app.post("/items/import/finn")
async def item_import_finn(request: Request):
    """Prefills the new-item form from a FINN.no listing. Re-renders the
    same form either way -- on failure the user still has everything they'd
    already typed, and can just fill it in by hand."""
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)

    base_item = dict(form)
    finnkode = finn_import.extract_finnkode(form.get("finn_input") or "")
    if not finnkode:
        return render(request, "item_form.html", item=base_item, is_edit=False,
                      error="Couldn't find a FINN-kode in that.", status_code=400, **FORM_TEMPLATE_CTX)

    try:
        listing = finn_import.fetch_listing(finnkode)
    except finn_import.FinnImportError as e:
        return render(request, "item_form.html", item=base_item, is_edit=False,
                      error=f"FINN import failed ({e}) — fill the form in manually.",
                      status_code=200, **FORM_TEMPLATE_CTX)

    merged = dict(base_item)
    merged["title"] = listing["title"]
    merged["purchase_tracking"] = listing["finnkode"]
    existing_notes = (base_item.get("notes") or "").strip()
    merged["notes"] = f"{existing_notes}\n\n--- Imported from FINN ---\n\n{listing['notes']}" if existing_notes else listing["notes"]
    if listing["condition"]:
        merged["condition"] = listing["condition"]
    if listing["category"]:
        merged["category"] = listing["category"]
    merged["finn_image_url"] = listing["image_url"] or ""

    notice = None
    if listing.get("partial"):
        notice = ("Partial import — this listing has no structured data left (likely sold or inactive), "
                   "so only the title, photo, and description came through. Condition and category weren't "
                   "detected; fill those in manually.")

    return render(request, "item_form.html", item=merged, is_edit=False, error=None, notice=notice, **FORM_TEMPLATE_CTX)


@app.get("/items/{item_id}")
def item_detail(request: Request, item_id: int):
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            return RedirectResponse("/items", status_code=303)
        photos = get_photos(conn, item_id)
        attachments = get_attachments(conn, item_id)
        costs = get_costs(conn, item_id)
        cost_total = sum(c["amount"] for c in costs)
        consumed_into = get_consumed_into(conn, item_id) if row["status"] == "consumed" else None
        consume_targets = []
        if row["status"] in ("in_stock", "listed"):
            consume_targets = [
                dict(r) for r in conn.execute(
                    "SELECT id, title FROM items WHERE status IN ('in_stock', 'listed') AND id != ? ORDER BY title",
                    (item_id,),
                ).fetchall()
            ]
    item = row_to_dict(row, cost_total)
    return render(request, "item_detail.html", item=item, photos=photos, attachments=attachments,
                  costs=costs, today=date.today().isoformat(),
                  consumed_into=consumed_into, consume_targets=consume_targets)


@app.get("/items/{item_id}/edit")
def item_edit_form(request: Request, item_id: int):
    with database.get_conn() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return RedirectResponse("/items", status_code=303)
    item = row_to_dict(row)
    return render(request, "item_form.html", item=item, error=None, is_edit=True, **FORM_TEMPLATE_CTX)


@app.post("/items/{item_id}/edit")
async def item_update(request: Request, item_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)

    values, error = _parse_item_form(form)
    if error:
        # Keep the item_id so "Cancel" and the form action still work.
        bad_item = dict(form)
        bad_item["id"] = item_id
        return render(request, "item_form.html", item=bad_item, error=error, is_edit=True,
                      status_code=400, **FORM_TEMPLATE_CTX)

    set_clause = ", ".join(f"{k} = ?" for k in values.keys())
    try:
        with database.get_conn() as conn:
            conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?", list(values.values()) + [item_id])
    except sqlite3.IntegrityError:
        bad_item = dict(form)
        bad_item["id"] = item_id
        return render(request, "item_form.html", item=bad_item,
                      error="Couldn't save item — check the required fields.", is_edit=True,
                      status_code=400, **FORM_TEMPLATE_CTX)

    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/costs")
async def item_cost_create(request: Request, item_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)

    values, error = _parse_cost_form(form)
    if error:
        # Costs aren't shown as a distinct error state in the template today;
        # redirecting back keeps this simple and consistent with how photo/
        # attachment uploads silently no-op on bad input.
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO costs (item_id, label, amount, date) VALUES (?, ?, ?, ?)",
            (item_id, values["label"], values["amount"], values["date"]),
        )
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/costs/{cost_id}/delete")
async def item_cost_delete(request: Request, item_id: int, cost_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)
    with database.get_conn() as conn:
        # source_item_id IS NULL so a consumption-linked cost can't be deleted
        # here -- that must go through /consume/undo to keep the source
        # item's status in sync.
        conn.execute(
            "DELETE FROM costs WHERE id = ? AND item_id = ? AND source_item_id IS NULL",
            (cost_id, item_id),
        )
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/consume")
async def item_consume(request: Request, item_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)

    try:
        target_id = int(form.get("target_item_id") or "")
    except ValueError:
        return RedirectResponse(f"/items/{item_id}", status_code=303)
    if target_id == item_id:
        return RedirectResponse(f"/items/{item_id}", status_code=303)

    with database.get_conn() as conn:
        source = conn.execute("SELECT * FROM items WHERE id = ? AND status IN ('in_stock', 'listed')",
                               (item_id,)).fetchone()
        target = conn.execute("SELECT * FROM items WHERE id = ? AND status IN ('in_stock', 'listed')",
                               (target_id,)).fetchone()
        if not source or not target:
            return RedirectResponse(f"/items/{item_id}", status_code=303)

        conn.execute(
            "INSERT INTO costs (item_id, label, amount, source_item_id) VALUES (?, ?, ?, ?)",
            (target_id, f"Consumed: {source['title']}", source["purchase_price"] or 0, item_id),
        )
        conn.execute("UPDATE items SET status = 'consumed' WHERE id = ?", (item_id,))

    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/consume/undo")
async def item_consume_undo(request: Request, item_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)
    with database.get_conn() as conn:
        conn.execute("DELETE FROM costs WHERE source_item_id = ?", (item_id,))
        conn.execute("UPDATE items SET status = 'in_stock' WHERE id = ? AND status = 'consumed'", (item_id,))
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/photos")
async def upload_photos(request: Request, item_id: int, files: List[UploadFile] = File(...)):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)
    for f in files:
        if f.filename:
            save_photo(item_id, f)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/photos/{photo_id}/delete")
async def delete_photo(request: Request, item_id: int, photo_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)
    with database.get_conn() as conn:
        photo = conn.execute("SELECT * FROM item_photos WHERE id = ? AND item_id = ?", (photo_id, item_id)).fetchone()
        if photo:
            conn.execute("DELETE FROM item_photos WHERE id = ?", (photo_id,))
            for fname in (photo["filename"], photo["thumb_filename"]):
                still_used = conn.execute(
                    "SELECT 1 FROM item_photos WHERE filename = ? OR thumb_filename = ? LIMIT 1",
                    (fname, fname),
                ).fetchone()
                if not still_used:
                    path = os.path.join(database.PHOTOS_DIR, fname)
                    if os.path.exists(path):
                        os.remove(path)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/attachments")
async def upload_attachments(request: Request, item_id: int, files: List[UploadFile] = File(...)):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)
    for f in files:
        if f.filename:
            save_attachment(item_id, f)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/attachments/{attachment_id}/delete")
async def delete_attachment(request: Request, item_id: int, attachment_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)
    with database.get_conn() as conn:
        att = conn.execute(
            "SELECT * FROM item_attachments WHERE id = ? AND item_id = ?", (attachment_id, item_id)
        ).fetchone()
        if att:
            conn.execute("DELETE FROM item_attachments WHERE id = ?", (attachment_id,))
            still_used = conn.execute(
                "SELECT 1 FROM item_attachments WHERE filename = ? LIMIT 1", (att["filename"],)
            ).fetchone()
            if not still_used:
                path = os.path.join(database.ATTACHMENTS_DIR, att["filename"])
                if os.path.exists(path):
                    os.remove(path)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/delete")
async def item_delete(request: Request, item_id: int):
    form = await request.form()
    if not auth.check_csrf(request, form):
        return RedirectResponse("/login", status_code=303)
    with database.get_conn() as conn:
        photos = get_photos(conn, item_id)
        attachments = get_attachments(conn, item_id)

        # Restore any items consumed into this one *before* deleting -- the
        # cost rows recording those consumptions are about to cascade away
        # with this item, and if we didn't do this first they'd be left
        # stuck at status='consumed' with nothing left pointing to them.
        consumed_children = conn.execute(
            "SELECT source_item_id FROM costs WHERE item_id = ? AND source_item_id IS NOT NULL", (item_id,)
        ).fetchall()
        for row in consumed_children:
            conn.execute("UPDATE items SET status = 'in_stock' WHERE id = ?", (row["source_item_id"],))

        # Cascades away this item's own item_photos/item_attachments/costs
        # rows, but leaves any sibling batch items' rows (and their files) intact.
        conn.execute("DELETE FROM items WHERE id = ?", (item_id,))

        for photo in photos:
            for fname in (photo["filename"], photo["thumb_filename"]):
                still_used = conn.execute(
                    "SELECT 1 FROM item_photos WHERE filename = ? OR thumb_filename = ? LIMIT 1",
                    (fname, fname),
                ).fetchone()
                if not still_used:
                    path = os.path.join(database.PHOTOS_DIR, fname)
                    if os.path.exists(path):
                        os.remove(path)

        for att in attachments:
            still_used = conn.execute(
                "SELECT 1 FROM item_attachments WHERE filename = ? LIMIT 1", (att["filename"],)
            ).fetchone()
            if not still_used:
                path = os.path.join(database.ATTACHMENTS_DIR, att["filename"])
                if os.path.exists(path):
                    os.remove(path)

    for base_dir in (database.PHOTOS_DIR, database.ATTACHMENTS_DIR):
        item_dir = os.path.join(base_dir, str(item_id))
        if os.path.isdir(item_dir) and not os.listdir(item_dir):
            shutil.rmtree(item_dir, ignore_errors=True)
    return RedirectResponse("/items", status_code=303)


@app.get("/export.csv")
def export_csv():
    with database.get_conn() as conn:
        rows = conn.execute("SELECT * FROM items ORDER BY created_at DESC").fetchall()
        cost_totals = get_cost_totals(conn)
        general_expenses = conn.execute(
            "SELECT label, amount, date FROM costs WHERE item_id IS NULL ORDER BY date DESC, id DESC"
        ).fetchall()

    output = io.StringIO()
    output.write("\ufeff")  # UTF-8 BOM so Excel renders norwegian characters correctly

    if rows:
        item_rows = []
        for r in rows:
            d = dict(r)
            cost_total = cost_totals.get(d["id"], 0.0)
            d["cost_total"] = cost_total
            d["profit"] = _compute_profit(d["status"], d.get("sale_price"), d.get("purchase_price"), cost_total)
            item_rows.append(d)
        # `status` (already a raw column here) plus cost_total/profit are what
        # let a reader avoid double counting: a 'consumed' item's own
        # purchase_price is also folded into its target's cost_total/profit,
        # so it must not be summed again as if it were still unsold stock.
        writer = csv.DictWriter(output, fieldnames=list(item_rows[0].keys()))
        writer.writeheader()
        for d in item_rows:
            writer.writerow(d)

    if general_expenses:
        output.write("\n")
        output.write("General expenses (not attributed to any item)\n")
        exp_writer = csv.DictWriter(output, fieldnames=["label", "amount", "date"])
        exp_writer.writeheader()
        for e in general_expenses:
            exp_writer.writerow(dict(e))

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pcflip_export.csv"},
    )
