"""
Import a listing from FINN.no into the new-item form.

Fetches https://www.finn.no/recommerce/forsale/item/{finnkode} once per
uncached finnkode, parses the embedded JSON-LD "Product" block plus the
server-rendered description paragraphs, and maps a couple of fields onto
this app's own dropdown values. Never raises past `fetch_listing()` --
callers get a `FinnImportError` with a message safe to show the user.

Note on parsing: FINN wraps the whole page body in
<template shadowrootmode="open"> (a declarative Shadow DOM). BeautifulSoup
silently drops text living inside a <template> from .get_text()/.strings/
.text -- they return "" for every paragraph on this page. `_tag_text()`
below works around that with find_all(string=True), which isn't subject
to that filtering.
"""
import json
import os
import re

import requests
from bs4 import BeautifulSoup

from . import database

FINN_ITEM_URL = "https://www.finn.no/recommerce/forsale/item/{}"
FINN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}
FETCH_TIMEOUT_SECONDS = 10

FINN_CACHE_DIR = os.environ.get(
    "FINN_CACHE_DIR", os.path.join(os.path.dirname(database.DB_PATH), "finn_cache")
)

# Only mapped when confident. schema.org's "UsedCondition" is too coarse to
# safely pick between this app's "Used - Good" / "Used - Fair" -- left blank.
CONDITION_MAP = {
    "NewCondition": "New",
}

# Matched against the last "> "-separated segment of FINN's category
# breadcrumb (e.g. "Elektronikk og hvitevarer > Data > Datakomponenter").
# Deliberately small -- anything not listed here is left at the form's
# default rather than guessed. Extend as real listings turn up new terms.
CATEGORY_MAP = {
    "bærbar pc": "Laptop",
    "bærbare pc-er": "Laptop",
    "laptop": "Laptop",
    "stasjonær pc": "Desktop PC",
    "stasjonære pc-er": "Desktop PC",
    "desktop": "Desktop PC",
    "skjermkort": "GPU",
    "grafikkort": "GPU",
    "prosessor": "CPU",
    "prosessorer": "CPU",
    "hovedkort": "Motherboard",
    "moderkort": "Motherboard",
    "minne": "RAM",
    "ram": "RAM",
    "ram-minne": "RAM",
    "harddisk": "SSD/Storage",
    "ssd": "SSD/Storage",
    "lagring": "SSD/Storage",
    "strømforsyning": "PSU",
    "kabinett": "Case",
    "skjerm": "Monitor",
    "skjermer": "Monitor",
    "monitor": "Monitor",
    "tilbehør": "Peripheral",
    "periferiutstyr": "Peripheral",
}


class FinnImportError(Exception):
    """Raised with a message that's safe to show directly to the user."""


def extract_finnkode(raw: str) -> str | None:
    """Accepts a bare finnkode, a /item/{id} URL, a legacy ?finnkode= URL,
    or falls back to the first 6-10 digit run found."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return raw
    m = re.search(r"(?:finnkode=|/item/)(\d+)", raw)
    if m:
        return m.group(1)
    m = re.search(r"(\d{6,10})", raw)
    return m.group(1) if m else None


def fetch_listing(finnkode: str) -> dict:
    """Returns a dict: finnkode, title, notes, image_url, condition, category,
    partial (True if this came from the Open Graph fallback rather than the
    JSON-LD Product block -- condition/category are always None then).
    Cached by finnkode on disk so re-imports never refetch finn.no."""
    cache_path = _cache_path(finnkode)
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass  # fall through and refetch a corrupt/unreadable cache entry

    try:
        resp = requests.get(
            FINN_ITEM_URL.format(finnkode), headers=FINN_HEADERS, timeout=FETCH_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise FinnImportError("couldn't reach finn.no") from e

    try:
        parsed = _parse_listing_html(resp.text, finnkode)
    except FinnImportError:
        raise
    except Exception as e:
        raise FinnImportError("couldn't read that listing's page") from e

    os.makedirs(FINN_CACHE_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f)
    return parsed


def _cache_path(finnkode: str) -> str:
    return os.path.join(FINN_CACHE_DIR, f"{finnkode}.json")


def _tag_text(tag) -> str:
    return "".join(str(s) for s in tag.find_all(string=True, recursive=True))


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())


def _parse_listing_html(html: str, finnkode: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    product = None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if data.get("@type") == "Product":
            product = data
            break

    if product is not None:
        category_raw = ""
        for prop in product.get("additionalProperty") or []:
            if prop.get("name") == "category":
                category_raw = prop.get("value") or ""
                break

        return {
            "finnkode": product.get("sku") or finnkode,
            "title": product.get("name") or "",
            "notes": _extract_full_description(soup, product.get("description") or ""),
            "image_url": product.get("image") or None,
            "condition": CONDITION_MAP.get(product.get("itemCondition") or ""),
            "category": _map_category(category_raw),
            "partial": False,
        }

    # No JSON-LD Product block -- seen on inactive/sold listings (meta
    # robots=noindex). Fall back to Open Graph meta tags; these sit in
    # <head>, outside the <template shadowrootmode> wrapper, so reading
    # their `content` attribute is unaffected by the TemplateString issue
    # above (that only breaks *text-node* extraction, not attributes).
    # Condition/category have no OG equivalent -- left blank rather than guessed.
    title = _clean_og_title(_meta_content(soup, "og:title"))
    if not title:
        raise FinnImportError("couldn't find listing data on that page")

    return {
        "finnkode": finnkode,
        "title": title,
        "notes": _extract_full_description(soup, _meta_content(soup, "og:description")),
        "image_url": _meta_content(soup, "og:image") or None,
        "condition": None,
        "category": None,
        "partial": True,
    }


def _meta_content(soup: BeautifulSoup, property_name: str) -> str:
    tag = soup.find("meta", attrs={"property": property_name})
    return (tag.get("content") or "").strip() if tag else ""


def _clean_og_title(raw: str) -> str:
    """FINN's og:title is "{title} | FINN-torget" (or a similar site-name
    suffix) -- take everything before the last " | "."""
    return raw.rsplit(" | ", 1)[0].strip() if " | " in raw else raw


def _extract_full_description(soup: BeautifulSoup, description_hint: str) -> str:
    """`description_hint` is whatever truncated description text we have --
    the JSON-LD description, or og:description in the fallback path -- used
    only to locate the right container by matching its first ~100 chars.
    Falls back to the whitespace-pre-wrap class selector if that fails (or
    if there's no hint at all)."""
    prefix = _normalize_ws(description_hint[:100])
    container = None

    if prefix:
        for div in soup.find_all("div"):
            paragraphs = div.find_all("p", recursive=False)
            if not paragraphs:
                continue
            joined = "\n".join(_tag_text(p) for p in paragraphs)
            if _normalize_ws(joined).startswith(prefix):
                container = div
                break

    if container is None:
        for div in soup.find_all("div", class_=lambda c: c and "whitespace-pre-wrap" in c.split()):
            container = div
            break

    if container is None:
        return description_hint  # last-resort fallback -- truncated, but better than nothing

    lines = [_tag_text(p).replace("\xa0", " ").strip() for p in container.find_all("p", recursive=False)]
    return "\n".join(lines)


def _map_category(raw: str) -> str | None:
    if not raw:
        return None
    leaf = raw.split(">")[-1].strip().lower()
    return CATEGORY_MAP.get(leaf)
