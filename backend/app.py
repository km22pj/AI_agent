"""HTTP API and static file server for Healthlingo (Python standard library only)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import auth
from .ai import INTENSITIES, WEEKDAY_NAMES, AIError, AINotConfigured, AIService, build_ai_service
from . import progression, routine_agent
from .nutrition import SOURCES as NUTRITION_SOURCES
from .nutrition import compute_targets
from .config import PROJECT_ROOT, Settings, parse_emails, read_env_value
from .db import Database, dumps, loads, now_iso
from .logic import (
    COUNTED_STATUSES,
    MEAL_TYPES,
    ValidationError,
    ai_profile,
    body_metrics,
    build_challenges,
    date_range,
    day_totals,
    default_settings,
    evaluate_day,
    meal_signature,
    normalize_settings,
    parse_client_id,
    parse_date,
    training_schedule,
    workout_profile,
    parse_items,
    parse_manual_nutrition,
    parse_time,
    text,
    total_from_items,
)

log = logging.getLogger("healthlingo")

MAX_BODY = 20 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
MAX_THUMB = 1024 * 1024
PENDING_TIMEOUT = timedelta(minutes=3)
ACCESS_COOKIE = "hl_access"
ACCESS_DAYS = 180
INVITE_IN_LOG = re.compile(r"([?&]invite=)[^&\s\"]*")
STATIC_FILES = {
    "/": ("healthlingo_v1.html", "text/html; charset=utf-8"),
    "/index.html": ("healthlingo_v1.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    # Installable app (PWA)
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
    "/icons/icon-maskable-512.png": ("icons/icon-maskable-512.png", "image/png"),
    "/icons/apple-touch-icon.png": ("icons/apple-touch-icon.png", "image/png"),
}
# Served without the invite check: browsers fetch the manifest and icons without cookies,
# and none of these contain personal data.
PUBLIC_PATHS = {"/favicon.ico", "/manifest.webmanifest", "/sw.js", "/icons/icon-192.png", "/icons/icon-512.png",
                "/icons/icon-maskable-512.png", "/icons/apple-touch-icon.png"}
INVITE_FORM_MAX = 2048
CSP = (
    "default-src 'self'; "
    "script-src 'self' https://accounts.google.com/gsi/client; "
    "frame-src https://accounts.google.com/gsi/; "
    "connect-src 'self' https://accounts.google.com/gsi/; "
    "style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style; "
    "img-src 'self' blob: data: https://*.googleusercontent.com; "
    "object-src 'none'; base-uri 'none'; form-action 'self'"
)


class HTTPError(Exception):
    def __init__(self, status: int, code: str, message: str, retryable: bool | None = None, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.extra = extra or {}


def not_found(what: str = "기록") -> HTTPError:
    # Same answer whether the record does not exist or belongs to someone else.
    return HTTPError(404, "not_found", f"{what}을(를) 찾을 수 없어요.")


def ai_http_error(exc: AIError, extra: dict | None = None) -> HTTPError:
    status = 503 if exc.code == "ai_not_configured" else 502
    return HTTPError(status, exc.code, exc.message, retryable=exc.retryable, extra=extra)


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode_image(value, limit: int, field: str) -> tuple[bytes, str]:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} 데이터가 없어요.")
    if value.startswith("data:"):
        value = value.split(",", 1)[-1]
    if len(value) > limit * 4 // 3 + 16:
        raise ValidationError("사진 용량이 너무 커요. 다른 사진을 선택해 주세요.")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("사진 데이터를 읽을 수 없어요.") from exc
    if len(data) > limit:
        raise ValidationError("사진 용량이 너무 커요. 다른 사진을 선택해 주세요.")
    mime = sniff_image(data)
    if not mime:
        raise ValidationError("JPEG, PNG, WebP 형식의 사진만 저장할 수 있어요.")
    return data, mime


EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def read_cookie(headers: list[str], name: str) -> str | None:
    """Find one cookie by name, reading each `name=value` pair on its own.

    http.cookies.SimpleCookie gives up on the whole header when any cookie has a
    value it doesn't like - e.g. Google Sign-In's `g_state={"i_l":0,...}` - and
    then our session cookie is lost too. Other sites' cookies must not break login.
    """
    for header in headers:
        for part in header.split(";"):
            key, sep, value = part.strip().partition("=")
            if sep and key.strip() == name:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1]
                return value or None
    return None


class Request:
    def __init__(self, handler: "Handler", params: dict):
        self.handler = handler
        parts = urlsplit(handler.path)
        self.path = parts.path
        self.query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.params = params
        self.user = None
        self._body = None

    @property
    def headers(self):
        return self.handler.headers

    def cookie(self, name: str) -> str | None:
        return read_cookie(self.headers.get_all("Cookie") or [], name)

    def json(self) -> dict:
        if self._body is None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise HTTPError(413, "too_large", "요청 크기가 너무 커요.")
            raw = self.handler.rfile.read(length) if length else b""
            self.handler.body_consumed = True
            if not raw:
                self._body = {}
            else:
                try:
                    self._body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise HTTPError(400, "bad_json", "요청 형식이 올바르지 않아요.") from exc
            if not isinstance(self._body, dict):
                raise HTTPError(400, "bad_json", "요청 형식이 올바르지 않아요.")
        return self._body


class Response:
    def __init__(self, status: int = 200, body=None, raw: bytes | None = None, content_type: str = "application/json; charset=utf-8"):
        self.status = status
        self.body = body
        self.raw = raw
        self.content_type = content_type
        self.headers: list[tuple[str, str]] = []


INVITE_FORM_HTML = """<form method="post" action="/invite" style="margin:20px 0 0;text-align:left">
<label for="code" style="display:block;margin-bottom:6px;color:#4a5164;font-size:13px">초대 링크 또는 코드</label>
<input id="code" name="code" required autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="받은 링크를 그대로 붙여 넣어도 돼요"
 style="box-sizing:border-box;width:100%;min-height:44px;padding:10px 12px;border:1px solid #dcdbe6;border-radius:12px;font-size:15px">
<button type="submit" style="width:100%;min-height:44px;margin-top:10px;border:0;border-radius:12px;background:#5365c7;color:#fff;font-size:15px;font-weight:700">들어가기</button>
<p style="margin:10px 0 0;color:#9195a0;font-size:12px;line-height:1.5">홈 화면에 설치한 앱에서 이 화면이 보이면, 받은 초대 링크를 여기에 붙여 넣어 주세요.</p>
</form>"""


def gate_page(title: str, detail: str, form: bool = False) -> Response:
    """Plain page shown instead of the app to browsers without an invite."""
    body = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>Healthlingo — 초대 필요</title></head>
<body style="margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;box-sizing:border-box;
background:linear-gradient(112deg,#f1f0ff,#f5f3ff 48%,#fff1e9);color:#20283a;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR',sans-serif">
<main style="width:min(420px,100%);padding:32px 28px;border:1px solid #e8e7e2;border-radius:20px;background:#fff;
box-shadow:0 12px 32px rgb(34 40 56 / 5%);text-align:center">
<div style="display:inline-grid;width:44px;height:44px;place-items:center;border-radius:14px;background:#5365c7;color:#fff;font-size:20px">🔒</div>
<h1 style="margin:18px 0 8px;font-size:22px;letter-spacing:-.04em">{html.escape(title)}</h1>
<p style="margin:0;color:#72798a;font-size:14px;line-height:1.6">{html.escape(detail)}</p>
{INVITE_FORM_HTML if form else ""}
<p style="margin:18px 0 0;color:#9195a0;font-size:12px">Healthlingo</p>
</main></body></html>"""
    resp = Response(403, raw=body.encode("utf-8"), content_type="text/html; charset=utf-8")
    resp.headers.append(("Cache-Control", "no-store"))
    resp.headers.append(("X-Robots-Tag", "noindex"))
    return resp


class App:
    def __init__(self, settings: Settings, ai: AIService | None = None, key_store: auth.GoogleKeyStore | None = None):
        self.settings = settings
        self._started_code_stamp = self._code_stamp()  # to tell "unknown API" apart from "server not restarted"
        self.db = Database(settings.data_dir / "healthlingo.db")
        self.upload_dir = settings.data_dir / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.ai = ai if ai is not None else build_ai_service(settings)
        self.keys = key_store or auth.GoogleKeyStore()
        self._busy: set[str] = set()
        self._busy_lock = threading.Lock()
        self.today_override: date | None = None  # tests: pretend the server's date
        self.run_jobs_inline = False  # tests: run the AI routine review in the request instead of a thread
        self._pending_reviews: dict[str, tuple] = {}
        R = self._route
        self.routes = [
            R("GET", r"/api/config", self.get_config, auth_required=False),
            R("GET", r"/api/me", self.get_me),
            R("POST", r"/api/auth/google", self.login_google, auth_required=False),
            R("POST", r"/api/auth/dev", self.login_dev, auth_required=False),
            R("POST", r"/api/auth/logout", self.logout, auth_required=False),
            R("GET", r"/api/settings", self.get_settings),
            R("PUT", r"/api/settings", self.put_settings),
            R("POST", r"/api/photos", self.upload_photo),
            R("GET", r"/api/photos/(?P<id>[0-9a-f]{32})", self.get_photo),
            R("GET", r"/api/photos/(?P<id>[0-9a-f]{32})/thumb", self.get_photo_thumb),
            R("POST", r"/api/photos/(?P<id>[0-9a-f]{32})/recognize", self.recognize_photo),
            R("GET", r"/api/meals", self.list_meals),
            R("POST", r"/api/meals", self.create_meal),
            R("GET", r"/api/meals/(?P<id>[0-9a-f]{32})", self.get_meal),
            R("PATCH", r"/api/meals/(?P<id>[0-9a-f]{32})", self.update_meal),
            R("DELETE", r"/api/meals/(?P<id>[0-9a-f]{32})", self.delete_meal),
            R("POST", r"/api/meals/(?P<id>[0-9a-f]{32})/analyze", self.analyze_meal),
            R("GET", r"/api/days/(?P<date>\d{4}-\d{2}-\d{2})", self.get_day),
            R("PUT", r"/api/days/(?P<date>\d{4}-\d{2}-\d{2})", self.put_day),
            R("POST", r"/api/days/(?P<date>\d{4}-\d{2}-\d{2})/coach", self.coach_day),
            R("GET", r"/api/stats/range", self.stats_range),
            R("GET", r"/api/body-logs", self.list_body_logs),
            R("POST", r"/api/body-logs", self.create_body_log),
            R("GET", r"/api/body-logs/(?P<id>[0-9a-f]{32})", self.get_body_log),
            R("PATCH", r"/api/body-logs/(?P<id>[0-9a-f]{32})", self.update_body_log),
            R("DELETE", r"/api/body-logs/(?P<id>[0-9a-f]{32})", self.delete_body_log),
            R("POST", r"/api/activities", self.add_activity),
            R("DELETE", r"/api/activities", self.remove_activity),
            R("GET", r"/api/overview", self.overview),
            R("GET", r"/api/workout-plan", self.get_workout_plan),
            R("POST", r"/api/workout-plan", self.create_workout_plan),
            R("PUT", r"/api/exercise-checks", self.put_exercise_check),
            R("PUT", r"/api/workout-feedback", self.put_workout_feedback),
            R("GET", r"/api/nutrition-target", self.get_nutrition_target),
            R("POST", r"/api/nutrition-target/explain", self.explain_nutrition_target),
        ]
        self.secret = self._load_secret()
        self._allow_lock = threading.Lock()
        self._allow_mtime: float | None = None
        self.cleanup_orphan_photos()
        self.db.close_thread_connection()

    @staticmethod
    def _route(method, pattern, func, auth_required=True):
        return method, re.compile(pattern + r"$"), func, auth_required

    def _load_secret(self) -> bytes:
        """Random server key used to sign the invite-access cookie (created once, kept in data/)."""
        path = self.settings.data_dir / "secret.key"
        if path.is_file():
            value = path.read_text(encoding="ascii").strip()
            if len(value) >= 64:
                return bytes.fromhex(value)
        key = secrets.token_bytes(32)
        path.write_text(key.hex(), encoding="ascii")
        return key

    # ------------------------------------------------------------ invite link gate

    def _access_token(self) -> str:
        # Bound to the current invite code: changing the code locks out every earlier link.
        return hmac.new(self.secret, b"invite-access:" + self.settings.invite_code.encode("utf-8"), hashlib.sha256).hexdigest()

    def _has_access(self, handler: "Handler") -> bool:
        token = read_cookie(handler.headers.get_all("Cookie") or [], ACCESS_COOKIE)
        return bool(token) and hmac.compare_digest(token, self._access_token())

    def _grant_access(self) -> Response:
        """Remember this browser, then go to the app (and drop the code from the address bar)."""
        resp = Response(303, raw=b"", content_type="text/plain; charset=utf-8")
        resp.headers.append(("Location", "/"))
        resp.headers.append(("Referrer-Policy", "no-referrer"))
        resp.headers.append(("Set-Cookie", self._cookie(ACCESS_COOKIE, self._access_token(), ACCESS_DAYS * 86400)))
        return resp

    def _invite_form(self, handler: "Handler") -> Response:
        """The invite link or code pasted into the gate page. Needed for apps installed on iPhone,
        which keep their own cookies apart from Safari, so the link opened in Safari does not carry over."""
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = handler.rfile.read(length) if 0 < length <= INVITE_FORM_MAX else b""
        handler.body_consumed = length <= INVITE_FORM_MAX
        given = (parse_qs(raw.decode("utf-8", "replace")).get("code") or [""])[0].strip()
        if "invite=" in given:  # the whole link was pasted
            given = (parse_qs(urlsplit(given).query).get("invite") or [""])[0].strip()
        if given and hmac.compare_digest(given.encode("utf-8"), self.settings.invite_code.encode("utf-8")):
            return self._grant_access()
        log.warning("초대 코드 입력 실패")
        return gate_page("초대 링크가 올바르지 않아요.", "받은 초대 링크 전체나 코드를 다시 붙여 넣어 주세요.", form=True)

    def _invite_gate(self, handler: "Handler", method: str, path: str) -> Response | None:
        """Returns a response that stops the request, or None to let it through."""
        if not self.settings.invite_code:
            return None
        if method == "GET" and path in PUBLIC_PATHS:
            return None
        if method == "POST" and path == "/invite":
            return self._invite_form(handler)
        query = parse_qs(urlsplit(handler.path).query)
        if method == "GET" and "invite" in query and not path.startswith("/api/"):
            given = query["invite"][0]
            if hmac.compare_digest(given.encode("utf-8"), self.settings.invite_code.encode("utf-8")):
                return self._grant_access()
            return gate_page("초대 링크가 올바르지 않거나 더 이상 사용할 수 없어요.", "초대한 사람에게 새 링크를 요청해 주세요.", form=True)
        if self._has_access(handler):
            return None
        if path.startswith("/api/"):
            raise HTTPError(403, "invite_required", "초대 링크로 접속해야 이용할 수 있어요. 받은 링크를 다시 열어 주세요.")
        return gate_page("초대받은 사람만 이용할 수 있어요.", "받은 초대 링크를 그대로 열거나, 아래에 붙여 넣어 주세요.", form=True)

    # ------------------------------------------------------------ dispatch

    def handle(self, handler: "Handler", method: str) -> Response:
        path = urlsplit(handler.path).path
        gate = self._invite_gate(handler, method, path)
        if gate is not None:
            return gate
        if method == "GET" and path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            resp = Response(raw=(PROJECT_ROOT / name).read_bytes(), content_type=ctype)
            resp.headers.append(("Cache-Control", "no-cache"))
            return resp
        if method == "GET" and path == "/favicon.ico":
            return Response(204, raw=b"", content_type="image/x-icon")
        if not path.startswith("/api/"):
            raise HTTPError(404, "not_found", "페이지를 찾을 수 없어요.")
        allowed_methods = False
        for route_method, pattern, func, auth_required in self.routes:
            match = pattern.match(path)
            if not match:
                continue
            allowed_methods = True
            if route_method != method:
                continue
            req = Request(handler, match.groupdict())
            if method not in ("GET", "HEAD"):
                self._check_csrf(req)
            if auth_required:
                req.user = auth.session_user(self.db, req.cookie(auth.SESSION_COOKIE))
                if req.user is None:
                    raise HTTPError(401, "unauthorized", "로그인이 필요해요. 다시 로그인해 주세요.")
                if not self._email_allowed(req.user["email"], req.user["is_dev"]):
                    # Removed from the allow list: existing sessions stop working right away.
                    raise HTTPError(403, "not_invited", "이 계정은 더 이상 이용 권한이 없어요. 초대한 사람에게 문의해 주세요.")
            try:
                return func(req)
            except ValidationError as exc:
                raise HTTPError(400, "invalid", str(exc)) from exc
        if allowed_methods:
            raise HTTPError(405, "method_not_allowed", "지원하지 않는 요청이에요.")
        if self.code_changed_since_start():
            # The page (read from disk on every request) is newer than the running server code.
            log.warning("알 수 없는 API 요청(%s %s): 서버 코드가 바뀌었어요. 서버를 다시 시작하세요.", method, path)
            raise HTTPError(404, "restart_needed", "서버가 아직 예전 버전으로 실행 중이라 이 기능을 쓸 수 없어요. 서버를 다시 시작해 주세요.")
        raise HTTPError(404, "not_found", "요청한 기능을 찾을 수 없어요.")

    @staticmethod
    def _code_stamp() -> float:
        files = [*(PROJECT_ROOT / "backend").glob("*.py"), PROJECT_ROOT / "run.py"]
        return max((f.stat().st_mtime for f in files if f.is_file()), default=0.0)

    def code_changed_since_start(self) -> bool:
        try:
            return self._code_stamp() > self._started_code_stamp
        except OSError:
            return False

    def _check_csrf(self, req: Request) -> None:
        # Cross-site forms cannot set custom headers; same-origin fetch() always does.
        if req.headers.get("X-Healthlingo") != "1":
            raise HTTPError(403, "csrf", "허용되지 않은 요청이에요. 페이지를 새로고침해 주세요.")
        origin = req.headers.get("Origin")
        if origin:
            # Behind a tunnel the Host header may be rewritten to 127.0.0.1:8000,
            # so the configured public address is accepted as well.
            allowed = {req.headers.get("Host", ""), req.headers.get("X-Forwarded-Host", "")}
            if self.settings.public_url:
                allowed.add(urlsplit(self.settings.public_url).netloc)
            if urlsplit(origin).netloc.lower() not in {a.lower() for a in allowed if a}:
                raise HTTPError(403, "csrf", "허용되지 않은 요청 출처예요.")

    def _allowed_emails(self) -> list[str]:
        """Current allow list. Edits to .env take effect without restarting the server."""
        path = self.settings.env_file
        if path is None:
            return self.settings.allowed_emails
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return self.settings.allowed_emails
        with self._allow_lock:
            if mtime != self._allow_mtime:
                new = parse_emails(read_env_value(path, "HEALTHLINGO_ALLOWED_EMAILS") or "")
                if self._allow_mtime is not None and new != self.settings.allowed_emails:
                    log.info("Allow list reloaded from .env: %d account(s)", len(new))
                self.settings.allowed_emails = new
                self._allow_mtime = mtime
            return self.settings.allowed_emails

    def _email_allowed(self, email: str | None, is_dev) -> bool:
        allowed = self._allowed_emails()
        if not allowed or is_dev:
            return True
        return bool(email) and email.lower() in allowed

    # ------------------------------------------------------------ auth

    def get_config(self, req: Request) -> Response:
        return Response(body={
            "google_client_id": self.settings.google_client_id or None,
            "google_configured": bool(self.settings.google_client_id),
            "dev_login_enabled": self.settings.dev_login_allowed,
            "invite_only": bool(self.settings.invite_code),
            "allow_list": bool(self._allowed_emails()),
            "ai_configured": self.ai.configured,
            "ai_provider": self.settings.ai_provider,
            "ai_provider_name": "Google Gemini" if self.settings.ai_provider == "gemini" else "Anthropic Claude",
            "ai_key_name": self.settings.ai_key_name,
            "ai_model": self.settings.ai_model if self.ai.configured else None,
        })

    def _user_out(self, user) -> dict:
        return {
            "id": user["id"],
            "email": user["email"],
            "name": user["name"],
            "picture": user["picture"],
            "is_dev": bool(user["is_dev"]),
        }

    def get_me(self, req: Request) -> Response:
        return Response(body={"user": self._user_out(req.user), "settings": self._load_settings(req.user["id"])})

    def _start_session(self, user_id: int) -> Response:
        token, max_age = auth.create_session(self.db, user_id, self.settings.session_days)
        with self.db.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        resp = Response(body={"user": self._user_out(user), "settings": self._load_settings(user_id)})
        resp.headers.append(("Set-Cookie", self._cookie(auth.SESSION_COOKIE, token, max_age)))
        return resp

    def _cookie(self, name: str, value: str, max_age: int) -> str:
        parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
        if self.settings.secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    def login_google(self, req: Request) -> Response:
        if not self.settings.google_client_id:
            raise HTTPError(503, "google_not_configured", "Google 로그인이 아직 설정되지 않았어요. 서버에 GOOGLE_CLIENT_ID를 설정해 주세요.")
        credential = req.json().get("credential")
        try:
            claims = auth.verify_google_id_token(credential, self.settings.google_client_id, self.keys)
        except auth.AuthError as exc:
            raise HTTPError(401, "google_rejected", str(exc)) from exc
        allowed = self._allowed_emails()
        if allowed:
            email = str(claims.get("email") or "").strip().lower()
            verified = claims.get("email_verified") in (True, "true")
            if email not in allowed:
                # Shown in the server window so the owner knows exactly which address to add.
                log.warning("로그인 거절: %s 은(는) 허용 목록에 없어요. 추가하려면: .venv\\Scripts\\python.exe run.py --allow %s",
                            email or "(이메일 없음)", email or "<이메일>")
                raise HTTPError(403, "not_invited", f"이 Google 계정({email or '이메일 없음'})은 초대 목록에 없어요. 초대한 사람에게 이 이메일 주소를 알려 추가를 요청하거나, 초대받은 계정으로 로그인해 주세요.")
            if not verified:
                log.warning("로그인 거절: %s 은(는) Google에서 이메일 인증이 안 된 계정이에요.", email)
                raise HTTPError(403, "not_invited", f"이 Google 계정({email})은 이메일 인증이 완료되지 않았어요. Google 계정에서 이메일 인증을 마친 뒤 다시 로그인해 주세요.")
        user_id = auth.upsert_user(
            self.db,
            sub="google:" + str(claims["sub"]),
            email=claims.get("email"),
            name=claims.get("name") or claims.get("email"),
            picture=claims.get("picture"),
        )
        return self._start_session(user_id)

    def login_dev(self, req: Request) -> Response:
        if not self.settings.dev_login_allowed:
            raise HTTPError(404, "not_found", "요청한 기능을 찾을 수 없어요.")
        name = text(req.json().get("name") or "테스트 사용자", 40, "이름")
        slug = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
        user_id = auth.upsert_user(self.db, sub=f"dev:{slug}", email=None, name=f"{name} (개발용)", picture=None, is_dev=True)
        return self._start_session(user_id)

    def logout(self, req: Request) -> Response:
        auth.delete_session(self.db, req.cookie(auth.SESSION_COOKIE))
        resp = Response(body={"ok": True})
        resp.headers.append(("Set-Cookie", self._cookie(auth.SESSION_COOKIE, "", 0)))
        return resp

    # ------------------------------------------------------------ settings

    def _load_settings(self, user_id: int) -> dict:
        with self.db.connect() as conn:
            row = conn.execute("SELECT data FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return {**default_settings(), **(loads(row["data"]) if row else {})}

    def get_settings(self, req: Request) -> Response:
        return Response(body={"settings": self._load_settings(req.user["id"])})

    def put_settings(self, req: Request) -> Response:
        uid = req.user["id"]
        raw = dict(req.json() or {})
        today = self._client_today(raw.pop("today", None))
        with self.db.write_lock:
            current = self._load_settings(uid)
            updated = normalize_settings(raw, current, now_iso())
            routine_inputs_changed = self._workout_hash(workout_profile(current)) != self._workout_hash(workout_profile(updated))
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO user_settings (user_id, data, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                    (uid, dumps(updated), now_iso()),
                )
            changed = self._refresh_targets(uid, updated, today)
        # body values, goal or exercise plan changed: the AI reviews this week's routine on its own
        review = self._request_review(uid, date.fromisoformat(today), "settings") if routine_inputs_changed else False
        return Response(body={"settings": updated, "nutrition": self._nutrition_payload(uid, today), "nutrition_changed": changed,
                              "routine_review_started": review})

    # ------------------------------------------------------------ nutrition targets

    def server_today(self) -> date:
        return self.today_override or datetime.now(timezone.utc).date()

    def _client_today(self, value) -> str:
        """The user's local date (sent by the browser), if it is plausible; else the server's date."""
        server = self.server_today()
        try:
            day = date.fromisoformat(parse_date(value))
            if abs((day - server).days) <= 1:
                return day.isoformat()
        except (ValidationError, ValueError, TypeError):
            pass
        return server.isoformat()

    def _target_rows(self, conn, uid: int):
        return conn.execute("SELECT * FROM nutrition_targets WHERE user_id = ? ORDER BY effective_date, id", (uid,)).fetchall()

    def _ensure_legacy_target(self, conn, uid: int, settings: dict) -> None:
        """Targets typed in by hand before auto calculation keep applying to the days before the first auto target."""
        if conn.execute("SELECT 1 FROM nutrition_targets WHERE user_id = ?", (uid,)).fetchone():
            return
        legacy = settings.get("targets") or {}
        if any(legacy.get(k) is not None for k in ("kcal", "carbs_g", "protein_g", "fat_g")):
            conn.execute(
                "INSERT INTO nutrition_targets (user_id, effective_date, status, source, kcal, carbs_g, protein_g, fat_g, message, created_at) "
                "VALUES (?, '0001-01-01', 'ok', 'legacy', ?, ?, ?, ?, ?, ?)",
                (uid, legacy.get("kcal"), legacy.get("carbs_g"), legacy.get("protein_g"), legacy.get("fat_g"),
                 "자동 계산 이전에 직접 입력한 목표", settings.get("targets_confirmed_at") or now_iso()),
            )

    def _refresh_targets(self, uid: int, settings: dict, today: str) -> bool:
        """Recalculate when an input changed. A new row takes effect today; earlier days keep theirs.
        Returns True when a new target row was stored. Caller holds the write lock."""
        result = compute_targets(settings)
        with self.db.write_lock, self.db.connect() as conn:
            self._ensure_legacy_target(conn, uid, settings)
            rows = self._target_rows(conn, uid)
            latest = rows[-1] if rows else None
            if latest is not None and latest["input_hash"] == result["input_hash"]:
                return False
            if result["status"] != "ok" and (latest is None or latest["status"] != "ok"):
                return False  # nothing to replace; the setup notice is computed live
            ok = result["status"] == "ok"
            conn.execute(
                "INSERT INTO nutrition_targets (user_id, effective_date, status, source, kcal, carbs_g, protein_g, fat_g, inputs, basis, input_hash, message, created_at) "
                "VALUES (?, ?, ?, 'auto', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, today, result["status"],
                 result.get("kcal") if ok else None, result.get("carbs_g") if ok else None,
                 result.get("protein_g") if ok else None, result.get("fat_g") if ok else None,
                 dumps(result["inputs"]), dumps(result.get("basis")), result["input_hash"], result.get("message", ""), now_iso()),
            )
        return True

    @staticmethod
    def _target_out(row) -> dict | None:
        if row is None:
            return None
        return {
            "id": row["id"], "effective_date": row["effective_date"], "status": row["status"], "source": row["source"],
            "kcal": row["kcal"], "carbs_g": row["carbs_g"], "protein_g": row["protein_g"], "fat_g": row["fat_g"],
            "basis": loads(row["basis"]), "message": row["message"], "created_at": row["created_at"],
            "explanation": loads(row["explanation"]), "explanation_status": row["explanation_status"],
        }

    def _ensure_legacy(self, uid: int) -> None:
        settings = self._load_settings(uid)
        with self.db.write_lock, self.db.connect() as conn:
            self._ensure_legacy_target(conn, uid, settings)

    def _target_for(self, uid: int, day: str) -> dict | None:
        """The target that applied on `day` (None if none, or if it was a 'missing/unsupported' period)."""
        self._ensure_legacy(uid)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM nutrition_targets WHERE user_id = ? AND effective_date <= ? ORDER BY effective_date DESC, id DESC LIMIT 1",
                (uid, day)).fetchone()
        out = self._target_out(row)
        return out if out and out["status"] == "ok" else None

    @staticmethod
    def _targets_dict(target: dict | None) -> dict:
        keys = ("kcal", "carbs_g", "protein_g", "fat_g")
        return {k: (target[k] if target else None) for k in keys}

    def _nutrition_payload(self, uid: int, today: str) -> dict:
        self._ensure_legacy(uid)
        settings = self._load_settings(uid)
        live = compute_targets(settings)  # deterministic, no AI: tells what is missing right now
        with self.db.connect() as conn:
            rows = self._target_rows(conn, uid)
        history = [self._target_out(r) for r in rows if r["effective_date"] <= today]
        current = history[-1] if history else None
        return {
            "current": current if current and current["status"] == "ok" else None,
            "current_row": current,
            "setup": {"status": live["status"], "missing": live.get("missing", []), "message": live.get("message", "")},
            "history": list(reversed(history))[:10],
            "sources": NUTRITION_SOURCES,
        }

    def _today_nutrition(self, uid: int, today: str) -> dict:
        """For the Today tab: the same stored target and the same progress numbers as the food screen."""
        payload = self._nutrition_payload(uid, today)
        summary = self._day_payload(uid, today)["summary"]
        return {"current": payload["current"], "setup": payload["setup"], "progress": summary["progress"],
                "meal_count": summary["meal_count"], "uncounted": summary["uncounted"]}

    def get_nutrition_target(self, req: Request) -> Response:
        return Response(body=self._nutrition_payload(req.user["id"], self._client_today(req.query.get("today"))))

    def explain_nutrition_target(self, req: Request) -> Response:
        """AI writes a short explanation of the stored numbers. The numbers themselves never come from AI."""
        uid = req.user["id"]
        today = self._client_today((req.json() or {}).get("today"))
        payload = self._nutrition_payload(uid, today)
        target = payload["current"]
        if not target or target["source"] != "auto":
            raise ValidationError("설명할 자동 계산 목표가 없어요. 개인 설정을 먼저 입력해 주세요.")
        settings = self._load_settings(uid)
        key = f"explain:{uid}"
        self._claim(key)
        try:
            try:
                explanation = self.ai.explain_targets({
                    "targets": self._targets_dict(target),
                    "calculation": target["basis"],
                    "goal": settings.get("goal_type") or None,
                    "goal_note": settings.get("goal_note") or None,
                    "cautions_from_user": (settings.get("body") or {}).get("condition_note") or None,
                })
                status = "ok"
            except AIError as exc:
                with self.db.write_lock, self.db.connect() as conn:
                    conn.execute("UPDATE nutrition_targets SET explanation_status = 'failed' WHERE id = ? AND user_id = ?", (target["id"], uid))
                raise ai_http_error(exc) from exc
        finally:
            self._release(key)
        with self.db.write_lock, self.db.connect() as conn:
            conn.execute("UPDATE nutrition_targets SET explanation = ?, explanation_status = ? WHERE id = ? AND user_id = ?",
                         (dumps(explanation), status, target["id"], uid))
        return Response(body=self._nutrition_payload(uid, today))

    # ------------------------------------------------------------ photos

    def _photo_row(self, conn, uid: int, photo_id: str):
        return conn.execute("SELECT * FROM photos WHERE id = ? AND user_id = ?", (photo_id, uid)).fetchone()

    def upload_photo(self, req: Request) -> Response:
        uid = req.user["id"]
        body = req.json()
        client_id = parse_client_id(body.get("client_id"))
        kind = body.get("kind")
        if kind not in ("meal", "body"):
            raise ValidationError("사진 종류가 올바르지 않아요.")
        image, mime = decode_image(body.get("image"), MAX_IMAGE, "사진")
        thumb, thumb_mime = decode_image(body.get("thumb"), MAX_THUMB, "미리보기 사진")
        with self.db.write_lock:
            with self.db.connect() as conn:
                existing = conn.execute("SELECT * FROM photos WHERE user_id = ? AND client_id = ?", (uid, client_id)).fetchone()
            if existing:
                return Response(200, {"photo": self._photo_out(existing)})
            photo_id = uuid.uuid4().hex
            folder = self.upload_dir / f"u{uid}"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{photo_id}.{EXT[mime]}"
            thumb_path = folder / f"{photo_id}_thumb.{EXT[thumb_mime]}"
            path.write_bytes(image)
            thumb_path.write_bytes(thumb)
            try:
                with self.db.connect() as conn:
                    conn.execute(
                        "INSERT INTO photos (id, user_id, client_id, kind, path, thumb_path, mime, size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (photo_id, uid, client_id, kind, str(path.relative_to(self.upload_dir)), str(thumb_path.relative_to(self.upload_dir)), mime, len(image), now_iso()),
                    )
                    row = self._photo_row(conn, uid, photo_id)
            except Exception:
                path.unlink(missing_ok=True)
                thumb_path.unlink(missing_ok=True)
                raise
        return Response(201, {"photo": self._photo_out(row)})

    @staticmethod
    def _photo_out(row) -> dict:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "url": f"/api/photos/{row['id']}",
            "thumb_url": f"/api/photos/{row['id']}/thumb",
            "recognition": loads(row["recognition"]),
        }

    def _serve_photo(self, req: Request, thumb: bool) -> Response:
        with self.db.connect() as conn:
            row = self._photo_row(conn, req.user["id"], req.params["id"])
        if row is None:
            raise not_found("사진")
        rel = row["thumb_path"] if thumb else row["path"]
        file_path = self.upload_dir / rel
        if not file_path.is_file():
            raise not_found("사진 파일")
        data = file_path.read_bytes()
        resp = Response(raw=data, content_type=sniff_image(data) or "application/octet-stream")
        resp.headers.append(("Cache-Control", "private, no-store"))
        return resp

    def get_photo(self, req: Request) -> Response:
        return self._serve_photo(req, thumb=False)

    def get_photo_thumb(self, req: Request) -> Response:
        return self._serve_photo(req, thumb=True)

    def recognize_photo(self, req: Request) -> Response:
        uid = req.user["id"]
        with self.db.connect() as conn:
            row = self._photo_row(conn, uid, req.params["id"])
        if row is None or row["kind"] != "meal":
            raise not_found("음식 사진")
        key = f"photo:{row['id']}"
        self._claim(key)
        try:
            data = (self.upload_dir / row["path"]).read_bytes()
            try:
                result = self.ai.recognize(data, row["mime"])
            except AIError as exc:
                raise ai_http_error(exc, {"photo_id": row["id"]}) from exc
            result["photo_id"] = row["id"]
            result["analyzed_at"] = now_iso()
            with self.db.write_lock, self.db.connect() as conn:
                conn.execute("UPDATE photos SET recognition = ? WHERE id = ? AND user_id = ?", (dumps(result), row["id"], uid))
        finally:
            self._release(key)
        return Response(body={"recognition": result})

    def _delete_photo(self, conn, uid: int, photo_id: str | None) -> None:
        if not photo_id:
            return
        row = self._photo_row(conn, uid, photo_id)
        if row is None:
            return
        conn.execute("DELETE FROM photos WHERE id = ? AND user_id = ?", (photo_id, uid))
        for rel in (row["path"], row["thumb_path"]):
            (self.upload_dir / rel).unlink(missing_ok=True)

    def _claim_photo(self, conn, uid: int, photo_id, kind: str, owner_table: str, owner_id: str | None = None) -> str | None:
        if photo_id in (None, ""):
            return None
        if not isinstance(photo_id, str) or not re.fullmatch(r"[0-9a-f]{32}", photo_id):
            raise ValidationError("사진 정보가 올바르지 않아요.")
        row = self._photo_row(conn, uid, photo_id)
        if row is None or row["kind"] != kind:
            raise ValidationError("사진을 찾을 수 없어요. 사진을 다시 선택해 주세요.")
        used = conn.execute(
            f"SELECT id FROM {owner_table} WHERE user_id = ? AND photo_id = ? AND id IS NOT ?", (uid, photo_id, owner_id)
        ).fetchone()
        if used:
            raise ValidationError("이미 다른 기록에 사용된 사진이에요.")
        return photo_id

    def cleanup_orphan_photos(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self.db.write_lock, self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM photos p WHERE created_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM meals m WHERE m.photo_id = p.id) "
                "AND NOT EXISTS (SELECT 1 FROM body_logs b WHERE b.photo_id = p.id)",
                (cutoff,),
            ).fetchall()
            for row in rows:
                self._delete_photo(conn, row["user_id"], row["id"])

    # ------------------------------------------------------------ busy guard

    def _claim(self, key: str) -> None:
        with self._busy_lock:
            if key in self._busy:
                raise HTTPError(409, "busy", "이미 분석 중이에요. 잠시만 기다려 주세요.")
            self._busy.add(key)

    def _release(self, key: str) -> None:
        with self._busy_lock:
            self._busy.discard(key)

    # ------------------------------------------------------------ meals

    def _meal_out(self, row) -> dict:
        status = row["nutrition_status"]
        error = row["analysis_error"]
        if status == "pending" and row["analysis_started_at"]:
            started = datetime.strptime(row["analysis_started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - started > PENDING_TIMEOUT:
                status, error = "failed", "분석이 중단됐어요. 다시 시도해 주세요."
        nutrition = loads(row["nutrition"])
        analysis = loads(row["analysis"])
        current = status in COUNTED_STATUSES
        return {
            "id": row["id"],
            "client_id": row["client_id"],
            "date": row["date"],
            "time": row["time"],
            "meal_type": row["meal_type"],
            "meal_type_label": MEAL_TYPES.get(row["meal_type"], row["meal_type"]),
            "items": loads(row["items"]),
            "memo": row["memo"],
            "photo": {"id": row["photo_id"], "url": f"/api/photos/{row['photo_id']}", "thumb_url": f"/api/photos/{row['photo_id']}/thumb"} if row["photo_id"] else None,
            "nutrition_status": status,
            # Outdated or failed analyses are never presented as current numbers.
            "nutrition": nutrition if current else None,
            "analysis": analysis if status == "ai" else None,
            "analysis_error": error if status == "failed" else None,
            "analyzed_at": row["analyzed_at"] if status == "ai" else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _get_meal_row(self, conn, uid: int, meal_id: str):
        row = conn.execute("SELECT * FROM meals WHERE id = ? AND user_id = ?", (meal_id, uid)).fetchone()
        if row is None:
            raise not_found("식사 기록")
        return row

    def _meals_for(self, uid: int, start: str, end: str) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM meals WHERE user_id = ? AND date BETWEEN ? AND ? ORDER BY date, COALESCE(time, '99:99'), created_at",
                (uid, start, end),
            ).fetchall()
        return [self._meal_out(r) for r in rows]

    def list_meals(self, req: Request) -> Response:
        if "date" in req.query:
            day = parse_date(req.query["date"])
            start = end = day
        else:
            start = parse_date(req.query.get("from"), "시작일")
            end = parse_date(req.query.get("to"), "종료일")
            date_range(start, end)
        return Response(body={"meals": self._meals_for(req.user["id"], start, end)})

    def get_meal(self, req: Request) -> Response:
        with self.db.connect() as conn:
            row = self._get_meal_row(conn, req.user["id"], req.params["id"])
        return Response(body={"meal": self._meal_out(row)})

    @staticmethod
    def _meal_type(value) -> str:
        if value not in MEAL_TYPES:
            raise ValidationError("식사 구분(아침·점심·저녁·간식)을 선택해 주세요.")
        return value

    def create_meal(self, req: Request) -> Response:
        uid = req.user["id"]
        body = req.json()
        client_id = parse_client_id(body.get("client_id"))
        day = parse_date(body.get("date"))
        when = parse_time(body.get("time"))
        meal_type = self._meal_type(body.get("meal_type"))
        items = parse_items(body.get("items"))
        memo = text(body.get("memo"), 500, "메모")
        manual = parse_manual_nutrition(body.get("manual_nutrition"))
        stamp = now_iso()
        with self.db.write_lock, self.db.connect() as conn:
            existing = conn.execute("SELECT * FROM meals WHERE user_id = ? AND client_id = ?", (uid, client_id)).fetchone()
            if existing:
                return Response(200, {"meal": self._meal_out(existing), "duplicate": True})
            photo_id = self._claim_photo(conn, uid, body.get("photo_id"), "meal", "meals")
            meal_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO meals (id, user_id, client_id, date, time, meal_type, items, memo, photo_id, nutrition, nutrition_status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (meal_id, uid, client_id, day, when, meal_type, dumps(items), memo, photo_id,
                 dumps(manual) if manual else None, "user" if manual else "none", stamp, stamp),
            )
            row = self._get_meal_row(conn, uid, meal_id)
        return Response(201, {"meal": self._meal_out(row)})

    def update_meal(self, req: Request) -> Response:
        uid = req.user["id"]
        body = req.json()
        with self.db.write_lock, self.db.connect() as conn:
            row = self._get_meal_row(conn, uid, req.params["id"])
            fields: dict = {}
            if "date" in body:
                fields["date"] = parse_date(body["date"])
            if "time" in body:
                fields["time"] = parse_time(body["time"])
            if "meal_type" in body:
                fields["meal_type"] = self._meal_type(body["meal_type"])
            if "memo" in body:
                fields["memo"] = text(body["memo"], 500, "메모")
            status = row["nutrition_status"]
            if "items" in body:
                items = parse_items(body["items"])
                if meal_signature(items) != meal_signature(loads(row["items"])):
                    fields["items"] = dumps(items)
                    if status in ("ai", "pending"):
                        status = "stale"  # keep the old numbers out of totals until re-analyzed
                    elif status == "failed":
                        status = "none"
            if "manual_nutrition" in body:
                manual = parse_manual_nutrition(body["manual_nutrition"])
                if manual is not None:
                    fields["nutrition"] = dumps(manual)
                    status = "user"
                elif status == "user":
                    fields["nutrition"] = None
                    status = "none"
            fields["nutrition_status"] = status
            fields["updated_at"] = now_iso()
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE meals SET {assignments} WHERE id = ? AND user_id = ?", (*fields.values(), row["id"], uid))
            row = self._get_meal_row(conn, uid, row["id"])
        return Response(body={"meal": self._meal_out(row)})

    def delete_meal(self, req: Request) -> Response:
        uid = req.user["id"]
        with self.db.write_lock, self.db.connect() as conn:
            row = self._get_meal_row(conn, uid, req.params["id"])
            conn.execute("DELETE FROM meals WHERE id = ? AND user_id = ?", (row["id"], uid))
            self._delete_photo(conn, uid, row["photo_id"])
        return Response(body={"ok": True, "deleted": row["id"], "date": row["date"]})

    def _day_complete(self, conn, uid: int, day: str) -> bool:
        meta = conn.execute("SELECT complete FROM day_meta WHERE user_id = ? AND date = ?", (uid, day)).fetchone()
        return bool(meta and meta["complete"])

    def analyze_meal(self, req: Request) -> Response:
        uid = req.user["id"]
        meal_id = req.params["id"]
        if not self.ai.configured:
            raise ai_http_error(AINotConfigured(self.ai.key_name))
        key = f"meal:{meal_id}"
        self._claim(key)
        try:
            with self.db.write_lock, self.db.connect() as conn:
                row = self._get_meal_row(conn, uid, meal_id)
                previous_status = row["nutrition_status"]
                conn.execute(
                    "UPDATE meals SET nutrition_status = 'pending', analysis_started_at = ?, analysis_error = NULL WHERE id = ? AND user_id = ?",
                    (now_iso(), meal_id, uid),
                )
                others = conn.execute(
                    "SELECT * FROM meals WHERE user_id = ? AND date = ? AND id != ?", (uid, row["date"], meal_id)
                ).fetchall()
                complete = self._day_complete(conn, uid, row["date"])
            meal = self._meal_out(row)
            other_meals = [self._meal_out(o) for o in others]
            agg = day_totals(other_meals)
            day_context = {
                "date": row["date"],
                "day_complete": complete,
                "other_meals_recorded": [{"meal_type": m["meal_type_label"], "items": [i["name"] for i in m["items"]], "analyzed": m["nutrition"] is not None} for m in other_meals],
                "other_meals_totals": {k: v["value"] for k, v in agg["totals"].items()},
                "unanalyzed_other_meals": agg["uncounted"],
            }
            settings, _ = self._settings_for_day(uid, row["date"])
            try:
                result = self.ai.estimate(meal, ai_profile(settings), day_context)
            except AIError as exc:
                with self.db.write_lock, self.db.connect() as conn:
                    restore = previous_status if previous_status in ("ai", "user") else "failed"
                    conn.execute(
                        "UPDATE meals SET nutrition_status = ?, analysis_error = ? WHERE id = ? AND user_id = ?",
                        (restore, exc.message, meal_id, uid),
                    )
                    row = conn.execute("SELECT * FROM meals WHERE id = ? AND user_id = ?", (meal_id, uid)).fetchone()
                raise ai_http_error(exc, {"meal": self._meal_out(row) if row else None}) from exc

            with self.db.write_lock, self.db.connect() as conn:
                latest = conn.execute("SELECT * FROM meals WHERE id = ? AND user_id = ?", (meal_id, uid)).fetchone()
                if latest is None:
                    raise not_found("식사 기록")
                changed = meal_signature(loads(latest["items"])) != meal_signature(meal["items"])
                if changed:
                    # The meal was edited while the AI was working; the result no longer matches.
                    conn.execute("UPDATE meals SET nutrition_status = 'stale' WHERE id = ? AND user_id = ?", (meal_id, uid))
            if changed:
                raise HTTPError(409, "changed", "분석 중에 기록이 수정됐어요. 다시 분석해 주세요.", retryable=True)
            with self.db.write_lock, self.db.connect() as conn:
                nutrition = {
                    "source": "ai",
                    "estimated": True,
                    "items": result["items"],
                    "total": total_from_items(result["items"]),
                    "confidence": result["confidence"],
                }
                analysis = {
                    "feedback": result["feedback"],
                    "assumptions": result["assumptions"],
                    "questions": result["questions"],
                    "model": result.get("model") or self.settings.ai_model,
                }
                conn.execute(
                    "UPDATE meals SET nutrition = ?, analysis = ?, nutrition_status = 'ai', analysis_error = NULL, analyzed_at = ? WHERE id = ? AND user_id = ?",
                    (dumps(nutrition), dumps(analysis), now_iso(), meal_id, uid),
                )
                row = self._get_meal_row(conn, uid, meal_id)
            return Response(body={"meal": self._meal_out(row)})
        finally:
            self._release(key)

    # ------------------------------------------------------------ days & coaching

    def _coach_hash(self, meals: list[dict], complete: bool, settings: dict) -> str:
        material = {
            "meals": [[m["id"], m["updated_at"], m["nutrition_status"], m["nutrition"]] for m in meals],
            "complete": complete,
            "profile": ai_profile(settings),
        }
        return hashlib.sha256(dumps(material).encode("utf-8")).hexdigest()

    def _settings_for_day(self, uid: int, day: str) -> tuple[dict, dict | None]:
        """Settings with `targets` replaced by the stored target that applied on that day."""
        settings = self._load_settings(uid)
        target = self._target_for(uid, day)
        return {**settings, "targets": self._targets_dict(target)}, target

    def _day_payload(self, uid: int, day: str) -> dict:
        meals = self._meals_for(uid, day, day)
        settings, target = self._settings_for_day(uid, day)
        with self.db.connect() as conn:
            meta = conn.execute("SELECT * FROM day_meta WHERE user_id = ? AND date = ?", (uid, day)).fetchone()
        complete = bool(meta and meta["complete"])
        coach = None
        if meta and meta["coach"]:
            coach = loads(meta["coach"])
            coach["created_at"] = meta["coach_created_at"]
            coach["stale"] = meta["coach_input_hash"] != self._coach_hash(meals, complete, settings)
        return {
            "date": day,
            "meals": meals,
            "summary": evaluate_day(meals, settings, complete),
            "complete": complete,
            "coach": coach,
            "target": {k: target[k] for k in ("source", "effective_date", "created_at")} if target else None,
        }

    def get_day(self, req: Request) -> Response:
        day = parse_date(req.params["date"])
        return Response(body=self._day_payload(req.user["id"], day))

    def put_day(self, req: Request) -> Response:
        uid = req.user["id"]
        day = parse_date(req.params["date"])
        complete = bool(req.json().get("complete"))
        with self.db.write_lock, self.db.connect() as conn:
            conn.execute(
                "INSERT INTO day_meta (user_id, date, complete) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, date) DO UPDATE SET complete = excluded.complete",
                (uid, day, 1 if complete else 0),
            )
        return Response(body=self._day_payload(uid, day))

    def coach_day(self, req: Request) -> Response:
        uid = req.user["id"]
        day = parse_date(req.params["date"])
        payload = self._day_payload(uid, day)
        if not payload["meals"]:
            raise ValidationError("이 날짜에는 식사 기록이 없어 피드백을 만들 수 없어요. 먼저 식사를 기록해 주세요.")
        settings, _ = self._settings_for_day(uid, day)
        summary = payload["summary"]
        ai_input = {
            "date": day,
            "day_complete": payload["complete"],
            "user_profile": ai_profile(settings),
            "meals": [
                {
                    "meal_type": m["meal_type_label"],
                    "time": m["time"],
                    "items": m["items"],
                    "analyzed": m["nutrition"] is not None,
                    "nutrition_total": m["nutrition"]["total"] if m["nutrition"] else None,
                }
                for m in payload["meals"]
            ],
            "totals_of_analyzed_meals": {k: v["value"] for k, v in summary["totals"].items()},
            "unanalyzed_meal_count": summary["uncounted"],
            "app_evaluation": [m["text"] for m in summary["messages"]],
        }
        key = f"coach:{uid}:{day}"
        self._claim(key)
        try:
            try:
                coach = self.ai.coach(ai_input, settings.get("allergies", []) + settings.get("avoid_foods", []))
            except AIError as exc:
                raise ai_http_error(exc) from exc
        finally:
            self._release(key)
        coach["model"] = coach.get("model") or self.settings.ai_model
        with self.db.write_lock, self.db.connect() as conn:
            conn.execute(
                "INSERT INTO day_meta (user_id, date, complete, coach, coach_input_hash, coach_created_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, date) DO UPDATE SET coach = excluded.coach, coach_input_hash = excluded.coach_input_hash, coach_created_at = excluded.coach_created_at",
                (uid, day, 1 if payload["complete"] else 0, dumps(coach),
                 self._coach_hash(payload["meals"], payload["complete"], settings), now_iso()),
            )
        return Response(body=self._day_payload(uid, day))

    def stats_range(self, req: Request) -> Response:
        uid = req.user["id"]
        start = parse_date(req.query.get("from"), "시작일")
        end = parse_date(req.query.get("to"), "종료일")
        days = date_range(start, end, max_days=92)
        meals = self._meals_for(uid, start, end)
        by_day: dict[str, list] = {d: [] for d in days}
        for meal in meals:
            by_day.setdefault(meal["date"], []).append(meal)
        series = []
        for d in days:
            agg = day_totals(by_day[d])
            series.append({
                "date": d,
                "meal_count": agg["meal_count"],
                "uncounted": agg["uncounted"],
                "totals": {k: v["value"] for k, v in agg["totals"].items()},
                "targets": self._targets_dict(self._target_for(uid, d)),  # each day keeps the target it had
            })
        latest = series[-1]["targets"] if series else self._targets_dict(None)
        return Response(body={"from": start, "to": end, "days": series, "targets": latest})

    # ------------------------------------------------------------ body logs

    def _body_out(self, row) -> dict:
        return {
            "id": row["id"],
            "client_id": row["client_id"],
            "date": row["date"],
            "note": row["note"],
            "photo": {"id": row["photo_id"], "url": f"/api/photos/{row['photo_id']}", "thumb_url": f"/api/photos/{row['photo_id']}/thumb"} if row["photo_id"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _get_body_row(self, conn, uid: int, log_id: str):
        row = conn.execute("SELECT * FROM body_logs WHERE id = ? AND user_id = ?", (log_id, uid)).fetchone()
        if row is None:
            raise not_found("바디 로그")
        return row

    def list_body_logs(self, req: Request) -> Response:
        uid = req.user["id"]
        order = "DESC" if req.query.get("order", "desc") != "asc" else "ASC"
        clauses, args = ["user_id = ?"], [uid]
        if req.query.get("from"):
            clauses.append("date >= ?")
            args.append(parse_date(req.query["from"], "시작일"))
        if req.query.get("to"):
            clauses.append("date <= ?")
            args.append(parse_date(req.query["to"], "종료일"))
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM body_logs WHERE {' AND '.join(clauses)} ORDER BY date {order}, created_at {order}", args
            ).fetchall()
        return Response(body={"body_logs": [self._body_out(r) for r in rows]})

    def get_body_log(self, req: Request) -> Response:
        with self.db.connect() as conn:
            row = self._get_body_row(conn, req.user["id"], req.params["id"])
        return Response(body={"body_log": self._body_out(row)})

    def create_body_log(self, req: Request) -> Response:
        uid = req.user["id"]
        body = req.json()
        client_id = parse_client_id(body.get("client_id"))
        day = parse_date(body.get("date"))
        note = text(body.get("note"), 1000, "메모")
        stamp = now_iso()
        with self.db.write_lock, self.db.connect() as conn:
            existing = conn.execute("SELECT * FROM body_logs WHERE user_id = ? AND client_id = ?", (uid, client_id)).fetchone()
            if existing:
                return Response(200, {"body_log": self._body_out(existing), "duplicate": True})
            photo_id = self._claim_photo(conn, uid, body.get("photo_id"), "body", "body_logs")
            if not photo_id and not note:
                raise ValidationError("사진이나 메모 중 하나는 남겨 주세요.")
            log_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO body_logs (id, user_id, client_id, date, note, photo_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (log_id, uid, client_id, day, note, photo_id, stamp, stamp),
            )
            row = self._get_body_row(conn, uid, log_id)
        return Response(201, {"body_log": self._body_out(row)})

    def update_body_log(self, req: Request) -> Response:
        uid = req.user["id"]
        body = req.json()
        with self.db.write_lock, self.db.connect() as conn:
            row = self._get_body_row(conn, uid, req.params["id"])
            fields: dict = {}
            if "date" in body:
                fields["date"] = parse_date(body["date"])
            if "note" in body:
                fields["note"] = text(body["note"], 1000, "메모")
            old_photo = row["photo_id"]
            new_photo = old_photo
            if "photo_id" in body:
                new_photo = self._claim_photo(conn, uid, body["photo_id"], "body", "body_logs", owner_id=row["id"])
                fields["photo_id"] = new_photo
            note = fields.get("note", row["note"])
            if not new_photo and not note:
                raise ValidationError("사진이나 메모 중 하나는 남겨 주세요.")
            fields["updated_at"] = now_iso()
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(f"UPDATE body_logs SET {assignments} WHERE id = ? AND user_id = ?", (*fields.values(), row["id"], uid))
            if old_photo and old_photo != new_photo:
                self._delete_photo(conn, uid, old_photo)
            row = self._get_body_row(conn, uid, row["id"])
        return Response(body={"body_log": self._body_out(row)})

    def delete_body_log(self, req: Request) -> Response:
        uid = req.user["id"]
        with self.db.write_lock, self.db.connect() as conn:
            row = self._get_body_row(conn, uid, req.params["id"])
            conn.execute("DELETE FROM body_logs WHERE id = ? AND user_id = ?", (row["id"], uid))
            self._delete_photo(conn, uid, row["photo_id"])
        return Response(body={"ok": True, "deleted": row["id"], "date": row["date"]})

    # ------------------------------------------------------------ activities & overview

    def _activity_input(self, req: Request) -> tuple[str, str]:
        source = req.json() if req.handler.command == "POST" else req.query
        day = parse_date(source.get("date"))
        kind = source.get("kind")
        if kind not in ("warmup", "movement"):
            raise ValidationError("활동 종류가 올바르지 않아요.")
        return day, kind

    def add_activity(self, req: Request) -> Response:
        day, kind = self._activity_input(req)
        with self.db.write_lock, self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO activities (user_id, date, kind, created_at) VALUES (?, ?, ?, ?)",
                (req.user["id"], day, kind, now_iso()),
            )
        return Response(body={"ok": True, "date": day, "kind": kind, "done": True})

    def remove_activity(self, req: Request) -> Response:
        day, kind = self._activity_input(req)
        with self.db.write_lock, self.db.connect() as conn:
            conn.execute("DELETE FROM activities WHERE user_id = ? AND date = ? AND kind = ?", (req.user["id"], day, kind))
        return Response(body={"ok": True, "date": day, "kind": kind, "done": False})

    # ------------------------------------------------------------ workout routine (saved per week)

    REVIEW_STALE = timedelta(minutes=4)
    TRIGGER_TEXT = {"feedback": "피드백 저장", "settings": "개인 설정 변경", "new_week": "새 주 준비"}

    def _workout_hash(self, profile: dict) -> str:
        return hashlib.sha256(dumps(profile).encode("utf-8")).hexdigest()

    def _migrate_legacy_plan(self, conn, uid: int) -> None:
        """Routines saved before weekly storage: move the single saved plan into the week it was made in."""
        if conn.execute("SELECT 1 FROM week_plans WHERE user_id = ? LIMIT 1", (uid,)).fetchone():
            return
        row = conn.execute("SELECT * FROM workout_plans WHERE user_id = ?", (uid,)).fetchone()
        if row is None:
            return
        monday = progression.week_start(date.fromisoformat(row["created_at"][:10])).isoformat()
        conn.execute("INSERT OR IGNORE INTO week_plans (user_id, week_start, plan, input_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (uid, monday, row["plan"], row["input_hash"], row["created_at"], row["created_at"]))

    def _week_row(self, uid: int, monday: date):
        with self.db.write_lock, self.db.connect() as conn:
            self._migrate_legacy_plan(conn, uid)
            return conn.execute("SELECT * FROM week_plans WHERE user_id = ? AND week_start = ?", (uid, monday.isoformat())).fetchone()

    def _checks_between(self, uid: int, start: date, end: date) -> set[tuple[str, str]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT date, item FROM exercise_checks WHERE user_id = ? AND date BETWEEN ? AND ?",
                                (uid, start.isoformat(), end.isoformat())).fetchall()
        return {(r["date"], r["item"]) for r in rows}

    def _feedback_rows(self, uid: int, start: date, end: date) -> list:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM workout_feedback WHERE user_id = ? AND date BETWEEN ? AND ? ORDER BY date",
                                (uid, start.isoformat(), end.isoformat())).fetchall()

    @staticmethod
    def _feedback_out(row) -> dict | None:
        if row is None:
            return None
        return {"id": row["id"], "date": row["date"], "text": row["text"], "label": row["text"] or "피드백 없음",
                "interpretation": loads(row["interpretation"]), "limits": loads(row["limits"]) or [],
                "intensity_applied": bool(row["intensity_applied"]), "updated_at": row["updated_at"]}

    @staticmethod
    def _day_progress(plan: dict, monday: date, checks: set) -> dict:
        """{"<weekday>": {"done": n, "planned": m}} from the saved completion checks."""
        out = {}
        for s in plan.get("sessions", []):
            wd = s.get("weekday")
            if wd is None:
                continue
            day = (monday + timedelta(days=wd)).isoformat()
            keys = [progression.exercise_key(i, ex["name"]) for i, ex in enumerate(s["exercises"])]
            out[str(wd)] = {"done": sum(1 for k in keys if (day, k) in checks), "planned": len(keys)}
        return out

    def _review_out(self, row) -> dict:
        if row is None:
            return {"status": "none", "message": "", "trigger": "", "trigger_label": ""}
        status, message = row["review_status"], row["review_message"]
        if status == "running" and row["review_started_at"]:
            started = datetime.strptime(row["review_started_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - started > self.REVIEW_STALE:
                status, message = "failed", "AI 자동 조정이 중단됐어요. 기존 계획과 기록은 그대로예요."
        return {"status": status, "message": message, "trigger": row["review_trigger"],
                "trigger_label": self.TRIGGER_TEXT.get(row["review_trigger"], "")}

    def _set_review(self, uid: int, monday: date, status: str, message: str, trigger: str) -> None:
        with self.db.write_lock, self.db.connect() as conn:
            conn.execute(
                "UPDATE week_plans SET review_status = ?, review_message = ?, review_trigger = ?, "
                "review_started_at = CASE WHEN ? = 'running' THEN ? ELSE review_started_at END WHERE user_id = ? AND week_start = ?",
                (status, message, trigger, status, now_iso(), uid, monday.isoformat()))

    def _week_finished(self, uid: int, row, today: date) -> bool:
        """Nothing left to do this week: no session after today, and today's session (if any) fully done."""
        sessions = {s.get("weekday"): s for s in loads(row["plan"]).get("sessions", []) if s.get("weekday") is not None}
        wd = today.weekday()
        if any(k > wd for k in sessions):
            return False
        if wd not in sessions:
            return True
        p = self._day_progress({"sessions": [sessions[wd]]}, progression.week_start(today), self._checks_between(uid, today, today)).get(str(wd))
        return bool(p) and p["done"] >= p["planned"]

    def _start_week_from_previous(self, uid: int, monday: date, today: date) -> bool:
        """A new week starts as a copy of the latest earlier week; the AI then reviews it in the background."""
        with self.db.write_lock, self.db.connect() as conn:
            if conn.execute("SELECT 1 FROM week_plans WHERE user_id = ? AND week_start = ?", (uid, monday.isoformat())).fetchone():
                return False
            prev = conn.execute("SELECT * FROM week_plans WHERE user_id = ? AND week_start < ? ORDER BY week_start DESC LIMIT 1",
                                (uid, monday.isoformat())).fetchone()
            if prev is None:
                return False
            plan = loads(prev["plan"])
            if plan.get("format", 1) < 3:
                return False
            copy = {k: plan[k] for k in ("sessions", "summary", "cautions", "model", "format") if k in plan}
            # Days changed only because of temporary feedback ("내일만 20분", "이번 주만 바빠요") go back to normal.
            by_day = {s["weekday"]: s for s in copy.get("sessions", []) if s.get("weekday") is not None}
            restored = []
            for key, original in (plan.get("temporary_originals") or {}).items():
                wd = int(key)
                if original:
                    by_day[wd] = original
                else:
                    by_day.pop(wd, None)
                restored.append(wd)
            copy["sessions"] = [by_day[k] for k in sorted(by_day)]
            copy.update({"copied_from": prev["week_start"], "adjustments": [], "agent": {}, "rest_overrides": {},
                         "restored_temporary_days": sorted(restored)})
            now = now_iso()
            conn.execute(
                "INSERT INTO week_plans (user_id, week_start, plan, input_hash, created_at, updated_at, review_status, review_trigger, review_started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', 'new_week', ?)",
                (uid, monday.isoformat(), dumps(copy), prev["input_hash"], now, now, now))
        self._start_agent_job(uid, monday, "new_week", today)
        return True

    def _ensure_week(self, uid: int, today: date) -> None:
        """This week always has a routine once the user has one; next week is prepared when this week is done."""
        monday = progression.week_start(today)
        row = self._week_row(uid, monday)
        if row is None:
            self._start_week_from_previous(uid, monday, today)
            return
        nxt = monday + timedelta(days=7)
        if row["review_status"] != "running" and self._week_row(uid, nxt) is None and self._week_finished(uid, row, today):
            self._start_week_from_previous(uid, nxt, today)

    # --- automatic AI review ---------------------------------------------------

    def _start_agent_job(self, uid: int, monday: date, trigger: str, today: date) -> None:
        if self.run_jobs_inline:
            self._run_agent_job(uid, monday, trigger, today)
        else:
            threading.Thread(target=self._run_agent_job, args=(uid, monday, trigger, today), name="routine-agent", daemon=True).start()

    def _request_review(self, uid: int, today: date, trigger: str) -> bool:
        """Review this week's routine after a feedback or settings change. Returns True when started."""
        monday = progression.week_start(today)
        row = self._week_row(uid, monday)
        if row is None or loads(row["plan"]).get("format", 1) < 3:
            return False
        self._set_review(uid, monday, "running", "", trigger)
        self._start_agent_job(uid, monday, trigger, today)
        return True

    def _run_agent_job(self, uid: int, monday: date, trigger: str, today: date) -> str:
        key = f"agent:{uid}:{monday.isoformat()}"
        with self._busy_lock:
            if key in self._busy:  # already running: run once more afterwards with the newest inputs
                self._pending_reviews[key] = (trigger, today)
                return "queued"
            self._busy.add(key)
        try:
            status = self._review_week(uid, monday, trigger, today)
        finally:
            with self._busy_lock:
                self._busy.discard(key)
                again = self._pending_reviews.pop(key, None)
        return self._run_agent_job(uid, monday, *again) if again else status

    def _review_week(self, uid: int, monday: date, trigger: str, today: date) -> str:
        try:
            if not self.ai.configured:
                self._set_review(uid, monday, "failed", "AI가 설정되지 않아 자동 조정을 하지 못했어요. 기존 계획을 그대로 써요.", trigger)
                return "failed"
            prepared = self._agent_inputs(uid, monday, today)
            if prepared is None:
                self._set_review(uid, monday, "idle", "", trigger)
                return "idle"
            context, base_row, base, check_ctx, sig = prepared
            if sig == (base.get("agent") or {}).get("last_sig"):
                self._set_review(uid, monday, "up_to_date", "새로 반영할 피드백이나 설정 변경이 없어 계획을 그대로 유지해요.", trigger)
                return "up_to_date"
            result = routine_agent.run_agent(
                self.ai, context,
                check=lambda proposal: routine_agent.check_proposal(base, proposal, check_ctx),
                history=lambda weeks: self._agent_history(uid, monday, weeks))
            self._apply_agent_result(uid, monday, base_row, result, trigger, sig, check_ctx)
            return "done"
        except AIError as exc:
            log.warning("루틴 자동 조정 실패 (%s): %s", exc.code, exc.message)
            message = exc.message if "기존 계획" in exc.message else f"{exc.message} 루틴은 기존 계획 그대로예요."
            self._set_review(uid, monday, "failed", message, trigger)
            return "failed"
        except Exception:  # never lose the plan because of a bug in the review
            log.exception("루틴 자동 조정 오류")
            self._set_review(uid, monday, "failed", "자동 조정 중 오류가 나서 기존 계획과 기록을 그대로 유지했어요.", trigger)
            return "failed"

    def _agent_inputs(self, uid: int, monday: date, today: date):
        row = self._week_row(uid, monday)
        if row is None:
            return None
        base = loads(row["plan"])
        if base.get("format", 1) < 3:
            return None
        settings = self._load_settings(uid)
        profile = workout_profile(settings)
        schedule = training_schedule(settings)
        week_dates = [(monday + timedelta(days=i)).isoformat() for i in range(7)]
        this_week = monday == progression.week_start(today)
        first_editable = today.weekday() if this_week else (0 if monday > today else 7)
        agent_state = base.get("agent") or {}
        mode = "new_week" if base.get("copied_from") and not agent_state.get("runs") else "this_week"
        last_monday = date.fromisoformat(base["copied_from"]) if base.get("copied_from") else monday - timedelta(days=7)
        checks = self._checks_between(uid, monday, monday + timedelta(days=6))
        last_row = self._week_row(uid, last_monday)
        last_checks = self._checks_between(uid, last_monday, last_monday + timedelta(days=6))
        feedback = [self._feedback_out(r) for r in self._feedback_rows(uid, last_monday, monday + timedelta(days=6))]

        sessions = {s["weekday"]: s for s in base.get("sessions", []) if s.get("weekday") is not None}
        locked: dict[int, dict] = {}
        if this_week and today.weekday() in sessions:
            for i, ex in enumerate(sessions[today.weekday()]["exercises"]):
                if (today.isoformat(), progression.exercise_key(i, ex["name"])) in checks:
                    locked.setdefault(today.weekday(), {})[i] = ex
        temp_limits: dict[str, int] = {}

        def add_limit(iso, minutes):
            if iso in week_dates and minutes:
                temp_limits[iso] = min(temp_limits.get(iso, minutes), int(minutes))
        for f in feedback:
            for lim in f["limits"]:
                add_limit(lim.get("date"), lim.get("max_minutes"))
            it = f["interpretation"] or {}
            if it.get("max_minutes"):
                for iso in week_dates:
                    if (it.get("applies_from") or "") <= iso <= (it.get("applies_to") or ""):
                        add_limit(iso, it["max_minutes"])
        recent_from = (today - timedelta(days=6)).isoformat()
        pain_recent = any(recent_from <= f["date"] <= today.isoformat()
                          and (routine_agent.has_pain(f["text"]) or (f["interpretation"] or {}).get("kind") == "pain") for f in feedback)
        records_sig = hashlib.sha256(dumps({"week": sorted(checks), "last": sorted(last_checks) if mode == "new_week" else [],
                                            "days_passed": first_editable}).encode("utf-8")).hexdigest()
        base_loads: dict[str, float] = {}
        for s in sessions.values():
            for ex in s["exercises"]:
                if ex.get("weight_kg"):
                    base_loads[ex["name"]] = max(base_loads.get(ex["name"], 0), ex["weight_kg"])
        levels = [INTENSITIES.index(s["intensity"]) for s in sessions.values() if s.get("intensity") in INTENSITIES]
        user_rest = set(schedule["rest_days"]) if schedule["source"] == "rest_days" else set()
        check_ctx = {
            "week_dates": week_dates, "first_editable": first_editable, "user_rest_days": user_rest,
            "training_days": set(schedule["training_days"]) if schedule["source"] == "rest_days" else set(),
            "minutes_limit": settings.get("minutes_per_session"), "temp_limits": temp_limits, "locked": locked,
            "weighted_ok": bool(set(settings.get("equipment") or []) & routine_agent.LOAD_GEAR) or bool((settings.get("equipment_note") or "").strip()),
            "pain_recent": pain_recent, "feedback_ids": {f["id"] for f in feedback},
            "consumed_feedback": {f["id"] for f in feedback if f["intensity_applied"]},
            "records_new": records_sig != agent_state.get("intensity_records_sig"), "records_sig": records_sig,
            "body_weight": (settings.get("body") or {}).get("weight_kg"),
            "base_loads": base_loads, "base_max_intensity": max(levels) if levels else None,
        }

        def day_entry(wd: int) -> dict:
            iso, s = week_dates[wd], sessions.get(wd)
            status = ("past" if wd < today.weekday() else "today" if wd == today.weekday() else "future") if this_week else "future"
            entry = {"weekday": WEEKDAY_NAMES[wd], "date": iso, "status": status, "editable": wd >= first_editable,
                     "user_rest_weekday": wd in user_rest, "rest_override": str(wd) in (base.get("rest_overrides") or {}),
                     "time_limit_minutes": temp_limits.get(iso), "session": None}
            if s:
                exercises = [{"index": i, "name": ex["name"], "amount": ex.get("amount", ""), "weight_kg": ex.get("weight_kg"),
                              "weight_note": ex.get("weight_note", ""), "done": (iso, progression.exercise_key(i, ex["name"])) in checks}
                             for i, ex in enumerate(s["exercises"])]
                entry["session"] = {"title": s["title"], "focus": s.get("focus", ""), "minutes": s["minutes"], "intensity": s["intensity"], "exercises": exercises}
                entry["done_count"] = sum(1 for e in exercises if e["done"])
                entry["planned_count"] = len(exercises)
            return entry

        last_week = None
        if last_row is not None:
            prog = self._day_progress(loads(last_row["plan"]), last_monday, last_checks)
            planned = sum(v["planned"] for v in prog.values())
            last_week = {"week_start": last_monday.isoformat(),
                         "days": [{"weekday": WEEKDAY_NAMES[int(k)], **v} for k, v in sorted(prog.items())],
                         "completion_rate": round(sum(v["done"] for v in prog.values()) / planned, 2) if planned else None}
        context = {
            "mode": mode, "today": today.isoformat(), "today_weekday": WEEKDAY_NAMES[today.weekday()], "week_start": monday.isoformat(),
            "editable_from": WEEKDAY_NAMES[first_editable] if first_editable < 7 else "없음",
            "user": profile, "week": [day_entry(wd) for wd in range(7)],
            "feedback": [{"id": f["id"], "date": f["date"], "text": f["label"], "interpretation": f["interpretation"],
                          "intensity_already_applied": f["intensity_applied"], "server_time_limits": f["limits"]} for f in feedback],
            "recent_pain": pain_recent, "last_week_results": last_week,
            "restored_after_temporary_feedback": [WEEKDAY_NAMES[d] for d in base.get("restored_temporary_days", [])] if mode == "new_week" else [],
            "recent_adjustments": [{"at": a.get("at"), "decision": a.get("decision"), "summary": a.get("summary")}
                                   for a in (base.get("adjustments") or []) if a.get("source") == "agent"][:3],
        }
        sig = hashlib.sha256(dumps({"profile": profile, "feedback": [(f["id"], f["text"], f["updated_at"]) for f in feedback],
                                    "records": records_sig, "mode": mode}).encode("utf-8")).hexdigest()
        return context, row, base, check_ctx, sig

    def _agent_history(self, uid: int, monday: date, weeks: int) -> dict:
        """Tool for the AI: earlier weeks' plans, completion per day and feedback."""
        out = []
        for k in range(1, weeks + 1):
            m = monday - timedelta(days=7 * k)
            end = m + timedelta(days=6)
            feedback = [{"date": f["date"], "text": f["label"]} for f in map(self._feedback_out, self._feedback_rows(uid, m, end))]
            row = self._week_row(uid, m)
            if row is None:
                out.append({"week_start": m.isoformat(), "plan": None, "feedback": feedback})
                continue
            plan = loads(row["plan"])
            prog = self._day_progress(plan, m, self._checks_between(uid, m, end))
            out.append({
                "week_start": m.isoformat(),
                "sessions": [{"weekday": WEEKDAY_NAMES[s["weekday"]], "title": s["title"], "intensity": s["intensity"], "minutes": s["minutes"],
                              "exercises": [f"{e['name']} {e.get('amount', '')}" + (f" {e['weight_kg']}kg" if e.get("weight_kg") else "") for e in s["exercises"]],
                              **prog.get(str(s["weekday"]), {})}
                             for s in plan.get("sessions", []) if s.get("weekday") is not None],
                "feedback": feedback,
                "adjustments": [a.get("summary") for a in plan.get("adjustments", []) if a.get("source") == "agent"][:3],
            })
        return {"weeks": out}

    def _apply_agent_result(self, uid: int, monday: date, base_row, result: dict, trigger: str, sig: str, check_ctx: dict) -> None:
        proposal, candidate = result["proposal"], result["candidate"]
        decision = proposal["decision"]
        profile_hash = self._workout_hash(workout_profile(self._load_settings(uid)))
        with self.db.write_lock, self.db.connect() as conn:
            row = conn.execute("SELECT * FROM week_plans WHERE user_id = ? AND week_start = ?", (uid, monday.isoformat())).fetchone()
            if row is None or row["updated_at"] != base_row["updated_at"]:
                raise AIError("AI가 검토하는 동안 루틴이 바뀌어 이번 조정은 반영하지 않았어요.", True, "agent_conflict")
            # an exercise checked while the AI was working must still be in place
            by_day = {s["weekday"]: s for s in candidate["sessions"]}
            for r in conn.execute("SELECT date, item FROM exercise_checks WHERE user_id = ? AND date BETWEEN ? AND ?",
                                  (uid, check_ctx["week_dates"][0], check_ctx["week_dates"][6])).fetchall():
                wd = check_ctx["week_dates"].index(r["date"])
                if wd < check_ctx["first_editable"]:
                    continue
                idx, _, name = r["item"].partition(":")
                s = by_day.get(wd)
                if not idx.isdigit() or not s or int(idx) >= len(s["exercises"]) or s["exercises"][int(idx)]["name"] != name:
                    raise AIError("AI가 검토하는 동안 완료한 운동이 생겨 이번 조정은 반영하지 않았어요.", True, "agent_conflict")
            agent = {**(candidate.get("agent") or {}), "last_sig": sig, "runs": (candidate.get("agent") or {}).get("runs", 0) + 1}
            if decision in ("increase", "decrease") and proposal.get("based_on_records"):
                agent["intensity_records_sig"] = check_ctx["records_sig"]
            summary = [s.strip()[:160] for s in proposal.get("summary") or [] if isinstance(s, str) and s.strip()][:4] or ["지금 계획을 그대로 유지해요."]
            entry = {"source": "agent", "at": now_iso(), "trigger": trigger, "decision": decision, "summary": summary,
                     "changed_days": sorted(result["changed"]), "model": result["model"] or self.settings.ai_model,
                     "turns": result["turns"], "tools": result["tools"], "failed_submits": result["failed_submits"]}
            # Remember what a day looked like before a temporary change, so next week starts from the normal version.
            temporary = set(check_ctx["temp_limits"])
            for note in proposal.get("feedback_notes") or []:
                if note.get("scope") in ("day", "this_week"):
                    temporary |= {iso for iso in check_ctx["week_dates"] if (note.get("applies_from") or "") <= iso <= (note.get("applies_to") or "")}
            base_sessions = {s["weekday"]: s for s in loads(base_row["plan"]).get("sessions", []) if s.get("weekday") is not None}
            originals = dict(candidate.get("temporary_originals") or {})
            for wd in result["changed"]:
                if check_ctx["week_dates"][wd] in temporary:
                    originals.setdefault(str(wd), base_sessions.get(wd))
                else:
                    originals.pop(str(wd), None)  # changed again for a lasting reason: the new version is the normal one
            plan = {**candidate, "agent": agent, "temporary_originals": originals,
                    "adjustments": [entry] + (candidate.get("adjustments") or [])[:9]}
            conn.execute(
                "UPDATE week_plans SET plan = ?, input_hash = ?, updated_at = ?, review_status = 'done', review_message = ?, review_trigger = ? "
                "WHERE user_id = ? AND week_start = ?",
                (dumps(plan), profile_hash, now_iso(), " ".join(summary)[:300], trigger, uid, monday.isoformat()))
            for note in proposal.get("feedback_notes") or []:
                conn.execute("UPDATE workout_feedback SET interpretation = ? WHERE id = ? AND user_id = ?", (dumps(note), note["feedback_id"], uid))
            if decision in ("increase", "decrease"):
                for fid in proposal.get("based_on_feedback_ids") or []:
                    conn.execute("UPDATE workout_feedback SET intensity_applied = 1 WHERE id = ? AND user_id = ?", (fid, uid))

    # --- payloads and endpoints --------------------------------------------------

    def _workout_payload(self, uid: int, today: date | None = None) -> dict:
        today = today or self.server_today()
        self._ensure_week(uid, today)
        settings = self._load_settings(uid)
        profile = workout_profile(settings)
        monday = progression.week_start(today)
        row = self._week_row(uid, monday)
        plan = None
        if row:
            stored = loads(row["plan"])
            stored.pop("agent", None)
            stored.pop("temporary_originals", None)
            checks = self._checks_between(uid, monday, monday + timedelta(days=6))
            plan = {**stored, "created_at": row["created_at"], "updated_at": row["updated_at"], "week_start": row["week_start"],
                    "stale": row["input_hash"] != self._workout_hash(profile), "progress": self._day_progress(stored, monday, checks)}
        nxt = self._week_row(uid, monday + timedelta(days=7))
        fb = self._feedback_rows(uid, today, today)
        return {"body": body_metrics(settings), "plan": plan, "schedule": training_schedule(settings), "week_start": monday.isoformat(),
                "review": self._review_out(row), "feedback_today": self._feedback_out(fb[0] if fb else None),
                "next_week": {"week_start": (monday + timedelta(days=7)).isoformat(), "ready": nxt is not None, "review": self._review_out(nxt)}}

    def _today_workout(self, uid: int, today: date) -> dict:
        """Today's part of the weekly routine, for the Today tab."""
        payload = self._workout_payload(uid, today)
        plan, schedule = payload["plan"], payload["schedule"]
        weekday = today.weekday()
        latest = next((a for a in (plan or {}).get("adjustments", []) if a.get("source") == "agent"), None)
        out = {"weekday": weekday, "status": "no_plan", "session": None, "stale": bool(plan and plan["stale"]), "rest_source": schedule["source"],
               "review": payload["review"], "feedback": payload["feedback_today"], "latest_adjustment": latest}
        # A rest day chosen by the user always wins, even over an older routine.
        if schedule["source"] == "rest_days" and weekday in schedule["rest_days"]:
            return {**out, "status": "rest"}
        if plan is None:
            return out
        if plan.get("format", 1) < 3:
            return {**out, "status": "outdated"}
        session = next((s for s in plan["sessions"] if s.get("weekday") == weekday), None)
        if session:
            exercises = [{**ex, "key": progression.exercise_key(i, ex["name"])} for i, ex in enumerate(session["exercises"])]
            keys = {ex["key"] for ex in exercises}
            with self.db.connect() as conn:
                rows = conn.execute("SELECT item FROM exercise_checks WHERE user_id = ? AND date = ?", (uid, today.isoformat())).fetchall()
            checked = [r["item"] for r in rows if r["item"] in keys]
            return {**out, "status": "workout", "session": {**session, "exercises": exercises}, "checked": checked}
        if weekday in schedule["rest_days"] or str(weekday) in (plan.get("rest_overrides") or {}):
            return {**out, "status": "rest"}
        return {**out, "status": "unscheduled"}  # the routine was made before today became a workout day

    def get_workout_plan(self, req: Request) -> Response:
        today = date.fromisoformat(self._client_today(req.query.get("today")))
        return Response(body=self._workout_payload(req.user["id"], today))

    def _keep_finished_days(self, uid: int, old: dict, new: dict, today: date) -> None:
        """'루틴 다시 받기': days before today keep their old sessions, and today keeps its session if anything is done."""
        wd = today.weekday()
        old_s = {s["weekday"]: s for s in old.get("sessions", []) if s.get("weekday") is not None}
        new_s = {s["weekday"]: s for s in new.get("sessions", []) if s.get("weekday") is not None}
        for d in range(wd):
            if d in old_s:
                new_s[d] = old_s[d]
            else:
                new_s.pop(d, None)
        if wd in old_s and self._checks_between(uid, today, today):
            new_s[wd] = old_s[wd]
        new["sessions"] = [new_s[k] for k in sorted(new_s)]

    def create_workout_plan(self, req: Request) -> Response:
        uid = req.user["id"]
        settings = self._load_settings(uid)
        metrics = body_metrics(settings)
        if metrics["height_cm"] is None or metrics["weight_kg"] is None:
            raise ValidationError("운동 루틴을 추천하려면 개인 설정에서 키와 몸무게를 먼저 입력해 주세요.")
        profile = workout_profile(settings)
        today = date.fromisoformat(self._client_today((req.json() or {}).get("today")))
        monday = progression.week_start(today)
        row = self._week_row(uid, monday)
        current = loads(row["plan"]) if row else None
        last_monday = monday - timedelta(days=7)
        last_row = self._week_row(uid, last_monday)
        previous = current or (loads(last_row["plan"]) if last_row else None)
        ai_input = dict(profile)  # the hash below uses `profile` only, so a review never marks the routine stale
        # Last week's results, unless this week already had an automatic review (no double adjustment).
        if last_row is not None and not (current and (current.get("agent") or {}).get("runs")):
            last_plan = loads(last_row["plan"])
            if last_plan.get("format", 1) >= 3:
                review = progression.review_week(last_plan, date.fromisoformat(last_row["created_at"][:10]),
                                                 self._checks_between(uid, last_monday, monday - timedelta(days=1)), last_monday)
                if review:
                    ai_input["progress_review"] = {"last_week_completion_rate": review["rate"], "done": review["done"], "planned": review["planned"]}
        if previous and previous.get("format", 1) >= 3:
            ai_input["previous_plan"] = [
                {"weekday": s.get("weekday"), "title": s["title"], "intensity": s["intensity"],
                 "exercises": [{"name": e["name"], "amount": e["amount"], "weight_kg": e.get("weight_kg")} for e in s["exercises"]]}
                for s in previous["sessions"]]
        key = f"workout:{uid}"
        self._claim(key)
        try:
            try:
                plan = self.ai.workout(ai_input)
            except AIError as exc:
                raise ai_http_error(exc) from exc
        finally:
            self._release(key)
        plan["model"] = plan.get("model") or self.settings.ai_model
        notes = ["새 주간 루틴을 받았어요."]
        if previous and previous.get("format", 1) >= 3:
            capped = progression.cap_against_previous(plan, previous, "increase")
            if capped:
                notes.append(f"안전을 위해 중량·강도 {capped}곳을 이전 루틴보다 한 단계 안으로 맞췄어요.")
        if current:
            self._keep_finished_days(uid, current, plan, today)
            if today.weekday():
                notes.append("지난 요일과 이미 완료한 오늘 운동은 그대로 두었어요.")
        plan["agent"] = {}
        plan["adjustments"] = [{"source": "regenerate", "at": now_iso(), "summary": notes}] + ((current or {}).get("adjustments") or [])[:9]
        now = now_iso()
        with self.db.write_lock, self.db.connect() as conn:
            conn.execute(
                "INSERT INTO week_plans (user_id, week_start, plan, input_hash, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, week_start) DO UPDATE SET plan = excluded.plan, input_hash = excluded.input_hash, "
                "updated_at = excluded.updated_at, review_status = 'idle', review_message = '', review_trigger = ''",
                (uid, monday.isoformat(), dumps(plan), self._workout_hash(profile), now, now))
        return Response(body=self._workout_payload(uid, today))

    def put_workout_feedback(self, req: Request) -> Response:
        """'오늘 운동은 어떠셨나요?' Optional; an empty text is saved as 피드백 없음. Saving a changed text starts the AI review."""
        uid = req.user["id"]
        body = req.json()
        day = date.fromisoformat(self._client_today(body.get("date")))
        value = text(body.get("text"), 200, "피드백")
        limits = routine_agent.parse_time_limits(value, day)
        now = now_iso()
        with self.db.write_lock, self.db.connect() as conn:
            row = conn.execute("SELECT * FROM workout_feedback WHERE user_id = ? AND date = ?", (uid, day.isoformat())).fetchone()
            changed = row is None or row["text"] != value
            if row is None:
                conn.execute("INSERT INTO workout_feedback (user_id, date, text, limits, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                             (uid, day.isoformat(), value, dumps(limits), now, now))
            elif changed:  # a new text is new information: read it again, and it may change intensity once
                conn.execute("UPDATE workout_feedback SET text = ?, limits = ?, interpretation = NULL, intensity_applied = 0, updated_at = ? WHERE id = ?",
                             (value, dumps(limits), now, row["id"]))
        started = self._request_review(uid, day, "feedback") if changed else False
        fb = self._feedback_rows(uid, day, day)
        return Response(body={"feedback": self._feedback_out(fb[0] if fb else None), "changed": changed, "review_started": started,
                              "review": self._review_out(self._week_row(uid, progression.week_start(day)))})

    def put_exercise_check(self, req: Request) -> Response:
        """Mark one exercise of the day's routine done / not done. A day with at least one
        done exercise counts as a movement day (streak, challenges, monthly stats)."""
        uid = req.user["id"]
        body = req.json()
        day = parse_date(body.get("date"))
        item, done = body.get("item"), body.get("done")
        if not isinstance(item, str) or not isinstance(done, bool):
            raise ValidationError("운동 완료 정보가 올바르지 않아요.")
        tw = self._today_workout(uid, date.fromisoformat(day))
        if tw["status"] != "workout" or item not in {ex["key"] for ex in tw["session"]["exercises"]}:
            raise ValidationError("오늘 루틴에 없는 운동이에요. 화면을 새로고침해 주세요.")
        now = now_iso()
        with self.db.write_lock, self.db.connect() as conn:
            if done:
                conn.execute("INSERT OR IGNORE INTO exercise_checks (user_id, date, item, created_at) VALUES (?, ?, ?, ?)", (uid, day, item, now))
            else:
                conn.execute("DELETE FROM exercise_checks WHERE user_id = ? AND date = ? AND item = ?", (uid, day, item))
            count = conn.execute("SELECT COUNT(*) FROM exercise_checks WHERE user_id = ? AND date = ?", (uid, day)).fetchone()[0]
            if count:
                conn.execute("INSERT OR IGNORE INTO activities (user_id, date, kind, created_at) VALUES (?, ?, 'movement', ?)", (uid, day, now))
            else:
                conn.execute("DELETE FROM activities WHERE user_id = ? AND date = ? AND kind = 'movement'", (uid, day))
        tw = self._today_workout(uid, date.fromisoformat(day))
        return Response(body={"checked": tw.get("checked", [])})

    # ------------------------------------------------------------ overview

    @staticmethod
    def _streaks(recorded: list[str], today: date) -> dict:
        """Consecutive record days. The current run counts up to today, or up to yesterday
        when today has no record yet (so the run is not shown as broken before the day ends)."""
        days = sorted({date.fromisoformat(d) for d in recorded if d <= today.isoformat()})
        best = run = 0
        prev = None
        for d in days:
            run = run + 1 if prev is not None and (d - prev).days == 1 else 1
            best = max(best, run)
            prev = d
        day_set = set(days)
        today_recorded = today in day_set
        cursor = today if today_recorded else today - timedelta(days=1)
        current = 0
        while cursor in day_set:
            current += 1
            cursor -= timedelta(days=1)
        return {"current": current, "best": best, "today_recorded": today_recorded}

    def overview(self, req: Request) -> Response:
        uid = req.user["id"]
        today = date.fromisoformat(parse_date(req.query.get("today"), "오늘 날짜"))
        week_start = today - timedelta(days=today.weekday())
        week = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
        month_start = today.replace(day=1).isoformat()
        next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_end = (next_month - timedelta(days=1)).isoformat()
        with self.db.connect() as conn:
            meal_dates = [r["date"] for r in conn.execute("SELECT date FROM meals WHERE user_id = ?", (uid,))]
            body_dates = [r["date"] for r in conn.execute("SELECT date FROM body_logs WHERE user_id = ?", (uid,))]
            acts = conn.execute("SELECT date, kind FROM activities WHERE user_id = ?", (uid,)).fetchall()
            has_plan = conn.execute("SELECT 1 FROM week_plans WHERE user_id = ? UNION SELECT 1 FROM workout_plans WHERE user_id = ?", (uid, uid)).fetchone() is not None
        act_dates = [a["date"] for a in acts]
        recorded = sorted(set(meal_dates) | set(body_dates) | set(act_dates))
        recorded_set = set(recorded)
        today_s = today.isoformat()
        in_month = lambda d: month_start <= d <= month_end  # noqa: E731

        # Badges are derived from the stored records only.
        restarted = any(
            (date.fromisoformat(b) - date.fromisoformat(a)).days >= 4 for a, b in zip(recorded, recorded[1:])
        )
        weeks: dict[tuple, int] = {}
        for d in recorded:
            weeks[date.fromisoformat(d).isocalendar()[:2]] = weeks.get(date.fromisoformat(d).isocalendar()[:2], 0) + 1
        routine = any(count >= 3 for count in weeks.values())
        streak = self._streaks(recorded, today)
        week_set = set(week)
        challenges = build_challenges(self._load_settings(uid), {
            "total_record_days": sum(1 for d in recorded if d <= today_s),
            "best_streak": streak["best"],
            "movement_week": sum(1 for a in acts if a["kind"] == "movement" and a["date"] in week_set),
            "meals_week": sum(1 for d in meal_dates if d in week_set),
            "body_month": sum(1 for d in body_dates if in_month(d)),
            "days_month": sum(1 for d in recorded if in_month(d)),
            "has_plan": has_plan,
        })

        return Response(body={
            "today": today_s,
            "week": [
                {
                    "date": d,
                    "has_record": d in recorded_set,
                    "meals": meal_dates.count(d),
                    "body_logs": body_dates.count(d),
                    "activities": act_dates.count(d),
                }
                for d in week
            ],
            "week_record_days": sum(1 for d in week if d in recorded_set),
            "streak": streak,
            "challenges": challenges,
            "today_workout": self._today_workout(uid, today),
            "nutrition": self._today_nutrition(uid, today_s),
            "missions": {
                "warmup": any(a["date"] == today_s and a["kind"] == "warmup" for a in acts),
                "movement": any(a["date"] == today_s and a["kind"] == "movement" for a in acts),
                "record": today_s in set(meal_dates) or today_s in set(body_dates),
            },
            "month": {
                "start": month_start,
                "end": month_end,
                "movement": sum(1 for a in acts if a["kind"] == "movement" and in_month(a["date"])),
                "meals": sum(1 for d in meal_dates if in_month(d)),
                "body_logs": sum(1 for d in body_dates if in_month(d)),
                "record_days": sum(1 for d in recorded if in_month(d)),
            },
            "badges": {
                "first_record": bool(recorded),
                "restart": restarted,
                "routine": routine,
            },
        })


class Handler(BaseHTTPRequestHandler):
    server_version = "Healthlingo/1.0"
    protocol_version = "HTTP/1.1"
    app: App  # set by make_server

    def log_message(self, fmt, *args):  # keep request logs short, never bodies or invite codes
        log.info("%s %s", self.address_string(), INVITE_IN_LOG.sub(r"\1***", fmt % args))

    def _dispatch(self, method: str):
        try:
            try:
                resp = self.app.handle(self, method)
            except HTTPError as exc:
                body = {"error": {"code": exc.code, "message": exc.message}}
                if exc.retryable is not None:
                    body["error"]["retryable"] = exc.retryable
                body.update(exc.extra)
                resp = Response(exc.status, body)
            except Exception:
                log.exception("Unhandled error on %s %s", method, urlsplit(self.path).path)
                resp = Response(500, {"error": {"code": "server_error", "message": "서버에서 문제가 생겼어요. 잠시 후 다시 시도해 주세요.", "retryable": True}})
            self._send(resp)
        finally:
            self.app.db.close_thread_connection()

    def _drain_body(self) -> None:
        # Answering before reading the request body (401/403/503 checks run first) and then
        # closing makes Windows reset the connection, so the browser sees "connection aborted"
        # instead of our error message. Read what is left first.
        if getattr(self, "body_consumed", False):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if 0 < length <= MAX_BODY:
            try:
                self.rfile.read(length)
            except OSError:
                pass
        self.body_consumed = True

    def _send(self, resp: Response):
        self._drain_body()
        payload = resp.raw if resp.raw is not None else json.dumps(resp.body, ensure_ascii=False).encode("utf-8")
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
        self.send_header("Content-Security-Policy", CSP)
        if resp.content_type.startswith("application/json"):
            self.send_header("Cache-Control", "no-store")
        for name, value in resp.headers:
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")


def make_server(settings: Settings, ai: AIService | None = None, key_store=None) -> ThreadingHTTPServer:
    app = App(settings, ai=ai, key_store=key_store)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((settings.host, settings.port), handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server
