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
from .ai import AIError, AINotConfigured, AIService, build_ai_service
from .config import PROJECT_ROOT, Settings
from .db import Database, dumps, loads, now_iso
from .logic import (
    COUNTED_STATUSES,
    MEAL_TYPES,
    ValidationError,
    ai_profile,
    date_range,
    day_totals,
    default_settings,
    evaluate_day,
    meal_signature,
    normalize_settings,
    parse_client_id,
    parse_date,
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
}
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


def gate_page(title: str, detail: str) -> Response:
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
<p style="margin:18px 0 0;color:#9195a0;font-size:12px">Healthlingo</p>
</main></body></html>"""
    resp = Response(403, raw=body.encode("utf-8"), content_type="text/html; charset=utf-8")
    resp.headers.append(("Cache-Control", "no-store"))
    resp.headers.append(("X-Robots-Tag", "noindex"))
    return resp


class App:
    def __init__(self, settings: Settings, ai: AIService | None = None, key_store: auth.GoogleKeyStore | None = None):
        self.settings = settings
        self.db = Database(settings.data_dir / "healthlingo.db")
        self.upload_dir = settings.data_dir / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.ai = ai if ai is not None else build_ai_service(settings)
        self.keys = key_store or auth.GoogleKeyStore()
        self._busy: set[str] = set()
        self._busy_lock = threading.Lock()
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
        ]
        self.secret = self._load_secret()
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

    def _invite_gate(self, handler: "Handler", method: str, path: str) -> Response | None:
        """Returns a response that stops the request, or None to let it through."""
        if not self.settings.invite_code:
            return None
        if method == "GET" and path == "/favicon.ico":
            return None
        query = parse_qs(urlsplit(handler.path).query)
        if method == "GET" and "invite" in query and not path.startswith("/api/"):
            given = query["invite"][0]
            if hmac.compare_digest(given.encode("utf-8"), self.settings.invite_code.encode("utf-8")):
                # Remember this browser, then drop the code from the address bar and history.
                resp = Response(303, raw=b"", content_type="text/plain; charset=utf-8")
                resp.headers.append(("Location", "/"))
                resp.headers.append(("Referrer-Policy", "no-referrer"))
                resp.headers.append(("Set-Cookie", self._cookie(ACCESS_COOKIE, self._access_token(), ACCESS_DAYS * 86400)))
                return resp
            return gate_page("초대 링크가 올바르지 않거나 더 이상 사용할 수 없어요.", "초대한 사람에게 새 링크를 요청해 주세요.")
        if self._has_access(handler):
            return None
        if path.startswith("/api/"):
            raise HTTPError(403, "invite_required", "초대 링크로 접속해야 이용할 수 있어요. 받은 링크를 다시 열어 주세요.")
        return gate_page("초대받은 사람만 이용할 수 있어요.", "받은 초대 링크를 그대로 열어 주세요.")

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
        raise HTTPError(404, "not_found", "요청한 기능을 찾을 수 없어요.")

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

    def _email_allowed(self, email: str | None, is_dev) -> bool:
        if not self.settings.allowed_emails or is_dev:
            return True
        return bool(email) and email.lower() in self.settings.allowed_emails

    # ------------------------------------------------------------ auth

    def get_config(self, req: Request) -> Response:
        return Response(body={
            "google_client_id": self.settings.google_client_id or None,
            "google_configured": bool(self.settings.google_client_id),
            "dev_login_enabled": self.settings.dev_login_allowed,
            "invite_only": bool(self.settings.invite_code),
            "allow_list": bool(self.settings.allowed_emails),
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
        if self.settings.allowed_emails:
            email = str(claims.get("email") or "").lower()
            if claims.get("email_verified") is not True or email not in self.settings.allowed_emails:
                log.info("Sign-in refused for an account that is not on the allow list")
                raise HTTPError(403, "not_invited", f"이 Google 계정({email or '이메일 없음'})은 초대 목록에 없어요. 초대한 사람에게 계정 추가를 요청하거나, 초대받은 계정으로 로그인해 주세요.")
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
        with self.db.write_lock:
            current = self._load_settings(uid)
            updated = normalize_settings(req.json(), current, now_iso())
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT INTO user_settings (user_id, data, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                    (uid, dumps(updated), now_iso()),
                )
        return Response(body={"settings": updated})

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
            settings = self._load_settings(uid)
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

    def _day_payload(self, uid: int, day: str) -> dict:
        meals = self._meals_for(uid, day, day)
        settings = self._load_settings(uid)
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
        settings = self._load_settings(uid)
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
        settings = self._load_settings(uid)
        series = []
        for d in days:
            agg = day_totals(by_day[d])
            series.append({
                "date": d,
                "meal_count": agg["meal_count"],
                "uncounted": agg["uncounted"],
                "totals": {k: v["value"] for k, v in agg["totals"].items()},
            })
        return Response(body={"from": start, "to": end, "days": series, "targets": settings["targets"]})

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
            "total_record_days": len(recorded),
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
