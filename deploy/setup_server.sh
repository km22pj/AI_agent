#!/usr/bin/env bash
# Healthlingo 서버 설치·업데이트 스크립트 (Ubuntu 24.04 LTS 기준)
#
#   tar xzf healthlingo-server.tar.gz
#   sudo bash healthlingo/deploy/setup_server.sh              # 처음 설치 또는 코드 업데이트
#   sudo bash healthlingo/deploy/setup_server.sh --replace-data  # 서버 데이터를 번들 데이터로 교체(기존 것은 백업)
#
# 하는 일: 패키지 설치 → 앱 복사(/opt/healthlingo) → 자동 실행 서비스 등록
#          → Tailscale 연결 → Funnel(무료 HTTPS 공개 주소) 켜기 → 공개 주소를 .env에 기록
# 서버에 이미 데이터나 .env가 있으면 덮어쓰지 않아요.
set -euo pipefail

APP_DIR=/opt/healthlingo
APP_USER=healthlingo
PORT=8000
TS_HOSTNAME="${TS_HOSTNAME:-healthlingo}"
REPLACE_DATA=0

for arg in "$@"; do
  case "$arg" in
    --replace-data) REPLACE_DATA=1 ;;
    *) echo "알 수 없는 옵션: $arg"; exit 1 ;;
  esac
done

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\n[중단] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "관리자 권한이 필요해요: sudo bash $0"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$SRC/run.py" ] || die "번들을 푼 폴더 안의 deploy/setup_server.sh 를 실행하세요."
[ "$SRC" != "$APP_DIR" ] || die "설치된 폴더가 아니라, 번들을 푼 폴더에서 실행하세요."

say "1/7 필요한 패키지 설치"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl ca-certificates >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || die "Python 3.12 이상이 필요해요. VM 부팅 디스크를 Ubuntu 24.04 LTS로 만드세요."

say "2/7 메모리 보조(스왑 1GB) 설정"
if [ -z "$(swapon --noheadings)" ]; then
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "  스왑을 만들었어요."
else
  echo "  이미 있어요."
fi

say "3/7 앱 파일 복사 ($APP_DIR)"
id -u "$APP_USER" >/dev/null 2>&1 \
  || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR/data"
rm -rf "$APP_DIR/backend" "$APP_DIR/deploy" "$APP_DIR/icons"
cp -r "$SRC/backend" "$SRC/deploy" "$SRC/icons" "$APP_DIR/"
for f in run.py app.js healthlingo_v1.html manifest.webmanifest sw.js requirements.txt README.md NUTRITION.md .env.example; do
  if [ -f "$SRC/$f" ]; then cp "$SRC/$f" "$APP_DIR/$f"; fi
done
find "$APP_DIR/backend" -name '__pycache__' -prune -exec rm -rf {} +

if [ -f "$APP_DIR/.env" ]; then
  echo "  서버의 기존 .env를 그대로 써요. (수정: sudo nano $APP_DIR/.env)"
elif [ -f "$SRC/.env" ]; then
  cp "$SRC/.env" "$APP_DIR/.env"
  echo "  .env를 복사했어요."
else
  touch "$APP_DIR/.env"
  echo "  주의: .env가 없어요. GOOGLE_CLIENT_ID, GEMINI_API_KEY를 $APP_DIR/.env 에 넣어야 해요."
fi

if [ -f "$SRC/data/healthlingo.db" ]; then
  if [ ! -f "$APP_DIR/data/healthlingo.db" ]; then
    cp -a "$SRC/data/." "$APP_DIR/data/"
    echo "  PC에서 가져온 기록(데이터)을 옮겼어요."
  elif [ "$REPLACE_DATA" -eq 1 ]; then
    systemctl stop healthlingo 2>/dev/null || true
    BACKUP="$APP_DIR/data.bak-$(date +%Y%m%d-%H%M%S)"
    mv "$APP_DIR/data" "$BACKUP"
    mkdir -p "$APP_DIR/data"
    cp -a "$SRC/data/." "$APP_DIR/data/"
    echo "  서버 데이터를 번들 데이터로 교체했어요. 이전 데이터: $BACKUP"
  else
    echo "  서버에 이미 기록이 있어 그대로 둬요. (번들 데이터는 무시, 교체하려면 --replace-data)"
  fi
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 700 "$APP_DIR/data"
chmod 600 "$APP_DIR/.env"

say "4/7 Python 패키지 설치 (1~3분)"
[ -x "$APP_DIR/.venv/bin/python" ] || sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --no-cache-dir --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q --no-cache-dir -r "$APP_DIR/requirements.txt"

say "5/7 자동 실행 서비스 등록 (재부팅해도 자동으로 켜져요)"
cat > /etc/systemd/system/healthlingo.service <<EOF
[Unit]
Description=Healthlingo
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python run.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TZ=Asia/Seoul
# 이 스크립트가 관리자 권한으로 Funnel을 등록하므로, 앱이 직접 다시 등록하지 않게 해요.
Environment=HEALTHLINGO_AUTO_FUNNEL=0
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable healthlingo >/dev/null 2>&1
systemctl restart healthlingo

wait_for_app() {
  for _ in $(seq 1 30); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/favicon.ico" || true)" = "204" ] && return 0
    sleep 1
  done
  return 1
}
wait_for_app || die "앱이 켜지지 않았어요. 로그 확인: sudo journalctl -u healthlingo -n 50"
echo "  앱이 켜졌어요."

say "6/7 Tailscale 연결"
command -v tailscale >/dev/null 2>&1 || curl -fsSL https://tailscale.com/install.sh | sh
if ! tailscale status >/dev/null 2>&1; then
  echo "  아래에 https://login.tailscale.com/... 주소가 나오면 PC 브라우저에서 열고,"
  echo "  PC에서 쓰던 것과 같은 Tailscale 계정으로 로그인·승인하세요. 승인되면 자동으로 계속돼요."
  tailscale up --hostname="$TS_HOSTNAME"
fi

say "7/7 Funnel 켜기 (인터넷 공개 HTTPS 주소)"
tailscale funnel --bg "$PORT"
DNS_NAME="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
[ -n "$DNS_NAME" ] || die "Tailscale 주소를 읽지 못했어요: tailscale status 로 확인하세요."
PUBLIC_URL="https://$DNS_NAME"

# 공개 주소를 .env에 기록하고, 초대 코드가 없으면 새로 만들어요 (값은 화면에 출력하지 않음).
cd "$APP_DIR"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" - "$PUBLIC_URL" <<'PY'
import secrets, sys
from pathlib import Path
from backend.config import read_env_value, set_env_value
env = Path(".env")
set_env_value(env, "HEALTHLINGO_PUBLIC_URL", sys.argv[1], comment="외부 공개 주소 (Tailscale Funnel)")
if not (read_env_value(env, "HEALTHLINGO_INVITE_CODE") or "").strip():
    set_env_value(env, "HEALTHLINGO_INVITE_CODE", secrets.token_urlsafe(24), comment="초대 링크 코드")
    print("  초대 코드를 새로 만들었어요.")
PY
systemctl restart healthlingo
wait_for_app || die "앱이 다시 켜지지 않았어요. 로그 확인: sudo journalctl -u healthlingo -n 50"

say "완료"
echo "  공개 주소 : $PUBLIC_URL"
echo "  ※ Google Cloud 콘솔 > OAuth 클라이언트 > '승인된 JavaScript 원본'에 위 주소를 추가해야 Google 로그인이 돼요."
echo
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" run.py --show-invite
echo
echo "  관리 명령 예시:"
echo "    상태 보기   : sudo systemctl status healthlingo"
echo "    로그 보기   : sudo journalctl -u healthlingo -f"
echo "    재시작      : sudo systemctl restart healthlingo"
echo "    백업 만들기 : cd $APP_DIR && sudo -u $APP_USER .venv/bin/python run.py --backup"
echo
echo "  번들에는 API 키(.env)가 들어 있어요. 설치가 끝났으니 지워 주세요:"
echo "    rm -rf \"$SRC\" ~/healthlingo-server.tar.gz"
