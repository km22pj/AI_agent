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
| `HEALTHLINGO_ALLOWED_EMAILS` | 외부 공개 시 | 로그인을 허용할 Google 계정(쉼표 구분). 설정하면 목록에 없는 계정은 로그인 불가 |

값은 `.env` 파일이나 시스템 환경변수로 넣을 수 있어요. 서버는 값이 있는지만 출력하고 값 자체는 출력하지 않아요.

## Google 로그인 설정

1. [Google Cloud 콘솔](https://console.cloud.google.com/)에서 프로젝트를 만들거나 선택해요.
2. **Google 인증 플랫폼(Google Auth Platform)** → 브랜딩에서 앱 이름·지원 이메일을 입력하고, 대상에서 *외부*를 골라요. 이 앱은 기본 정보(이름·이메일·프로필)만 요청하므로 "테스트" 상태여도 **테스트 사용자 등록이 필요 없어요.** 누가 로그인할 수 있는지는 `.env`의 `HEALTHLINGO_ALLOWED_EMAILS`로 정해요.
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

Tailscale Funnel로 이 PC의 서버를 `https://내PC이름.tailXXXX.ts.net` 형태의 주소에 연결해요(실제 주소는 `tailscale funnel status`로 확인하고 `.env`의 `HEALTHLINGO_PUBLIC_URL`에 넣어요). PC와 서버가 켜져 있는 동안만 접속돼요.

**접근 제어 (두 단계)**
1. **초대 링크**: `HEALTHLINGO_INVITE_CODE`가 있으면 `주소/?invite=코드`로 처음 들어온 브라우저에만 접근 쿠키(180일)를 줘요. 쿠키가 없으면 앱 화면·API 모두 "초대받은 사람만 이용할 수 있어요" 안내만 보여요. 링크를 열면 주소창에서 코드는 바로 지워져요.
2. **허용 계정**: `HEALTHLINGO_ALLOWED_EMAILS`에 있는 Google 계정만 로그인돼요. 목록에서 빼면 이미 로그인한 사람도 다음 요청부터 막혀요.

**자주 쓰는 명령**
```powershell
.\.venv\Scripts\python.exe run.py --show-invite      # 공유할 초대 링크 보기
.\.venv\Scripts\python.exe run.py --new-invite-code  # 새 코드 만들기 → .env에 넣고 서버 재시작
& "$env:ProgramFiles\Tailscale\tailscale.exe" funnel status          # 공개 상태 확인
& "$env:ProgramFiles\Tailscale\tailscale.exe" funnel --https=443 off # 외부 공개 끄기
& "$env:ProgramFiles\Tailscale\tailscale.exe" funnel --bg 8000       # 다시 켜기
```

- 링크가 원치 않는 사람에게 퍼졌으면: 새 코드를 만들어 `.env`에 넣고 서버를 재시작하세요. 기존 링크와 그 링크로 받은 접근 쿠키가 모두 무효가 돼요. 허용 계정 목록이 있으면 링크가 퍼져도 목록 밖 계정은 로그인할 수 없어요.
- 사람 추가: `HEALTHLINGO_ALLOWED_EMAILS`에 Gmail 주소를 쉼표로 더하고 서버를 재시작한 뒤 초대 링크를 보내세요.
- Google 로그인: Google Cloud 콘솔의 OAuth 클라이언트 → **승인된 JavaScript 원본**에 외부 공개 주소(`HEALTHLINGO_PUBLIC_URL` 값)를 추가해야 외부에서 로그인돼요.
- Funnel은 `--bg`로 켜 두어서 PC를 재부팅해도 다시 켜져요(Tailscale 트레이 앱이 실행 중이어야 해요). 서버(`run.py`)는 직접 실행해야 해요.
- 외부 사용자의 AI 분석 비용은 내 Gemini 키로 나가요.

## 동작 요약

- **기록 분리**: 모든 조회·수정·삭제 쿼리가 로그인한 사용자 ID로 제한돼요. 다른 사람의 기록·사진 ID를 알아도 404가 돌아가요.
- **사진**: 브라우저에서 다시 압축(위치 정보 등 메타데이터 제거)한 뒤 업로드하고, 서버의 `data/uploads/`에 저장해요. 공개 경로가 없고 로그인한 소유자만 `/api/photos/...`로 받을 수 있어요.
- **음식 사진 흐름**: 사진 선택 → 업로드 → AI 인식 → 사용자가 이름·양·조리법 확인/수정 → 저장 → 영양 추정·피드백 → 합계·그래프 갱신. AI가 실패해도 기록은 저장돼요.
- **추정치 표시**: 영양 정보가 없으면 0이 아니라 "분석 전/정보 부족"으로 표시해요. 음식·양을 수정하면 이전 분석은 "재분석 필요"로 바뀌고 합계에서 빠져요.
- **평가**: 합계·달성률은 코드로 계산해요. 목표가 없으면 달성률을 표시하지 않고, "이 날의 식사를 모두 기록했어요"를 체크하기 전에는 하루 전체가 부족/과다라고 단정하지 않아요.
- **추천 안전장치**: 알레르기·피하는 음식이 들어간 추천은 서버에서 한 번 더 걸러내고, 굶기·단식·만회 운동 같은 문장은 제거해요.
- **중복 방지**: 기록 생성 요청마다 고유 요청 ID를 보내 재시도·연속 클릭에도 한 번만 저장돼요.
- **데이터 보관**: `data/` 폴더를 백업하면 모든 기록과 사진이 보관돼요.
