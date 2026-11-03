# AI_agent

# Healthlingo

개인 맞춤형 운동·식단 기록 서비스. 기존 화면 디자인(`healthlingo_v1.html`)을 유지하고, 실제 로그인·저장·AI 분석을 연결했어요.

## 구성

| 경로 | 역할 |
|---|---|
| `healthlingo_v1.html` | 화면 (기존 디자인 + 새 화면 요소) |
| `app.js` | 화면 동작. 모든 수치는 서버에서 받아 표시 |
| `backend/app.py` | API 서버 (Python 표준 라이브러리) |
| `backend/auth.py` | Google ID 토큰 서명 검증, 세션 쿠키 |
| `backend/ai.py` | AI(기본 Gemini) 음식 인식·영양 추정·식단 피드백, 응답 검증 |
| `backend/logic.py` | 입력 검증, 영양 합계·달성률·평가 계산 |
| `backend/db.py` | SQLite 스키마 |
| `backend/backup.py` | 데이터 백업, 클라우드 서버용 묶음 만들기 |
| `deploy/setup_server.sh` | 클라우드 VM 설치·업데이트 스크립트 |
| `data/` | 실행 시 생성. `healthlingo.db`(기록) + `uploads/u<사용자ID>/`(사진, 비공개) |
| `tests/` | 자동 테스트 |

## 실행

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env          # 값 채우기 (아래 참고)
.\.venv\Scripts\python.exe run.py
```

브라우저에서 **http://localhost:8000** 으로 접속하세요. (Google 로그인은 Google 콘솔에 등록한 주소로 접속해야 동작해요. 아래 설정에서는 `localhost`를 등록해요.)

테스트:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

## 환경변수

| 이름 | 필수 | 설명 |
|---|---|---|
| `GOOGLE_CLIENT_ID` | 로그인에 필요 | Google OAuth "웹 애플리케이션" 클라이언트 ID |
| `GEMINI_API_KEY` | AI 분석에 필요 | 없으면 AI 기능만 꺼지고, 직접 입력 기록은 그대로 동작 |
| `HEALTHLINGO_AI_MODEL` | 선택 | 기본 `gemini-3.6-flash` |
| `HEALTHLINGO_AI_FALLBACK_MODELS` | 선택 | 기본 모델이 과부하·지연일 때 쓸 모델(쉼표 구분). 기본 `gemini-3.5-flash-lite`, 끄려면 `none` |
| `HEALTHLINGO_AI_TIMEOUT` | 선택 | 모델 1개당 최대 대기 시간(초). 기본 `45` |
| `HEALTHLINGO_AI_PROVIDER` | 선택 | 기본 `gemini`. `anthropic`으로 바꾸면 Claude 사용 (`ANTHROPIC_API_KEY` 필요) |
| `HEALTHLINGO_AI_EFFORT` | 선택 | Claude 사용 시에만 적용. 기본 `medium` |
| `HEALTHLINGO_HOST` / `HEALTHLINGO_PORT` | 선택 | 기본 `127.0.0.1` / `8000` |
| `HEALTHLINGO_DATA_DIR` | 선택 | 기본 `data` |
| `HEALTHLINGO_COOKIE_SECURE` | 배포 시 | HTTPS 배포에서 `1` |
| `HEALTHLINGO_DEV_LOGIN` | 개발용 | `1`이면 이 컴퓨터에서만 Google 없이 쓰는 테스트 계정 로그인 표시 (Google 로그인 아님). 아래 외부 공개 설정이 하나라도 있으면 자동으로 꺼져요 |
| `HEALTHLINGO_PUBLIC_URL` | 외부 공개 시 | 외부 공개 주소 (예: `https://내PC이름.tailXXXX.ts.net`) |
| `HEALTHLINGO_INVITE_CODE` | 외부 공개 시 | 초대 링크 코드. 설정하면 초대 링크로 들어온 브라우저만 이용 가능 |
| `HEALTHLINGO_ALLOWED_EMAILS` | 선택 | 로그인을 허용할 Google 계정(쉼표 구분). 비워 두면(기본) Google 계정이면 누구나 로그인, 설정하면 목록에 있는 계정만 로그인 |
| `HEALTHLINGO_AUTO_FUNNEL` | 선택 | 기본 `1`. 공개 주소가 `*.ts.net`이면 서버 시작 때 Tailscale Funnel을 다시 등록(`funnel reset` → `funnel --bg 포트`). `0`이면 끔 |

값은 `.env` 파일이나 시스템 환경변수로 넣을 수 있어요. 서버는 값이 있는지만 출력하고 값 자체는 출력하지 않아요.

## Google 로그인 설정

1. [Google Cloud 콘솔](https://console.cloud.google.com/)에서 프로젝트를 만들거나 선택해요.
2. **Google 인증 플랫폼(Google Auth Platform)** → 브랜딩에서 앱 이름·지원 이메일을 입력하고, 대상에서 *외부*를 골라요. 이 앱은 기본 정보(이름·이메일·프로필)만 요청하므로 "테스트" 상태여도 **테스트 사용자 등록이 필요 없어요.** Google 계정이면 누구나 로그인할 수 있고, 계정을 제한하고 싶을 때만 `.env`의 `HEALTHLINGO_ALLOWED_EMAILS`를 쓰면 돼요.
3. **클라이언트 → 클라이언트 만들기** → 애플리케이션 유형 **웹 애플리케이션**.
4. **승인된 JavaScript 원본**에 아래 두 개를 추가해요.
   - `http://localhost:8000`
   - `http://localhost`
5. **승인된 리디렉션 URI**: 이 앱은 Google 로그인 *팝업* 방식이라 **필요 없어요.** 비워 두세요.
6. 생성된 **클라이언트 ID**를 `.env`의 `GOOGLE_CLIENT_ID`에 넣어요. 클라이언트 보안 비밀번호(secret)는 쓰지 않으니 어디에도 넣지 마세요.
7. 서버를 다시 시작하고 `http://localhost:8000`으로 접속해요.

배포할 때는 `https://내-도메인`을 JavaScript 원본에 추가하고, HTTPS 뒤에서 `HEALTHLINGO_COOKIE_SECURE=1`로 실행하세요.

## Gemini AI 설정 (기본)

1. [Google AI Studio](https://aistudio.google.com/apikey)에서 API 키를 만들어요. 무료 사용량을 넘기면 결제 설정이 필요할 수 있어요.
2. `.env`에 `GEMINI_API_KEY=발급받은키` 줄을 넣고 서버를 다시 시작해요.
3. 기본 모델은 `gemini-3.6-flash`이고, 과부하(503)·한도 초과·시간 초과가 나면 `gemini-3.5-flash-lite`로 자동 전환해요. 문제가 난 모델은 5분 동안 건너뛰어요. `HEALTHLINGO_AI_MODEL`로 다른 모델(예: `gemini-3.8-flash`)을 지정할 수 있지만, 인기 모델은 사용자가 몰리면 응답이 매우 느려질 수 있어요.

응답 속도를 위해 사진 인식은 AI의 생각 단계를 최소(`minimal`), 영양 추정·피드백은 낮게(`low`) 설정해 호출해요. SDK 자체 재시도는 끄고(과부하 모델을 다시 기다리면 대기만 길어져서), 대신 다른 모델로 넘겨요.

Gemini Interactions API(`google-genai` SDK)로 호출하고, 정해진 JSON 스키마로 응답을 받은 뒤 서버에서 다시 검증해요. 건강 정보라서 `store=false`로 호출해 Google 쪽에 대화 기록을 남기지 않도록 요청해요. AI는 사진 분석·영양 추정·피드백을 요청할 때만 호출되고, 그때 사진과 식사 정보(목표·알레르기 포함, 이름·이메일 제외)가 Google로 전송돼요.

Claude로 되돌리려면 `HEALTHLINGO_AI_PROVIDER=anthropic`과 `ANTHROPIC_API_KEY`를 설정하세요.

## 외부 공개 (초대받은 사람만)

Tailscale Funnel로 이 PC의 서버를 `https://내PC이름.tailXXXX.ts.net` 형태의 주소에 연결해요(실제 주소는 `tailscale funnel status`로 확인하고 `.env`의 `HEALTHLINGO_PUBLIC_URL`에 넣어요). PC와 서버가 켜져 있는 동안만 접속돼요. PC가 꺼져 있어도 접속되게 하려면 아래 "클라우드 서버"를 보세요.

**접근 제어 (두 단계)**
1. **초대 링크**: `HEALTHLINGO_INVITE_CODE`가 있으면 `주소/?invite=코드`로 처음 들어온 브라우저에만 접근 쿠키(180일)를 줘요. 쿠키가 없으면 앱 화면·API 모두 "초대받은 사람만 이용할 수 있어요" 안내만 보여요. 링크를 열면 주소창에서 코드는 바로 지워져요.
2. **허용 계정 (선택, 현재 꺼짐)**: 비워 두면 초대 링크로 들어온 사람은 어떤 Google 계정으로든 로그인할 수 있어요. `HEALTHLINGO_ALLOWED_EMAILS`에 이메일을 넣으면 그 계정만 로그인되고, 목록에서 빼면 이미 로그인한 사람도 다음 요청부터 막혀요.

**자주 쓰는 명령**
```powershell
.\.venv\Scripts\python.exe run.py --show-invite      # 공유할 초대 링크 보기
.\.venv\Scripts\python.exe run.py --new-invite-code  # 새 코드 만들기 → .env에 넣고 서버 재시작
.\.venv\Scripts\python.exe run.py --allow 팀원@gmail.com   # 허용 계정 추가 (재시작 불필요)
.\.venv\Scripts\python.exe run.py --disallow 팀원@gmail.com  # 허용 계정 제거 (재시작 불필요)
.\.venv\Scripts\python.exe run.py --allowed                # 허용 계정 목록 보기
& "$env:ProgramFiles\Tailscale\tailscale.exe" funnel status          # 공개 상태 확인
& "$env:ProgramFiles\Tailscale\tailscale.exe" funnel --https=443 off # 외부 공개 끄기
& "$env:ProgramFiles\Tailscale\tailscale.exe" funnel --bg 8000       # 다시 켜기
```

- 링크가 원치 않는 사람에게 퍼졌으면: 새 코드를 만들어 `.env`에 넣고 서버를 재시작하세요. 기존 링크와 그 링크로 받은 접근 쿠키가 모두 무효가 돼요. 허용 계정 목록이 있으면 링크가 퍼져도 목록 밖 계정은 로그인할 수 없어요.
- 사람 추가: 허용 계정이 꺼져 있으면 초대 링크만 보내면 돼요. 켜 둔 경우에는 `run.py --allow 이메일`을 실행하거나 `.env`의 `HEALTHLINGO_ALLOWED_EMAILS`에 Gmail 주소를 쉼표로 더한 뒤 초대 링크를 보내세요. 실행 중인 서버가 `.env` 변경을 감지해 바로 적용해요. 목록에 없는 계정이 로그인을 시도하면 서버 창에 그 이메일과 추가 명령이 표시돼요.
- Google 로그인: Google Cloud 콘솔의 OAuth 클라이언트 → **승인된 JavaScript 원본**에 외부 공개 주소(`HEALTHLINGO_PUBLIC_URL` 값)를 추가해야 외부에서 로그인돼요.
- 서버(`run.py`)를 시작하면 Funnel을 자동으로 다시 등록해요(백그라운드, Tailscale이 켜지는 중이면 최대 90초 기다림). 외부에서 열리기까지 30초 정도 걸려요. 서버 창에 "Tailscale Funnel을 다시 등록했어요"가 보이면 성공이고, 실패하면 이유가 경고로 표시돼요. Tailscale 트레이 앱은 실행 중이어야 해요.
- **이 PC에서는 열리는데 다른 장치에서 `ERR_CONNECTION_CLOSED`가 뜨면**: 이 PC는 Tailscale 내부 경로로 접속해서 외부 공개 상태를 확인할 수 없어요. 서버를 다시 시작하거나, 아래 명령으로 Funnel을 직접 다시 등록하세요.
  ```powershell
  & "$env:ProgramFiles\Tailscale\tailscale.exe" funnel reset
  & "$env:ProgramFiles\Tailscale\tailscale.exe" funnel --bg 8000
  ```
  확인은 휴대폰 데이터(와이파이 끄기)로 접속하거나, https://check-host.net 에서 주소를 넣어 해외에서 열리는지 보면 돼요(초대 링크 없이 열면 403 안내 페이지가 정상).
- 외부 사용자의 AI 분석 비용은 내 Gemini 키로 나가요.

## PC가 꺼져 있어도 접속 (클라우드 서버)

Google Cloud 무료 VM(e2-micro, "Always Free")에 같은 앱을 그대로 올려요. 기록(SQLite)과 사진은 VM 디스크에 남고, 공개 주소는 VM에서 Tailscale Funnel로 만들어요(도메인 불필요). 서버는 자동 실행 서비스로 등록돼서 VM이 재부팅돼도 다시 켜져요.

**1. VM 만들기 (Google Cloud 콘솔, 직접 해야 해요)**
1. https://console.cloud.google.com → OAuth 클라이언트가 있는 프로젝트 선택 → 결제 계정 연결(카드 등록 필요). 무료 범위 안에서는 청구되지 않지만, **결제 > 예산 및 알림**에서 작은 금액(예: 1,000원) 알림을 꼭 만들어 두세요.
2. **Compute Engine > VM 인스턴스 > 인스턴스 만들기** (처음이면 API 사용 설정)
   - 리전: `us-west1`, `us-central1`, `us-east1` 중 하나 (그 외 리전은 유료)
   - 머신 유형: `e2-micro`
   - 부팅 디스크: **Ubuntu 24.04 LTS (x86/64)**, 디스크 유형 **표준 영구 디스크(Standard)**, 30GB 이하 (균형/SSD는 유료)
   - 방화벽의 HTTP/HTTPS 허용은 체크하지 않아도 돼요(Funnel이 바깥으로 연결해요). 외부 IP는 기본값(임시) 그대로 두세요.
3. 무료 범위: e2-micro 1대, 표준 디스크 30GB, 북미 밖으로 나가는 트래픽 월 1GB. 사진을 많이 보면 1GB를 넘을 수 있고, 넘으면 GB당 소액이 청구돼요. 처음 며칠은 **결제 > 보고서**에서 금액이 0원인지 확인하세요.

**2. PC에서 옮길 파일 만들기**
```powershell
# 먼저 PC의 서버를 끄세요(Ctrl+C). 이후 PC에 저장한 기록은 옮겨지지 않아요.
.\.venv\Scripts\python.exe run.py --export-server-bundle
```
`dist\healthlingo-server.tar.gz`가 만들어져요. 코드 + `.env`(API 키) + 기록·사진 사본이 들어 있으니 다른 곳에 공유하지 마세요.

**3. VM에 설치**
1. VM 목록의 **SSH** 버튼 → 열린 창 오른쪽 위 **파일 업로드**로 `healthlingo-server.tar.gz` 올리기
2. 같은 창에서:
   ```bash
   tar xzf healthlingo-server.tar.gz
   sudo bash healthlingo/deploy/setup_server.sh
   ```
3. 중간에 `https://login.tailscale.com/...` 주소가 나오면 PC 브라우저에서 열고, PC와 **같은 Tailscale 계정**으로 승인하세요.
4. 끝나면 공개 주소(예: `https://healthlingo.tailXXXX.ts.net`)와 새 초대 링크가 나와요. 설치 후 `rm -rf healthlingo healthlingo-server.tar.gz`로 업로드한 파일을 지우세요.

**4. 마무리 (직접 해야 해요)**
- Google Cloud 콘솔 → OAuth 클라이언트 → **승인된 JavaScript 원본**에 새 공개 주소를 추가하세요.
- 팀원에게 **새 초대 링크**를 보내세요(주소가 바뀌어 예전 링크는 이 서버로 연결되지 않아요).
- PC 쪽 공개를 끄세요. 두 곳에 따로 기록이 쌓이지 않게 하려는 거예요.
  ```powershell
  & "$env:ProgramFiles\Tailscale\tailscale.exe" funnel reset
  ```

**서버 관리 (VM의 SSH 창에서)**
```bash
sudo systemctl status healthlingo        # 상태
sudo journalctl -u healthlingo -f        # 로그
sudo systemctl restart healthlingo       # 재시작 (.env 수정 후)
sudo nano /opt/healthlingo/.env          # 설정 수정
cd /opt/healthlingo
sudo -u healthlingo .venv/bin/python run.py --show-invite         # 초대 링크
sudo -u healthlingo .venv/bin/python run.py --allow 팀원@gmail.com  # 허용 계정 추가
sudo -u healthlingo .venv/bin/python run.py --backup              # 백업 → /opt/healthlingo/backups/
```
- 백업 파일은 SSH 창의 **파일 다운로드**로 PC에 받아 두세요(경로 예: `/opt/healthlingo/backups/healthlingo-data-날짜.tar.gz`). 다운로드는 권한상 먼저 `sudo cp /opt/healthlingo/backups/파일 ~/ && sudo chown $USER ~/파일` 후 받으면 돼요.
- **코드 업데이트**: PC에서 `run.py --export-server-bundle --code-only` → `dist\healthlingo-code.tar.gz`를 올리고 `tar xzf healthlingo-code.tar.gz && sudo bash healthlingo/deploy/setup_server.sh`. 서버의 기록과 `.env`는 그대로 유지돼요.
- 서버 기록을 PC 번들 데이터로 바꾸고 싶을 때만 `--replace-data`를 붙이세요(기존 데이터는 `data.bak-날짜`로 보관돼요).

## 동작 요약

- **기록 분리**: 모든 조회·수정·삭제 쿼리가 로그인한 사용자 ID로 제한돼요. 다른 사람의 기록·사진 ID를 알아도 404가 돌아가요.
- **사진**: 브라우저에서 다시 압축(위치 정보 등 메타데이터 제거)한 뒤 업로드하고, 서버의 `data/uploads/`에 저장해요. 공개 경로가 없고 로그인한 소유자만 `/api/photos/...`로 받을 수 있어요.
- **음식 사진 흐름**: 사진 선택 → 업로드 → AI 인식 → 사용자가 이름·양·조리법 확인/수정 → 저장 → 영양 추정·피드백 → 합계·그래프 갱신. AI가 실패해도 기록은 저장돼요.
- **추정치 표시**: 영양 정보가 없으면 0이 아니라 "분석 전/정보 부족"으로 표시해요. 음식·양을 수정하면 이전 분석은 "재분석 필요"로 바뀌고 합계에서 빠져요.
- **하루 영양 목표 (자동 계산)**: 개인 설정을 바탕으로 서버가 공개 근거 계산식으로 산정·저장해요. 이 계산식에 쓰는 값은 키·몸무게·나이·성별·평소 활동량·해당 상황·목표·운동 계획이에요. AI는 숫자를 만들지 않고 설명만 써요. 목표는 날짜별 이력으로 보관돼 과거 날짜의 목표·달성률은 바뀌지 않아요. 계산식·기준·출처·적용 범위는 [NUTRITION.md](NUTRITION.md)에 있어요.
- **운동 루틴 주간 자동 조정**: 매주 지난주 "오늘의 운동 목표" 완료율을 보고 루틴을 조정해요. 절반 미만이면 강도와 양을 낮추고, 모두 완료하면 늘려요(중량은 ACSM 권고 2~10% 범위 안). 자세한 규칙은 [NUTRITION.md](NUTRITION.md) 아래쪽에 있어요.
- **평가**: 합계·달성률은 코드로 계산해요. 목표가 없으면 달성률을 표시하지 않고, "이 날의 식사를 모두 기록했어요"를 체크하기 전에는 하루 전체가 부족/과다라고 단정하지 않아요.
- **추천 안전장치**: 알레르기·피하는 음식이 들어간 추천은 서버에서 한 번 더 걸러내고, 굶기·단식·만회 운동 같은 문장은 제거해요.
- **몸 상태**: 개인 설정의 "현재 몸 상태"에 키·몸무게·골격근량(선택)·운동 시 주의할 점을 입력해요. BMI와 골격근 비율은 코드로 계산하고 "계산값"으로 표시해요. 골격근량은 몸무게보다 작아야 저장돼요.
- **운동 루틴 추천 (바디 로그 탭)**: 몸 상태와 목표·운동 경험·장비·주당 횟수·1회 시간을 AI에 보내 이번 주 루틴을 받아요. 키와 몸무게가 없으면 요청하지 않아요. 결과는 "AI 추천 · 참고용"으로 표시하고, 형식 검증과 함께 1회 시간 상한을 지키게 하며, 벌칙·보상 운동 같은 동작은 서버에서 빼요. 사용자별로 마지막 루틴을 저장하고, 몸 상태나 운동 설정이 바뀌면 "다시 받기" 안내를 보여요. AI 오류일 때는 가짜 루틴 없이 오류만 보여요.
- **주간 편성·중량**: 개인 설정에서 휴식 요일을 고르면 나머지 요일(월~일 기준)에 하루 하나씩 루틴을 편성해요. 서버가 AI 결과가 운동 요일과 정확히 맞는지 확인해요. 휴식 요일을 아직 고르지 않았으면 주당 운동 횟수만큼 고르게 배치해요. 중량 운동에는 시작 중량(kg, 0.5kg 단위)과 설명(예: 한 손 기준)이 붙고, 몸무게의 1.5배(최대 150kg)를 넘는 값은 표시하지 않아요.
- **오늘의 운동 목표 (오늘 탭)**: 오늘 요일의 루틴 운동을 목록으로 보여주고, 운동마다 "완료하기"로 체크해요(진행 막대·오른쪽 링에 반영). 하나라도 완료한 날은 운동한 날로 기록돼서 연속 기록·도전과제·이번 달 운동 기록에 들어가요. 휴식 요일이면 목록 대신 "오늘은 쉬는 날"을 보여주고, 루틴이 없으면 바디 로그로 안내해요.
- **수준별 도전과제 (내 기록 탭)**: 운동 경험으로 수준(입문·초급·중급·상급)을 정하고, 수준에 맞는 목표치의 도전과제를 보여줘요(연속 기록, 이번 주 운동·식사, 이번 달 바디 로그·기록한 날, 입문은 첫 루틴 받기). 운동 경험이 비어 있으면 기록한 날 수로 수준을 정해요. 모두 저장된 기록으로 계산돼요.
- **연속 기록 (오늘 탭)**: 식사·바디 로그·오늘의 목표 중 하나라도 남긴 날이 며칠 이어졌는지 보여요. 오늘 아직 기록이 없으면 어제까지의 연속 일수를 보여주고, 가장 길었던 기록도 함께 알려줘요.
- **중복 방지**: 기록 생성 요청마다 고유 요청 ID를 보내 재시도·연속 클릭에도 한 번만 저장돼요.
- **데이터 보관**: `data/` 폴더를 백업하면 모든 기록과 사진이 보관돼요.
