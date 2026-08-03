"""
Login/session handling for pcflip.

- Passwords are hashed with bcrypt (never stored/compared in plaintext).
- Sessions are a signed, timestamped cookie (itsdangerous) -- the cookie
  can't be forged or edited without SECRET_KEY, and it expires server-side
  even if someone tries to replay an old cookie past SESSION_MAX_AGE_SECONDS.
- Failed logins are rate-limited per source IP with an increasing lockout,
  to slow down password-guessing bots that find the tunnel URL.
- A per-session CSRF token is required on every state-changing POST.
"""
import hmac
import secrets
import time

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse
from starlette.requests import Request

from . import config

SESSION_COOKIE = "pcflip_session"
CSRF_FIELD = "csrf_token"

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="pcflip-session")

# Paths reachable without being logged in.
PUBLIC_PATHS = {"/login", "/health"}
PUBLIC_PREFIXES = ("/static/",)

# --- Brute force protection (in-memory, per-process) ---
# Fine for a single-container, single-admin app. Resets on restart.
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 60
_failed_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Behind a Cloudflare tunnel the real client IP is in this header.
    fwd = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else request.client.host) or "unknown"


def is_locked_out(request: Request) -> bool:
    ip = _client_ip(request)
    attempts = [t for t in _failed_attempts.get(ip, []) if time.time() - t < _LOCKOUT_SECONDS]
    _failed_attempts[ip] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def record_failed_login(request: Request) -> None:
    ip = _client_ip(request)
    _failed_attempts.setdefault(ip, []).append(time.time())


def clear_failed_logins(request: Request) -> None:
    _failed_attempts.pop(_client_ip(request), None)


# --- Password hashing ---

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        # Malformed hash in config -- fail closed.
        return False


def verify_username(candidate: str) -> bool:
    # constant-time compare so timing can't leak whether the username matched
    return hmac.compare_digest(candidate or "", config.ADMIN_USERNAME)


# --- Session cookie ---

def create_session_cookie_value(username: str) -> str:
    csrf_token = secrets.token_urlsafe(32)
    return _serializer.dumps({"u": username, "csrf": csrf_token})


def read_session(request: Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        return _serializer.loads(raw, max_age=config.SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def set_session_cookie(response, value: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=config.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def csrf_token_for(request: Request) -> str:
    session = read_session(request)
    return session["csrf"] if session else ""


def verify_csrf(request: Request, session: dict, submitted_token: str) -> bool:
    return bool(submitted_token) and hmac.compare_digest(submitted_token, session.get("csrf", ""))


class AuthMiddleware(BaseHTTPMiddleware):
    """
    Blocks every request that isn't logged in, except the login page/assets.
    Only inspects the cookie -- deliberately does NOT touch the request body,
    so multipart file uploads in route handlers are unaffected (a request
    body can only safely be read/parsed once in Starlette).
    CSRF tokens on POSTs are checked separately, inside each route handler,
    via check_csrf() below.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        session = read_session(request)
        if not session:
            if request.method == "GET":
                return RedirectResponse(f"/login?next={path}", status_code=303)
            return RedirectResponse("/login", status_code=303)

        request.state.session = session
        request.state.user = session["u"]
        return await call_next(request)


def check_csrf(request: Request, form) -> bool:
    """Call from inside a POST route handler, after `form = await request.form()`."""
    session = getattr(request.state, "session", None) or read_session(request)
    if not session:
        return False
    return verify_csrf(request, session, form.get(CSRF_FIELD, ""))
