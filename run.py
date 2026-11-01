"""Start the Healthlingo server:  .venv\\Scripts\\python.exe run.py

Extra commands:
  run.py --show-invite      print the invite link(s) to share (needs HEALTHLINGO_INVITE_CODE)
  run.py --new-invite-code  print a new random invite code to put in .env
"""
from __future__ import annotations

import argparse
import logging
import secrets
import sys

from backend.app import make_server
from backend.config import Settings


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
    args = parser.parse_args()

    if args.new_invite_code:
        print(secrets.token_urlsafe(24))
        return
    settings = Settings.from_env()
    if args.show_invite:
        show_invite(settings)
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
    print(f"  초대 링크        : {'켜짐 — 링크 확인: .venv\\Scripts\\python.exe run.py --show-invite' if settings.invite_code else '꺼짐'}")
    print(f"  허용 계정        : {str(len(settings.allowed_emails)) + '개' if settings.allowed_emails else '제한 없음 (모든 Google 계정)'}")
    if settings.dev_login_allowed:
        print("  개발용 테스트 로그인: 켜짐 (HEALTHLINGO_DEV_LOGIN=1, 이 컴퓨터에서만)")
    elif settings.dev_login:
        print("  개발용 테스트 로그인: 외부 공개 설정이 있어 자동으로 꺼졌어요")
    if settings.exposed and not settings.allowed_emails:
        print("  주의: 허용 계정 목록이 비어 있어, 초대 링크를 받은 사람은 어떤 Google 계정으로도 로그인할 수 있어요.")
    if settings.invite_code and len(settings.invite_code) < 16:
        print("  주의: 초대 코드가 짧아 추측될 수 있어요. run.py --new-invite-code 로 새 코드를 만드세요.")
    print("종료하려면 Ctrl+C")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
