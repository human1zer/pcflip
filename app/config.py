"""
Customize your app here. Edit the values below, then rebuild:
    docker compose up -d --build

Colors are plain hex codes. Any color picker (e.g. https://coolors.co) will
give you hex values you can paste in directly.
"""
import os
import sys
import json

# --- Auth ---
# Credentials are read from a plain JSON file inside the data folder, NOT
# from docker-compose.yml or a .env file. This is deliberate: Docker
# Compose treats "$" as the start of a variable substitution anywhere it
# parses a YAML value (and also inside a referenced .env file's values in
# some Compose versions/setups), which silently mangles a bcrypt hash like
# "$2b$12$...". A file that Compose never touches can't have this problem.
#
# Run `python3 generate_password_hash.py` to create this file.
AUTH_CONFIG_PATH = os.environ.get("AUTH_CONFIG_PATH", "/app/data/auth_config.json")

ADMIN_USERNAME = None
ADMIN_PASSWORD_HASH = None
SECRET_KEY = None

if os.path.exists(AUTH_CONFIG_PATH):
    try:
        with open(AUTH_CONFIG_PATH) as f:
            _auth = json.load(f)
        ADMIN_USERNAME = _auth.get("username")
        ADMIN_PASSWORD_HASH = _auth.get("password_hash")
        SECRET_KEY = _auth.get("secret_key")
    except (json.JSONDecodeError, OSError):
        pass

# Set COOKIE_SECURE=false only if you access the app over plain http on your
# LAN with no tunnel in front of it. Leave it true (default) whenever a
# Cloudflare tunnel / any https reverse proxy sits in front of the app.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
SESSION_MAX_AGE_SECONDS = int(os.environ.get("SESSION_MAX_AGE_SECONDS", str(60 * 60 * 24 * 14)))  # 14 days

if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH or not SECRET_KEY:
    sys.exit(
        "\nMissing or unreadable auth config.\n"
        f"Expected a file at {AUTH_CONFIG_PATH} (inside your ./data folder, so it's\n"
        "already bind-mounted into the container -- nothing extra to configure).\n"
        "Run `python3 generate_password_hash.py` from your pcflip folder on the host\n"
        "to create it. See README.md.\n"
    )

# --- Branding ---
APP_NAME = os.environ.get("APP_NAME", "Flip It")
APP_TAGLINE = os.environ.get("APP_TAGLINE", "Buy & sell tracker")

# --- Colors ---
# ACCENT is used for buttons, active filters, links, and highlights.
# ACCENT_HOVER is used when you hover/tap those elements (usually a shade
# darker or lighter than ACCENT).
ACCENT = os.environ.get("ACCENT_COLOR", "#10b981")          # default: emerald green
ACCENT_HOVER = os.environ.get("ACCENT_HOVER_COLOR", "#059669")

# Page background and card background.
BG_COLOR = os.environ.get("BG_COLOR", "#020617")             # default: near-black
CARD_COLOR = os.environ.get("CARD_COLOR", "#0f172a")         # default: dark slate

# --- A few ready-made themes you can copy into the values above ---
# Blue:    ACCENT="#3b82f6" ACCENT_HOVER="#2563eb"
# Purple:  ACCENT="#a855f7" ACCENT_HOVER="#9333ea"
# Orange:  ACCENT="#f97316" ACCENT_HOVER="#ea580c"
# Red:     ACCENT="#ef4444" ACCENT_HOVER="#dc2626"
