"""Google ID token verification and server-side sessions.

The browser gets an ID token from Google Identity Services and posts it here.
We verify the RS256 signature against Google's published keys, then check
issuer, audience (our client ID) and expiry before creating a session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from .db import Database, now_iso

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
SESSION_COOKIE = "hl_session"
CLOCK_SKEW_SECONDS = 60

# DER prefix of DigestInfo for SHA-256 (PKCS#1 v1.5 signatures).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class AuthError(Exception):
    """Token rejected. The message is safe to show (it never contains the token)."""


def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def rsa_sha256_verify(message: bytes, signature: bytes, n: int, e: int) -> bool:
    key_len = (n.bit_length() + 7) // 8
    if len(signature) != key_len:
        return False
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        return False
    em = pow(sig_int, e, n).to_bytes(key_len, "big")
    digest = hashlib.sha256(message).digest()
    t = _SHA256_DIGEST_INFO + digest
    pad_len = key_len - len(t) - 3
    if pad_len < 8:
        return False
    expected = b"\x00\x01" + b"\xff" * pad_len + b"\x00" + t
    return hmac.compare_digest(em, expected)


class GoogleKeyStore:
    """Fetches and caches Google's signing keys (refreshes on unknown key id)."""

    def __init__(self, url: str = GOOGLE_JWKS_URL, fetcher=None):
        self.url = url
        self._fetcher = fetcher or self._fetch
        self._keys: dict[str, tuple[int, int]] = {}
        self._expires = 0.0
        self._lock = threading.Lock()

    def _fetch(self):
        with urllib.request.urlopen(self.url, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            max_age = 3600
            cache = resp.headers.get("Cache-Control", "")
            for part in cache.split(","):
                part = part.strip()
                if part.startswith("max-age="):
                    try:
                        max_age = int(part.split("=", 1)[1])
                    except ValueError:
                        pass
            return body, max_age

    def get(self, kid: str) -> tuple[int, int] | None:
        with self._lock:
            if kid in self._keys and time.time() < self._expires:
                return self._keys[kid]
            try:
                body, max_age = self._fetcher()
            except Exception as exc:  # network failure
                raise AuthError("Google 인증 서버에 연결하지 못했어요. 잠시 후 다시 시도해 주세요.") from exc
            keys = {}
            for jwk in body.get("keys", []):
                if jwk.get("kty") == "RSA" and "kid" in jwk:
                    keys[jwk["kid"]] = (
                        int.from_bytes(b64url_decode(jwk["n"]), "big"),
                        int.from_bytes(b64url_decode(jwk["e"]), "big"),
                    )
            self._keys = keys
            self._expires = time.time() + max(60, min(max_age, 86400))
            return self._keys.get(kid)


def verify_google_id_token(token: str, client_id: str, keys: GoogleKeyStore, now: float | None = None) -> dict:
    if not client_id:
        raise AuthError("Google 로그인이 아직 설정되지 않았어요.")
    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 8192:
        raise AuthError("로그인 정보 형식이 올바르지 않아요.")
    header_b64, payload_b64, sig_b64 = token.split(".")
    try:
        header = json.loads(b64url_decode(header_b64))
        claims = json.loads(b64url_decode(payload_b64))
        signature = b64url_decode(sig_b64)
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthError("로그인 정보를 읽을 수 없어요.") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise AuthError("지원하지 않는 로그인 토큰이에요.")
    key = keys.get(header["kid"])
    if key is None:
        raise AuthError("로그인 토큰의 서명 키를 확인할 수 없어요.")
    if not rsa_sha256_verify(f"{header_b64}.{payload_b64}".encode("ascii"), signature, *key):
        raise AuthError("로그인 토큰 서명이 올바르지 않아요.")
    now = time.time() if now is None else now
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError("Google에서 발급한 토큰이 아니에요.")
    aud = claims.get("aud")
    if aud != client_id and not (isinstance(aud, list) and client_id in aud):
        raise AuthError("이 서비스용으로 발급된 로그인 토큰이 아니에요.")
    try:
        exp = float(claims["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthError("로그인 토큰에 만료 정보가 없어요.") from exc
    if exp + CLOCK_SKEW_SECONDS < now:
        raise AuthError("로그인 시간이 만료됐어요. 다시 로그인해 주세요.")
    iat = claims.get("iat")
    if isinstance(iat, (int, float)) and iat - CLOCK_SKEW_SECONDS > now:
        raise AuthError("로그인 토큰 시간이 올바르지 않아요.")
    if not claims.get("sub"):
        raise AuthError("Google 계정 정보를 확인할 수 없어요.")
    return claims


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def upsert_user(db: Database, sub: str, email: str | None, name: str | None, picture: str | None, is_dev: bool = False) -> int:
    stamp = now_iso()
    with db.write_lock, db.connect() as conn:
        row = conn.execute("SELECT id FROM users WHERE google_sub = ?", (sub,)).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET email = ?, name = ?, picture = ?, last_login_at = ? WHERE id = ?",
                (email, name, picture, stamp, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO users (google_sub, email, name, picture, is_dev, created_at, last_login_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sub, email, name, picture, 1 if is_dev else 0, stamp, stamp),
        )
        return cur.lastrowid


def create_session(db: Database, user_id: int, days: int) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    with db.write_lock, db.connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, now_iso(), expires.strftime("%Y-%m-%dT%H:%M:%SZ")),
        )
        # Housekeeping: drop expired sessions.
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
    return token, days * 86400


def session_user(db: Database, token: str | None):
    if not token or len(token) > 200:
        return None
    with db.connect() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
            (_hash_token(token), now_iso()),
        ).fetchone()
    return row


def delete_session(db: Database, token: str | None) -> None:
    if not token:
        return
    with db.write_lock, db.connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))
