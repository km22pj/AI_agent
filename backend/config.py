"""Settings loaded from environment variables (and an optional local .env file).

Secret values are never printed. Only whether they are set is reported.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODELS = {"gemini": "gemini-3.6-flash", "anthropic": "claude-opus-5-5"}
# Tried in order when the main model is overloaded or too slow.
DEFAULT_FALLBACKS = {"gemini": ["gemini-3.5-flash-lite"], "anthropic": []}


def load_dotenv(path: Path) -> None:
    """Read KEY=VALUE lines into os.environ without overriding values already set."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_origin(value: str) -> str:
    """'https://Host.example/path/' -> 'https://host.example'. Empty or invalid -> ''."""
    from urllib.parse import urlsplit

    value = (value or "").strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    google_client_id: str = ""
    dev_login: bool = False
    cookie_secure: bool = False
    session_days: int = 30
    ai_provider: str = "gemini"
    ai_model: str = "gemini-3.6-flash"
    ai_fallback_models: list = field(default_factory=lambda: list(DEFAULT_FALLBACKS["gemini"]))
    ai_timeout: float = 45.0
    ai_effort: str = "medium"
    anthropic_key_set: bool = False
    gemini_key_set: bool = False
    # Access control for when the site is reachable from outside.
    public_url: str = ""            # e.g. https://my-pc.tail1234.ts.net (no trailing slash)
    invite_code: str = ""           # when set, only browsers that opened /?invite=<code> get in
    allowed_emails: list = field(default_factory=list)  # when set, only these Google accounts can sign in

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")
        data_dir = os.environ.get("HEALTHLINGO_DATA_DIR")
        effort = os.environ.get("HEALTHLINGO_AI_EFFORT", "medium").strip().lower()
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            effort = "medium"
        provider = os.environ.get("HEALTHLINGO_AI_PROVIDER", "gemini").strip().lower() or "gemini"
        if provider not in DEFAULT_MODELS:
            provider = "gemini"
        model = os.environ.get("HEALTHLINGO_AI_MODEL", "").strip() or DEFAULT_MODELS[provider]
        raw_fallbacks = os.environ.get("HEALTHLINGO_AI_FALLBACK_MODELS")
        if raw_fallbacks is None:
            fallbacks = list(DEFAULT_FALLBACKS[provider])
        elif raw_fallbacks.strip().lower() in {"", "none", "off"}:
            fallbacks = []
        else:
            fallbacks = [m.strip() for m in raw_fallbacks.split(",") if m.strip()]
        fallbacks = [m for m in fallbacks if m != model]
        try:
            timeout = min(300.0, max(10.0, float(os.environ.get("HEALTHLINGO_AI_TIMEOUT", "45"))))
        except ValueError:
            timeout = 45.0
        return cls(
            host=os.environ.get("HEALTHLINGO_HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=int(os.environ.get("HEALTHLINGO_PORT", "8000")),
            data_dir=Path(data_dir) if data_dir else PROJECT_ROOT / "data",
            google_client_id=os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
            dev_login=_flag("HEALTHLINGO_DEV_LOGIN"),
            cookie_secure=_flag("HEALTHLINGO_COOKIE_SECURE"),
            ai_provider=provider,
            ai_model=model,
            ai_fallback_models=fallbacks,
            ai_timeout=timeout,
            ai_effort=effort,
            anthropic_key_set=bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
            gemini_key_set=bool(os.environ.get("GEMINI_API_KEY", "").strip()),
            public_url=normalize_origin(os.environ.get("HEALTHLINGO_PUBLIC_URL", "")),
            invite_code=os.environ.get("HEALTHLINGO_INVITE_CODE", "").strip(),
            allowed_emails=sorted({e.strip().lower() for e in os.environ.get("HEALTHLINGO_ALLOWED_EMAILS", "").split(",") if e.strip()}),
        )

    @property
    def exposed(self) -> bool:
        """True when the site is meant to be reachable from other machines."""
        return bool(self.public_url or self.invite_code or self.host not in {"127.0.0.1", "localhost", "::1"})

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure or self.public_url.startswith("https://")

    @property
    def ai_key_name(self) -> str:
        return "GEMINI_API_KEY" if self.ai_provider == "gemini" else "ANTHROPIC_API_KEY"

    @property
    def dev_login_allowed(self) -> bool:
        # The test login only works when explicitly enabled AND the site is not exposed.
        # A tunnel connects from 127.0.0.1 too, so "bound to localhost" alone is not enough.
        return self.dev_login and not self.exposed
