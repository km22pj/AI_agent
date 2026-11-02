"""Re-register Tailscale Funnel each time the server starts.

After a reboot or a Tailscale restart, Funnel can look "on" locally while the
public entry point no longer forwards to this PC (other devices then get
ERR_CONNECTION_CLOSED). Running `funnel reset` + `funnel --bg <port>` fixes it,
so the server does that on startup, in the background.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("healthlingo.funnel")


def find_tailscale() -> str | None:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base and (Path(base) / "Tailscale" / "tailscale.exe").is_file():
                return str(Path(base) / "Tailscale" / "tailscale.exe")
    return None


def public_host(settings) -> str:
    return (urlsplit(settings.public_url).hostname or "").lower()


def wanted(settings) -> bool:
    """Only when enabled and the public address is a Tailscale Funnel address."""
    return bool(settings.auto_funnel and public_host(settings).endswith(".ts.net"))


def _run(exe: str, *args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console window flashing on Windows
    return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=flags)


def _wait_until_running(exe: str, wait_seconds: float, poll: float) -> str | None:
    """Wait for Tailscale to be connected; return this machine's DNS name, or None on timeout."""
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            data = json.loads(_run(exe, "status", "--json", timeout=15).stdout or "{}")
            if data.get("BackendState") == "Running":
                return ((data.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


def refresh_funnel(settings, wait_seconds: float = 90, poll: float = 3) -> bool:
    exe = find_tailscale()
    if not exe:
        log.warning("Tailscale을 찾지 못해 Funnel 자동 등록을 건너뛰었어요.")
        return False
    dns = _wait_until_running(exe, wait_seconds, poll)
    if dns is None:
        log.warning("Tailscale이 연결되지 않아 Funnel을 등록하지 못했어요. Tailscale 앱이 켜져 있는지 확인한 뒤 서버를 다시 시작하세요.")
        return False
    if dns and dns != public_host(settings):
        log.warning("이 PC의 Tailscale 주소(%s)가 HEALTHLINGO_PUBLIC_URL(%s)과 달라요. .env 값을 확인하세요.",
                    dns, public_host(settings))
    for args in (("funnel", "reset"), ("funnel", "--bg", str(settings.port))):
        try:
            result = _run(exe, *args, timeout=60)
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("Funnel 등록 실패 (tailscale %s): %s", " ".join(args), type(exc).__name__)
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            log.warning("Funnel 등록 실패 (tailscale %s): %s", " ".join(args), detail[-1] if detail else result.returncode)
            return False
    log.info("Tailscale Funnel을 다시 등록했어요: %s (외부에서 열리기까지 30초 정도 걸려요)", settings.public_url)
    return True


def start_in_background(settings) -> threading.Thread:
    thread = threading.Thread(target=refresh_funnel, args=(settings,), name="funnel-refresh", daemon=True)
    thread.start()
    return thread
