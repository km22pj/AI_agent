"""Start the Healthlingo server:  .venv\\Scripts\\python.exe run.py

Extra commands:
  run.py --show-invite             print the invite link(s) to share (needs HEALTHLINGO_INVITE_CODE)
  run.py --new-invite-code         print a new random invite code to put in .env
  run.py --allow a@x.com b@y.com   add Google accounts to the allow list (no restart needed)
  run.py --disallow a@x.com        remove accounts from the allow list (no restart needed)
  run.py --allowed                 show the allow list
  run.py --export-server-bundle    pack code + .env + a data snapshot into dist/ for the cloud server
  run.py --export-server-bundle --code-only   pack only the code (to update an existing server)
  run.py --backup                  save a data snapshot into backups/
"""
from __future__ import annotations

import argparse
import logging
import re
import os
import secrets
import socket
import sys
from datetime import datetime

from backend.app import make_server
from backend.backup import make_backup, make_server_bundle
from backend import funnel
from backend.config import PROJECT_ROOT, Settings, parse_emails, read_env_value, set_env_value

ENV_FILE = PROJECT_ROOT / ".env"
ALLOW_KEY = "HEALTHLINGO_ALLOWED_EMAILS"
EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


def edit_allow_list(add: list[str], remove: list[str]) -> None:
    current = parse_emails(read_env_value(ENV_FILE, ALLOW_KEY) or "")
    bad = [e for e in add if not EMAIL_RE.match(e.strip())]
    if bad:
        print("이메일 형식이 아니라 건너뛰었어요:", ", ".join(bad))
    wanted = set(current) | set(parse_emails(",".join(e for e in add if e not in bad)))
    wanted -= set(parse_emails(",".join(remove)))
    updated = sorted(wanted)
    if updated == current:
        print("변경 사항이 없어요.")
    else:
        set_env_value(ENV_FILE, ALLOW_KEY, ",".join(updated), comment="로그인을 허용할 Google 계정 (쉼표로 구분)")
        print("허용 목록을 저장했어요. 실행 중인 서버에도 바로 적용돼요(재시작 불필요).")
    show_allow_list(updated)


def show_allow_list(emails: list[str] | None = None) -> None:
    emails = parse_emails(read_env_value(ENV_FILE, ALLOW_KEY) or "") if emails is None else emails
    if not emails:
        print("허용 목록이 비어 있어요 (모든 Google 계정 허용).")
        return
    print(f"허용된 Google 계정 {len(emails)}개:")
    for e in emails:
        print(f"  - {e}")


PYTHON_CMD = r".venv\Scripts\python.exe" if os.name == "nt" else ".venv/bin/python"


def server_running(settings: Settings) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", settings.port), timeout=0.5):
            return True
    except OSError:
        return False


def export_bundle(settings: Settings, code_only: bool) -> None:
    out = PROJECT_ROOT / "dist" / ("healthlingo-code.tar.gz" if code_only else "healthlingo-server.tar.gz")
    if not code_only and server_running(settings):
        print("주의: 이 PC에서 서버가 실행 중이에요. 지금 이후에 저장하는 기록은 묶음에 들어가지 않아요.")
        print("      서버를 끄고(Ctrl+C) 다시 만들면 가장 안전해요.")
    summary = make_server_bundle(PROJECT_ROOT, settings.data_dir, out,
                                 include_data=not code_only, include_env=not code_only)
    print(f"만들었어요: {out}")
    if code_only:
        print(f"  코드 파일 {summary['code_files']}개 (데이터와 .env는 빼서, 서버의 기록과 설정은 그대로 유지돼요)")
        return
    print(f"  코드 파일 {summary['code_files']}개, .env {'포함' if summary['env'] else '없음'}, "
          f"데이터베이스 {'포함' if summary['database'] else '없음'}, 사진 등 파일 {summary['files']}개")
    print("  이 파일에는 API 키가 들어 있어요. 서버에 올린 뒤에는 지우고, 다른 사람과 공유하지 마세요.")


def backup(settings: Settings) -> None:
    out = PROJECT_ROOT / "backups" / f"healthlingo-data-{datetime.now():%Y%m%d-%H%M%S}.tar.gz"
    summary = make_backup(settings.data_dir, out)
    print(f"백업했어요: {out}")
    print(f"  데이터베이스 {'포함' if summary['database'] else '없음'}, 사진 등 파일 {summary['files']}개")


def local_url(settings: Settings) -> str:
    return f"http://{'localhost' if settings.host in ('127.0.0.1', '::1') else settings.host}:{settings.port}"


def show_invite(settings: Settings) -> None:
    if not settings.invite_code:
        print("초대 링크가 꺼져 있어요. .env에 HEALTHLINGO_INVITE_CODE를 설정하세요.")
        return
    print("공유할 초대 링크 (비밀번호처럼 다뤄 주세요):")
    if settings.public_url:
        print(f"  외부용 : {settings.public_url}/?invite={settings.invite_code}")
    else:
        print("  외부용 : (HEALTHLINGO_PUBLIC_URL 미설정)")
    print(f"  이 PC용: {local_url(settings)}/?invite={settings.invite_code}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Healthlingo server")
    parser.add_argument("--show-invite", action="store_true", help="print the invite links and exit")
    parser.add_argument("--new-invite-code", action="store_true", help="print a new random invite code and exit")
    parser.add_argument("--allow", nargs="+", metavar="EMAIL", help="add Google accounts to the allow list")
    parser.add_argument("--disallow", nargs="+", metavar="EMAIL", help="remove Google accounts from the allow list")
    parser.add_argument("--allowed", action="store_true", help="show the allow list")
    parser.add_argument("--export-server-bundle", action="store_true", help="pack files for the cloud server and exit")
    parser.add_argument("--code-only", action="store_true", help="with --export-server-bundle: leave out data and .env")
    parser.add_argument("--backup", action="store_true", help="save a data snapshot into backups/ and exit")
    args = parser.parse_args()

    if args.new_invite_code:
        print(secrets.token_urlsafe(24))
        return
    if args.allow or args.disallow:
        edit_allow_list(args.allow or [], args.disallow or [])
        return
    if args.allowed:
        show_allow_list()
        return
    settings = Settings.from_env()
    if args.show_invite:
        show_invite(settings)
        return
    if args.export_server_bundle:
        export_bundle(settings, args.code_only)
        return
    if args.backup:
        backup(settings)
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = make_server(settings)
    app = server.app
    # Only report whether settings exist; never print their values.
    print(f"Healthlingo 실행 중: {local_url(settings)}")
    print(f"  데이터 폴더      : {settings.data_dir}")
    print(f"  Google 로그인    : {'설정됨' if settings.google_client_id else '미설정 (GOOGLE_CLIENT_ID 필요)'}")
    print(f"  AI 분석          : {'설정됨 (' + settings.ai_provider + ' / ' + settings.ai_model + ')' if app.ai.configured else '미설정 (' + settings.ai_key_name + ' 필요)'}")
    print(f"  외부 공개 주소   : {settings.public_url or '없음 (이 PC에서만)'}")
    print(f"  초대 링크        : {'켜짐 — 링크 확인: ' + PYTHON_CMD + ' run.py --show-invite' if settings.invite_code else '꺼짐'}")
    print(f"  허용 계정        : {str(len(settings.allowed_emails)) + '개만 로그인 가능' if settings.allowed_emails else '제한 없음 (Google 계정이면 누구나 로그인)'}")
    if settings.env_notes:
        print(f"  안내: 터미널에 남아 있던 예전 값 대신 .env 값을 적용했어요 ({', '.join(settings.env_notes)}).")
    if settings.dev_login_allowed:
        print("  개발용 테스트 로그인: 켜짐 (HEALTHLINGO_DEV_LOGIN=1, 이 컴퓨터에서만)")
    elif settings.dev_login:
        print("  개발용 테스트 로그인: 외부 공개 설정이 있어 자동으로 꺼졌어요")
    if settings.exposed and not settings.allowed_emails and not settings.invite_code:
        print("  주의: 초대 링크와 허용 계정이 모두 꺼져 있어, 주소를 아는 누구나 Google 계정으로 가입하고 AI 분석을 쓸 수 있어요.")
    if settings.invite_code and len(settings.invite_code) < 16:
        print("  주의: 초대 코드가 짧아 추측될 수 있어요. run.py --new-invite-code 로 새 코드를 만드세요.")
    if funnel.wanted(settings):
        print("  Funnel 자동 등록  : 켜짐 — 백그라운드에서 다시 등록 중 (끄려면 HEALTHLINGO_AUTO_FUNNEL=0)")
    print("종료하려면 Ctrl+C")
    sys.stdout.flush()
    if funnel.wanted(settings):
        funnel.start_in_background(settings)  # the port is already bound, so Funnel can forward right away
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
