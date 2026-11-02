"""End-to-end API tests against a real server instance on a temporary data folder.

The AI and Google parts are replaced by local fakes:
- FakeAI returns canned structured responses (no Anthropic API call).
- Google ID tokens are signed with an RSA key generated in this test, and the
  server's key store is pointed at that key (no Google servers involved).
These prove our handling of the responses, not the external services themselves.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from backend import auth
from backend.ai import AIError, AIService, strip_unsafe_sentences, validate_nutrition
from backend.app import make_server
from backend.config import Settings

CLIENT_ID = "test-client.apps.googleusercontent.com"

# Synthetic "photos": the server only checks the file signature, the fake AI reads the marker.
KIMCHI_JPEG = b"\xff\xd8\xff\xe0" + b"KIMCHI" + bytes(200)
SALAD_JPEG = b"\xff\xd8\xff\xe0" + b"SALAD" + bytes(200)
DESK_PNG = b"\x89PNG\r\n\x1a\n" + b"DESK" + bytes(200)
BODY_JPEG = b"\xff\xd8\xff\xe0" + b"BODY" + bytes(300)
BODY2_JPEG = b"\xff\xd8\xff\xe0" + b"BODY2" + bytes(300)
THUMB = b"\xff\xd8\xff\xe0" + b"THUMB" + bytes(50)

FOODS = {
    "김치볶음밥": (520, 70, 14, 20),
    "계란후라이": (90, 0.5, 6, 7),
    "닭가슴살 샐러드": (250, 10, 35, 7),
}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class FakeAIClient:
    """Mimics AnthropicJSONClient.complete_json with deterministic answers."""

    def __init__(self):
        self.mode = "ok"
        self.calls = 0
        self.coach_recs = None

    def complete_json(self, system, content, schema, task=None):
        self.calls += 1
        if self.mode == "fail":
            raise AIError("AI 서비스에 연결하지 못했어요. 인터넷 연결을 확인하고 다시 시도해 주세요.", True, "ai_network")
        if "identify the foods" in system:
            data = base64.b64decode(content[0]["data"])
            if b"KIMCHI" in data:
                items = [
                    {"name": "김치볶음밥", "estimated_amount": "1공기", "amount_certain": False, "cooking_method": "볶음", "cooking_certain": True, "confidence": "high"},
                    {"name": "계란후라이", "estimated_amount": "1개", "amount_certain": True, "cooking_method": "튀김", "cooking_certain": False, "confidence": "medium"},
                ]
                return {"status": "recognized", "message": "볶음밥과 계란이 보여요.", "items": items, "questions": ["기름을 얼마나 썼나요?"]}
            if b"SALAD" in data:
                items = [{"name": "닭가슴살 샐러드", "estimated_amount": "1접시", "amount_certain": False, "cooking_method": "생것", "cooking_certain": True, "confidence": "high"}]
                return {"status": "recognized", "message": "샐러드가 보여요.", "items": items, "questions": ["드레싱을 뿌렸나요?"]}
            return {"status": "not_food", "message": "음식이 보이지 않아요.", "items": [], "questions": []}
        if "estimate nutrition" in system:
            payload = json.loads(content[0]["text"].split("\n", 1)[1])
            out = []
            for item in payload["confirmed_items"]:
                kcal, c, p, f = FOODS.get(item["name"], (300, 40, 10, 10))
                factor = 2 if "2" in item["amount"] else 1
                if self.mode == "invalid":
                    kcal = 5  # does not match the macros -> must be rejected
                out.append({"name": item["name"], "kcal": kcal * factor, "carbs_g": c * factor, "protein_g": p * factor, "fat_g": f * factor})
            return {"items": out, "confidence": "medium", "assumptions": ["일반적인 1인분 기준"], "feedback": "단백질이 들어 있어요. 굶어서 만회하세요.", "questions": []}
        if "meal coaching" in system:
            recs = self.coach_recs or [
                {"food": "두부 샐러드", "reason": "단백질을 보충할 수 있어요.", "ingredients": ["두부", "채소"]},
                {"food": "땅콩버터 토스트", "reason": "간편해요.", "ingredients": ["빵", "땅콩버터"]},
                {"food": "고등어구이", "reason": "단백질과 지방이 있어요.", "ingredients": ["고등어"]},
            ]
            return {"summary": "지금까지 기록 기준이에요.", "observations": ["단백질 기록이 있어요."], "recommendations": recs, "questions": ["저녁은 드셨나요?"]}
        raise AssertionError("unexpected prompt")


# ---------------------------------------------------------------- RSA helpers for fake Google tokens

def _is_probable_prime(n: int, rounds: int = 24) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    rng = random.Random(n)
    for _ in range(rounds):
        a = rng.randrange(2, n - 2)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random) -> int:
    while True:
        candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(candidate):
            return candidate


def make_rsa_key(seed: int = 7):
    rng = random.Random(seed)
    e = 65537
    while True:
        p, q = _prime(512, rng), _prime(512, rng)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e:
            n = p * q
            return n, e, pow(e, -1, phi)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt(claims: dict, key, kid: str = "k1") -> str:
    n, e, d = key
    header = b64url(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    signing_input = f"{header}.{payload}".encode()
    k = (n.bit_length() + 7) // 8
    t = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    sig = pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")
    return f"{header}.{payload}.{b64url(sig)}"


RSA_KEY = make_rsa_key()


def jwks_fetcher():
    n, e, _ = RSA_KEY
    return {"keys": [{"kty": "RSA", "kid": "k1", "n": b64url(n.to_bytes((n.bit_length() + 7) // 8, "big")), "e": b64url(e.to_bytes(3, "big"))}]}, 3600


# ---------------------------------------------------------------- HTTP client

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # let tests see 303 responses and their cookies


_OPENER = urllib.request.build_opener(_NoRedirect)


class Client:
    """Tiny browser stand-in: keeps cookies by name and never follows redirects."""

    def __init__(self, base: str):
        self.base = base
        self.jar: dict[str, str] = {}
        self._raw: str | None = None  # a hand-written Cookie header set by a test
        self.last_headers = None
        self.extra_headers: dict[str, str] = {}

    @property
    def cookie(self) -> str | None:
        if self._raw is not None:
            return self._raw
        return "; ".join(f"{k}={v}" for k, v in self.jar.items()) or None

    @cookie.setter
    def cookie(self, value: str | None) -> None:
        self._raw = value

    def request(self, method: str, path: str, body=None, csrf: bool = True, raw: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if csrf:
            req.add_header("X-Healthlingo", "1")
        for name, value in self.extra_headers.items():
            req.add_header(name, value)
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            with _OPENER.open(req, timeout=20) as resp:
                status, headers, payload = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, headers, payload = exc.code, exc.headers, exc.read()
            exc.close()
        self.last_headers = headers
        for set_cookie in headers.get_all("Set-Cookie") or []:
            self._raw = None
            name, _, value = set_cookie.split(";", 1)[0].partition("=")
            if value:
                self.jar[name] = value
            else:
                self.jar.pop(name, None)
        if raw:
            return status, payload
        is_json = (headers.get("Content-Type") or "").startswith("application/json")
        return status, (json.loads(payload) if payload and is_json else payload)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def patch(self, path, body, **kw):
        return self.request("PATCH", path, body, **kw)

    def put(self, path, body, **kw):
        return self.request("PUT", path, body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


_counter = [0]


def cid() -> str:
    _counter[0] += 1
    return f"test{_counter[0]:06d}abcdef"


class ServerTestCase(unittest.TestCase):
    ai_enabled = True
    extra_settings: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="healthlingo-test-"))
        self.fake = FakeAIClient()
        self.start_server()

    def start_server(self, **overrides):
        options = {**self.extra_settings, **overrides}
        settings = Settings(host="127.0.0.1", port=0, data_dir=self.tmp, google_client_id=CLIENT_ID, dev_login=True, ai_model="fake-model", **options)
        ai = AIService(self.fake if self.ai_enabled else None)
        self.server = make_server(settings, ai=ai, key_store=auth.GoogleKeyStore(fetcher=jwks_fetcher))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def tearDown(self):
        self.stop_server()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, name="사용자A") -> Client:
        client = Client(self.base)
        status, body = client.post("/api/auth/dev", {"name": name})
        self.assertEqual(status, 200, body)
        return client

    def upload(self, client, data, kind="meal"):
        status, body = client.post("/api/photos", {"client_id": cid(), "kind": kind, "image": b64(data), "thumb": b64(THUMB)})
        self.assertEqual(status, 201, body)
        return body["photo"]

    def create_meal(self, client, date="2026-10-29", items=None, meal_type="lunch", **extra):
        body = {"client_id": cid(), "date": date, "time": "12:30", "meal_type": meal_type, "items": items or [{"name": "김치볶음밥", "amount": "1공기", "cooking": "볶음"}], "memo": "", **extra}
        status, res = client.post("/api/meals", body)
        self.assertIn(status, (200, 201), res)
        return res["meal"]


# ---------------------------------------------------------------- tests

class NewUserAndAuthTests(ServerTestCase):
    def test_new_user_sees_no_fake_records(self):
        a = self.login()
        status, ov = a.get("/api/overview?today=2026-10-30")
        self.assertEqual(status, 200)
        self.assertFalse(any(d["has_record"] for d in ov["week"]))
        self.assertEqual(ov["month"], {**ov["month"], "movement": 0, "meals": 0, "body_logs": 0, "record_days": 0})
        self.assertEqual(ov["total_record_days"], 0)
        self.assertEqual(ov["badges"], {"first_record": False, "restart": False, "routine": False})
        self.assertEqual(ov["missions"], {"warmup": False, "movement": False, "record": False})
        self.assertEqual(a.get("/api/meals?date=2026-10-30")[1]["meals"], [])
        self.assertEqual(a.get("/api/body-logs")[1]["body_logs"], [])
        day = a.get("/api/days/2026-10-30")[1]
        self.assertEqual(day["summary"]["state"], "empty")
        # Missing nutrition is None, never 0.
        self.assertTrue(all(v["value"] is None for v in day["summary"]["totals"].values()))
        settings = a.get("/api/settings")[1]["settings"]
        self.assertEqual(settings["targets"], {"kcal": None, "carbs_g": None, "protein_g": None, "fat_g": None})

    def test_login_required_and_csrf_header(self):
        anon = Client(self.base)
        self.assertEqual(anon.get("/api/meals?date=2026-10-30")[0], 401)
        a = self.login()
        status, body = a.post("/api/meals", {"client_id": cid()}, csrf=False)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "csrf")

    def test_session_survives_google_g_state_cookie(self):
        # Google Sign-In leaves `g_state={"i_l":0,...}` on logout; it used to make the
        # server drop every cookie, so signing in again "failed" with a 401.
        a = self.login()
        session = a.cookie
        self.assertEqual(a.post("/api/auth/logout")[0], 200)
        relogin = self.login()
        relogin.cookie = 'g_state={"i_l":0,"i_ll":1761900000000}; ' + relogin.cookie + '; other="x y"; broken'
        status, body = relogin.get("/api/me")
        self.assertEqual(status, 200, body)
        stale = Client(self.base)
        stale.cookie = 'g_state={"i_l":0}; ' + session
        self.assertEqual(stale.get("/api/me")[0], 401)  # the logged-out session stays dead

    def test_read_cookie(self):
        from backend.app import read_cookie

        self.assertEqual(read_cookie(['g_state={"i_l":0,"x":1}; hl_session=abc'], "hl_session"), "abc")
        self.assertEqual(read_cookie(["a=1", "hl_session=xyz"], "hl_session"), "xyz")
        self.assertIsNone(read_cookie(["xhl_session=1; hl_sessionx=2"], "hl_session"))
        self.assertIsNone(read_cookie(["hl_session="], "hl_session"))

    def test_logout_invalidates_session(self):
        a = self.login()
        token_cookie = a.cookie
        self.assertEqual(a.post("/api/auth/logout")[0], 200)
        self.assertIsNone(a.cookie)
        stale = Client(self.base)
        stale.cookie = token_cookie
        self.assertEqual(stale.get("/api/me")[0], 401)

    def test_google_id_token_verification(self):
        now = int(time.time())
        good = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1234567890", "email": "a@example.com", "name": "구글 사용자", "iat": now, "exp": now + 600}
        c = Client(self.base)
        status, body = c.post("/api/auth/google", {"credential": sign_jwt(good, RSA_KEY)})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["user"]["email"], "a@example.com")
        self.assertEqual(c.get("/api/me")[0], 200)

        cases = {
            "wrong audience": {**good, "aud": "someone-else"},
            "expired": {**good, "exp": now - 3600},
            "wrong issuer": {**good, "iss": "https://evil.example"},
        }
        for label, claims in cases.items():
            with self.subTest(label):
                status, body = Client(self.base).post("/api/auth/google", {"credential": sign_jwt(claims, RSA_KEY)})
                self.assertEqual(status, 401, body)
        token = sign_jwt(good, RSA_KEY)
        head, payload, sig = token.split(".")
        forged = b64url(json.dumps({**good, "sub": "attacker"}).encode())
        self.assertEqual(Client(self.base).post("/api/auth/google", {"credential": f"{head}.{forged}.{sig}"})[0], 401)
        self.assertEqual(Client(self.base).post("/api/auth/google", {"credential": "not-a-token"})[0], 401)

    def test_google_login_refused_when_not_configured(self):
        self.stop_server()
        settings = Settings(host="127.0.0.1", port=0, data_dir=self.tmp, google_client_id="", dev_login=False)
        self.server = make_server(settings, ai=AIService(None), key_store=auth.GoogleKeyStore(fetcher=jwks_fetcher))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        c = Client(self.base)
        cfg = c.get("/api/config")[1]
        self.assertFalse(cfg["google_configured"])
        self.assertFalse(cfg["dev_login_enabled"])
        status, body = c.post("/api/auth/google", {"credential": "x.y.z"})
        self.assertEqual(status, 503)
        self.assertEqual(c.post("/api/auth/dev", {"name": "x"})[0], 404)


class IsolationAndPersistenceTests(ServerTestCase):
    def test_users_cannot_touch_each_others_records(self):
        a, b = self.login("사용자A"), self.login("사용자B")
        photo = self.upload(a, KIMCHI_JPEG)
        meal = self.create_meal(a, photo_id=photo["id"])
        body_photo = self.upload(a, BODY_JPEG, kind="body")
        status, res = a.post("/api/body-logs", {"client_id": cid(), "date": "2026-10-29", "note": "A의 메모", "photo_id": body_photo["id"]})
        self.assertEqual(status, 201)
        log = res["body_log"]

        self.assertEqual(b.get("/api/meals?date=2026-10-29")[1]["meals"], [])
        self.assertEqual(b.get("/api/body-logs")[1]["body_logs"], [])
        for method, path, body in [
            ("GET", f"/api/meals/{meal['id']}", None),
            ("PATCH", f"/api/meals/{meal['id']}", {"memo": "hack"}),
            ("DELETE", f"/api/meals/{meal['id']}", None),
            ("POST", f"/api/meals/{meal['id']}/analyze", {}),
            ("GET", f"/api/photos/{photo['id']}", None),
            ("GET", f"/api/photos/{body_photo['id']}/thumb", None),
            ("POST", f"/api/photos/{photo['id']}/recognize", {}),
            ("GET", f"/api/body-logs/{log['id']}", None),
            ("PATCH", f"/api/body-logs/{log['id']}", {"note": "hack"}),
            ("DELETE", f"/api/body-logs/{log['id']}", None),
        ]:
            with self.subTest(f"{method} {path}"):
                self.assertEqual(b.request(method, path, body)[0], 404)
        # B also cannot attach A's photo to their own record.
        status, res = b.post("/api/meals", {"client_id": cid(), "date": "2026-10-29", "meal_type": "lunch", "items": [{"name": "x"}], "photo_id": photo["id"]})
        self.assertEqual(status, 400)
        # A's data is untouched.
        self.assertEqual(a.get(f"/api/meals/{meal['id']}")[1]["meal"]["memo"], "")
        self.assertEqual(a.get(f"/api/body-logs/{log['id']}")[1]["body_log"]["note"], "A의 메모")
        status, raw = a.get(f"/api/photos/{photo['id']}", raw=True)
        self.assertEqual((status, raw), (200, KIMCHI_JPEG))

    def test_records_and_photos_survive_restart(self):
        a = self.login()
        photo = self.upload(a, KIMCHI_JPEG)
        meal = self.create_meal(a, photo_id=photo["id"])
        self.assertEqual(a.post(f"/api/meals/{meal['id']}/analyze")[0], 200)
        body_photo = self.upload(a, BODY_JPEG, kind="body")
        a.post("/api/body-logs", {"client_id": cid(), "date": "2026-10-20", "note": "재시작 전", "photo_id": body_photo["id"]})
        a.put("/api/settings", {"goal_type": "근육 증가", "targets": {"protein_g": 120}})

        self.stop_server()
        self.start_server()
        again = self.login()  # same dev account → same user
        meals = again.get("/api/meals?date=2026-10-29")[1]["meals"]
        self.assertEqual(len(meals), 1)
        self.assertEqual(meals[0]["nutrition_status"], "ai")
        self.assertEqual(meals[0]["nutrition"]["total"]["kcal"], 520)
        logs = again.get("/api/body-logs")[1]["body_logs"]
        self.assertEqual([l["note"] for l in logs], ["재시작 전"])
        self.assertEqual(again.get(logs[0]["photo"]["url"], raw=True), (200, BODY_JPEG))
        self.assertEqual(again.get(meals[0]["photo"]["thumb_url"], raw=True), (200, THUMB))
        settings = again.get("/api/settings")[1]["settings"]
        self.assertEqual((settings["goal_type"], settings["targets"]["protein_g"]), ("근육 증가", 120))


class MealAnalysisTests(ServerTestCase):
    def test_each_photo_gets_its_own_analysis_and_chart(self):
        a = self.login()
        p1 = self.upload(a, KIMCHI_JPEG)
        p2 = self.upload(a, SALAD_JPEG)
        r1 = a.post(f"/api/photos/{p1['id']}/recognize")[1]["recognition"]
        r2 = a.post(f"/api/photos/{p2['id']}/recognize")[1]["recognition"]
        self.assertEqual([i["name"] for i in r1["items"]], ["김치볶음밥", "계란후라이"])
        self.assertEqual([i["name"] for i in r2["items"]], ["닭가슴살 샐러드"])
        self.assertFalse(r1["items"][0]["amount_certain"])  # amount must be confirmed by the user
        self.assertEqual(r1["photo_id"], p1["id"])
        self.assertEqual(r2["photo_id"], p2["id"])

        to_items = lambda rec: [{"name": i["name"], "amount": i["amount"], "cooking": i["cooking"]} for i in rec["items"]]  # noqa: E731
        m1 = self.create_meal(a, date="2026-10-28", items=to_items(r1), photo_id=p1["id"])
        m2 = self.create_meal(a, date="2026-10-29", items=to_items(r2), photo_id=p2["id"])
        self.assertEqual(a.post(f"/api/meals/{m1['id']}/analyze")[0], 200)
        self.assertEqual(a.post(f"/api/meals/{m2['id']}/analyze")[0], 200)

        d1 = a.get("/api/days/2026-10-28")[1]["summary"]["totals"]
        d2 = a.get("/api/days/2026-10-29")[1]["summary"]["totals"]
        self.assertEqual((d1["kcal"]["value"], d1["protein_g"]["value"]), (610, 20))  # 520+90, 14+6
        self.assertEqual((d2["kcal"]["value"], d2["protein_g"]["value"]), (250, 35))
        stats = a.get("/api/stats/range?from=2026-10-27&to=2026-10-29")[1]["days"]
        self.assertEqual([d["totals"]["kcal"] for d in stats], [None, 610, 250])

    def test_unrecognizable_photo(self):
        a = self.login()
        p = self.upload(a, DESK_PNG)
        rec = a.post(f"/api/photos/{p['id']}/recognize")[1]["recognition"]
        self.assertEqual(rec["status"], "not_food")
        self.assertEqual(rec["items"], [])

    def test_edit_updates_same_record_and_marks_reanalysis(self):
        a = self.login()
        body = {"client_id": cid(), "date": "2026-10-29", "time": "08:10", "meal_type": "breakfast", "items": [{"name": "김치볶음밥", "amount": "1공기", "cooking": "볶음"}], "memo": ""}
        first = a.post("/api/meals", body)
        repeat = a.post("/api/meals", body)  # double click / retry with same request id
        self.assertEqual(first[0], 201)
        self.assertEqual(repeat[0], 200)
        self.assertEqual(first[1]["meal"]["id"], repeat[1]["meal"]["id"])
        meal_id = first[1]["meal"]["id"]
        self.assertEqual(a.post(f"/api/meals/{meal_id}/analyze")[1]["meal"]["nutrition_status"], "ai")

        status, res = a.patch(f"/api/meals/{meal_id}", {"memo": "참기름 조금", "time": "08:30"})
        self.assertEqual(res["meal"]["nutrition_status"], "ai")  # memo/time edits keep the analysis
        self.assertEqual(res["meal"]["memo"], "참기름 조금")

        status, res = a.patch(f"/api/meals/{meal_id}", {"items": [{"name": "김치볶음밥", "amount": "2공기", "cooking": "볶음"}]})
        self.assertEqual(status, 200)
        meal = res["meal"]
        self.assertEqual(meal["nutrition_status"], "stale")
        self.assertIsNone(meal["nutrition"])  # old numbers are not presented as current
        summary = a.get("/api/days/2026-10-29")[1]["summary"]
        self.assertEqual((summary["counted"], summary["uncounted"]), (0, 1))
        self.assertTrue(summary["incomplete_data"])
        self.assertIsNone(summary["totals"]["kcal"]["value"])

        meal = a.post(f"/api/meals/{meal_id}/analyze")[1]["meal"]
        self.assertEqual((meal["nutrition_status"], meal["nutrition"]["total"]["kcal"]), ("ai", 1040))
        self.assertEqual(len(a.get("/api/meals?date=2026-10-29")[1]["meals"]), 1)

    def test_concurrent_duplicate_creates_make_one_record(self):
        a = self.login()
        body = {"client_id": cid(), "date": "2026-10-29", "meal_type": "snack", "items": [{"name": "사과"}]}
        results = []
        threads = [threading.Thread(target=lambda: results.append(a.post("/api/meals", body))) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({r[1]["meal"]["id"] for r in results}), 1)
        self.assertEqual(len(a.get("/api/meals?date=2026-10-29")[1]["meals"]), 1)

    def test_manual_nutrition_keeps_unknowns_unknown(self):
        a = self.login()
        self.create_meal(a, items=[{"name": "편의점 샌드위치"}], manual_nutrition={"kcal": 350})
        self.create_meal(a, items=[{"name": "물"}], meal_type="snack")
        summary = a.get("/api/days/2026-10-29")[1]["summary"]
        self.assertEqual(summary["totals"]["kcal"]["value"], 350)
        self.assertIsNone(summary["totals"]["protein_g"]["value"])
        self.assertEqual(summary["totals"]["protein_g"]["missing"], 2)
        self.assertEqual(summary["uncounted"], 1)
        self.assertTrue(summary["incomplete_data"])

    def test_past_meal_view_edit_delete(self):
        a = self.login()
        photo = self.upload(a, SALAD_JPEG)
        meal = self.create_meal(a, date="2026-01-15", meal_type="dinner", photo_id=photo["id"])
        self.assertEqual(len(a.get("/api/meals?date=2026-01-15")[1]["meals"]), 1)
        self.assertEqual(a.get("/api/meals?date=2026-01-16")[1]["meals"], [])
        res = a.patch(f"/api/meals/{meal['id']}", {"meal_type": "lunch", "date": "2026-01-14"})[1]["meal"]
        self.assertEqual((res["meal_type"], res["date"]), ("lunch", "2026-01-14"))
        self.assertEqual(a.get("/api/meals?date=2026-01-15")[1]["meals"], [])
        self.assertEqual(a.delete(f"/api/meals/{meal['id']}")[0], 200)
        self.assertEqual(a.get("/api/meals?date=2026-01-14")[1]["meals"], [])
        self.assertEqual(a.get(f"/api/photos/{photo['id']}")[0], 404)  # photo removed with the record

    def test_targets_drive_evaluation_without_overreaching(self):
        a = self.login()
        meal = self.create_meal(a)
        a.post(f"/api/meals/{meal['id']}/analyze")
        summary = a.get("/api/days/2026-10-29")[1]["summary"]
        self.assertFalse(summary["has_targets"])
        self.assertIsNone(summary["progress"]["protein_g"]["pct"])
        self.assertIn("set_targets", [q["id"] for q in summary["questions"]])

        a.put("/api/settings", {"targets": {"kcal": 2000, "protein_g": 100}})
        summary = a.get("/api/days/2026-10-29")[1]["summary"]
        self.assertEqual(summary["progress"]["protein_g"]["pct"], 14)
        self.assertEqual(summary["state"], "partial")
        self.assertFalse(any("부족" in m["text"] for m in summary["messages"]))  # one meal ≠ whole day
        self.assertIn("mark_complete", [q["id"] for q in summary["questions"]])

        summary = a.put("/api/days/2026-10-29", {"complete": True})[1]["summary"]
        self.assertEqual(summary["state"], "complete")
        low = [m["text"] for m in summary["messages"] if m["level"] == "low"]
        self.assertTrue(any("단백질" in t and "14/100g" in t for t in low), low)

    def test_unrealistic_targets_rejected(self):
        a = self.login()
        status, body = a.put("/api/settings", {"targets": {"kcal": 600}})
        self.assertEqual(status, 400)
        self.assertIn("1000", body["error"]["message"])


class AIFailureTests(ServerTestCase):
    def test_ai_failure_keeps_record_and_retry_works(self):
        a = self.login()
        meal = self.create_meal(a)
        self.fake.mode = "fail"
        status, body = a.post(f"/api/meals/{meal['id']}/analyze")
        self.assertEqual(status, 502)
        self.assertTrue(body["error"]["retryable"])
        self.assertEqual(body["meal"]["nutrition_status"], "failed")
        saved = a.get(f"/api/meals/{meal['id']}")[1]["meal"]
        self.assertEqual(saved["nutrition_status"], "failed")
        self.assertIn("연결", saved["analysis_error"])
        self.assertIsNone(saved["nutrition"])  # no made-up numbers

        p = self.upload(a, KIMCHI_JPEG)
        status, body = a.post(f"/api/photos/{p['id']}/recognize")
        self.assertEqual((status, body["error"]["retryable"]), (502, True))

        self.fake.mode = "ok"
        self.assertEqual(a.post(f"/api/meals/{meal['id']}/analyze")[1]["meal"]["nutrition_status"], "ai")

    def test_invalid_ai_response_is_rejected(self):
        a = self.login()
        meal = self.create_meal(a)
        self.fake.mode = "invalid"
        status, body = a.post(f"/api/meals/{meal['id']}/analyze")
        self.assertEqual((status, body["error"]["code"]), (502, "ai_invalid"))
        self.assertIsNone(a.get(f"/api/meals/{meal['id']}")[1]["meal"]["nutrition"])

    def test_unsafe_advice_is_removed(self):
        a = self.login()
        meal = self.create_meal(a)
        feedback = a.post(f"/api/meals/{meal['id']}/analyze")[1]["meal"]["analysis"]["feedback"]
        self.assertIn("단백질", feedback)
        self.assertNotIn("굶", feedback)
        cleaned, removed = strip_unsafe_sentences("좋아요. 저녁은 굶으세요. 물을 드세요.")
        self.assertTrue(removed)
        self.assertNotIn("굶", cleaned)

    def test_coach_respects_allergies_and_goes_stale(self):
        a = self.login()
        a.put("/api/settings", {"allergies": "땅콩", "avoid_foods": ["고등어"]})
        meal = self.create_meal(a)
        a.post(f"/api/meals/{meal['id']}/analyze")
        day = a.post("/api/days/2026-10-29/coach")[1]
        foods = [r["food"] for r in day["coach"]["recommendations"]]
        self.assertEqual(foods, ["두부 샐러드"])
        self.assertEqual(day["coach"]["filtered_recommendations"], 2)
        self.assertFalse(day["coach"]["stale"])
        self.create_meal(a, items=[{"name": "바나나"}], meal_type="snack")
        self.assertTrue(a.get("/api/days/2026-10-29")[1]["coach"]["stale"])

    def test_validator_unit(self):
        with self.assertRaises(AIError):
            validate_nutrition({"items": [{"name": "x", "kcal": -1, "carbs_g": 0, "protein_g": 0, "fat_g": 0}], "confidence": "low", "assumptions": [], "feedback": "", "questions": []}, [{"name": "x"}])
        with self.assertRaises(AIError):
            validate_nutrition({"items": [], "confidence": "low", "assumptions": [], "feedback": "", "questions": []}, [{"name": "x"}])


class AINotConfiguredTests(ServerTestCase):
    ai_enabled = False

    def test_manual_records_work_without_ai(self):
        a = self.login()
        self.assertFalse(a.get("/api/config")[1]["ai_configured"])
        photo = self.upload(a, KIMCHI_JPEG)
        status, body = a.post(f"/api/photos/{photo['id']}/recognize")
        self.assertEqual((status, body["error"]["code"]), (503, "ai_not_configured"))
        meal = self.create_meal(a, photo_id=photo["id"])
        status, body = a.post(f"/api/meals/{meal['id']}/analyze")
        self.assertEqual(status, 503)
        self.assertEqual(a.get(f"/api/meals/{meal['id']}")[1]["meal"]["nutrition_status"], "none")
        status, body = a.post("/api/days/2026-10-29/coach")
        self.assertEqual(status, 503)


class BodyLogTests(ServerTestCase):
    def test_dates_detail_gallery_and_edit(self):
        a = self.login()
        p1 = self.upload(a, BODY_JPEG, kind="body")
        logs = []
        for date, note, photo in [("2026-09-03", "9월 첫 기록", p1["id"]), ("2026-10-12", "아침", None), ("2026-10-12", "저녁", None), ("2026-10-25", "최근", None)]:
            status, res = a.post("/api/body-logs", {"client_id": cid(), "date": date, "note": note, "photo_id": photo})
            self.assertEqual(status, 201, res)
            logs.append(res["body_log"])

        month = a.get("/api/body-logs?from=2026-09-28&to=2026-11-08&order=asc")[1]["body_logs"]
        self.assertEqual(sorted({l["date"] for l in month}), ["2026-10-12", "2026-10-25"])
        self.assertEqual(len([l for l in month if l["date"] == "2026-10-12"]), 2)  # several per day
        self.assertEqual(a.get("/api/body-logs?from=2026-10-13&to=2026-10-13")[1]["body_logs"], [])  # empty day

        desc = [l["date"] for l in a.get("/api/body-logs?order=desc")[1]["body_logs"]]
        asc = [l["date"] for l in a.get("/api/body-logs?order=asc")[1]["body_logs"]]
        self.assertEqual(desc, sorted(desc, reverse=True))
        self.assertEqual(asc, sorted(asc))
        self.assertEqual(len(a.get("/api/body-logs?from=2026-10-01&to=2026-10-31")[1]["body_logs"]), 3)

        first = logs[0]
        detail = a.get(f"/api/body-logs/{first['id']}")[1]["body_log"]
        self.assertEqual(a.get(detail["photo"]["url"], raw=True), (200, BODY_JPEG))

        p2 = self.upload(a, BODY2_JPEG, kind="body")
        res = a.patch(f"/api/body-logs/{first['id']}", {"note": "수정한 메모", "date": "2026-09-04", "photo_id": p2["id"]})[1]["body_log"]
        self.assertEqual((res["note"], res["date"], res["photo"]["id"]), ("수정한 메모", "2026-09-04", p2["id"]))
        self.assertEqual(a.get(f"/api/photos/{p1['id']}")[0], 404)  # replaced photo is deleted
        self.assertEqual(len(a.get("/api/body-logs")[1]["body_logs"]), 4)  # edit did not duplicate

        self.assertEqual(a.delete(f"/api/body-logs/{first['id']}")[0], 200)
        self.assertEqual(a.get(f"/api/photos/{p2['id']}")[0], 404)
        self.assertEqual(len(a.get("/api/body-logs")[1]["body_logs"]), 3)

    def test_requires_photo_or_note_and_right_photo_kind(self):
        a = self.login()
        status, body = a.post("/api/body-logs", {"client_id": cid(), "date": "2026-10-01", "note": ""})
        self.assertEqual(status, 400)
        meal_photo = self.upload(a, KIMCHI_JPEG)
        status, body = a.post("/api/body-logs", {"client_id": cid(), "date": "2026-10-01", "photo_id": meal_photo["id"]})
        self.assertEqual(status, 400)
        bad = a.post("/api/photos", {"client_id": cid(), "kind": "body", "image": b64(b"not an image at all"), "thumb": b64(THUMB)})
        self.assertEqual(bad[0], 400)

    def test_photo_upload_is_idempotent(self):
        a = self.login()
        body = {"client_id": cid(), "kind": "body", "image": b64(BODY_JPEG), "thumb": b64(THUMB)}
        first, second = a.post("/api/photos", body), a.post("/api/photos", body)
        self.assertEqual(first[1]["photo"]["id"], second[1]["photo"]["id"])

    def test_overview_reflects_saved_records(self):
        a = self.login()
        a.post("/api/body-logs", {"client_id": cid(), "date": "2026-10-30", "note": "오늘"})
        self.create_meal(a, date="2026-10-27")
        a.post("/api/activities", {"date": "2026-10-30", "kind": "movement"})
        ov = a.get("/api/overview?today=2026-10-30")[1]
        recorded = [d["date"] for d in ov["week"] if d["has_record"]]
        self.assertEqual(recorded, ["2026-10-27", "2026-10-30"])
        self.assertEqual(ov["missions"], {"warmup": False, "movement": True, "record": True})
        self.assertEqual((ov["month"]["movement"], ov["month"]["meals"], ov["month"]["body_logs"], ov["month"]["record_days"]), (1, 1, 1, 2))
        self.assertTrue(ov["badges"]["first_record"])
        a.delete("/api/activities?date=2026-10-30&kind=movement")
        self.assertFalse(a.get("/api/overview?today=2026-10-30")[1]["missions"]["movement"])


INVITE = "test-invite-code-0123456789abcdef"
PUBLIC = "https://my-pc.tail1234.ts.net"
ALLOWED = "friend@example.com"


def google_token(email: str, verified: bool = True, sub: str | None = None) -> str:
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": sub or f"sub-{email}", "email": email,
              "email_verified": verified, "name": email.split("@")[0], "iat": now, "exp": now + 600}
    return sign_jwt(claims, RSA_KEY)


class AccessControlTests(ServerTestCase):
    """Invite link + allowed Google accounts (settings used when the site is public)."""

    extra_settings = {"invite_code": INVITE, "allowed_emails": [ALLOWED], "public_url": PUBLIC}

    def invited(self) -> Client:
        c = Client(self.base)
        status, _ = c.get(f"/?invite={INVITE}")
        self.assertEqual(status, 303)
        return c

    def test_without_invite_nothing_is_reachable(self):
        c = Client(self.base)
        status, page = c.get("/")
        self.assertEqual(status, 403)
        self.assertIn("초대받은 사람만", page.decode("utf-8"))
        self.assertNotIn(b"app.js", page)  # the app itself is not served
        self.assertEqual(c.get("/app.js")[0], 403)
        status, body = c.get("/api/config")
        self.assertEqual((status, body["error"]["code"]), (403, "invite_required"))
        status, body = c.post("/api/auth/google", {"credential": google_token(ALLOWED)})
        self.assertEqual((status, body["error"]["code"]), (403, "invite_required"))
        status, page = c.get("/?invite=wrong-code")
        self.assertEqual(status, 403)
        self.assertIn("올바르지 않거나", page.decode("utf-8"))
        self.assertNotIn("hl_access", c.jar)

    def test_invite_link_lets_the_browser_in(self):
        c = Client(self.base)
        status, _ = c.get(f"/?invite={INVITE}")
        self.assertEqual(status, 303)
        self.assertEqual(c.last_headers["Location"], "/")  # code removed from the address bar
        set_cookie = c.last_headers["Set-Cookie"]
        for flag in ("HttpOnly", "SameSite=Lax", "Secure"):
            self.assertIn(flag, set_cookie)
        self.assertNotIn(INVITE, set_cookie)  # the cookie is a signature, not the code
        self.assertEqual(c.get("/")[0], 200)
        cfg = c.get("/api/config")[1]
        self.assertTrue(cfg["invite_only"] and cfg["allow_list"])
        self.assertFalse(cfg["dev_login_enabled"])  # never available once the site is public
        self.assertEqual(c.post("/api/auth/dev", {"name": "x"})[0], 404)

    def test_only_allowed_google_accounts_can_sign_in(self):
        c = self.invited()
        status, body = c.post("/api/auth/google", {"credential": google_token("stranger@example.com")})
        self.assertEqual((status, body["error"]["code"]), (403, "not_invited"))
        self.assertNotIn("hl_session", c.jar)
        status, body = c.post("/api/auth/google", {"credential": google_token(ALLOWED, verified=False)})
        self.assertEqual(status, 403)
        status, body = c.post("/api/auth/google", {"credential": google_token("Friend@Example.com")})
        self.assertEqual(status, 200, body)  # email match ignores case
        self.assertEqual(c.get("/api/me")[0], 200)

    def test_removing_an_account_or_changing_the_code_revokes_access(self):
        c = self.invited()
        self.assertEqual(c.post("/api/auth/google", {"credential": google_token(ALLOWED)})[0], 200)
        self.stop_server()
        self.start_server(allowed_emails=["someone-else@example.com"])
        c.base = self.base
        status, body = c.get("/api/me")
        self.assertEqual((status, body["error"]["code"]), (403, "not_invited"))
        self.stop_server()
        self.start_server(invite_code="a-brand-new-invite-code-987654321")
        c.base = self.base
        self.assertEqual(c.get("/")[0], 403)  # old invite cookie no longer works
        self.assertEqual(c.get("/api/me")[1]["error"]["code"], "invite_required")

    def test_requests_through_a_tunnel_pass_the_origin_check(self):
        c = self.invited()
        c.extra_headers = {"Origin": PUBLIC}  # Host stays 127.0.0.1:port, like a rewriting tunnel
        self.assertEqual(c.post("/api/auth/google", {"credential": google_token(ALLOWED)})[0], 200)
        self.assertEqual(c.post("/api/auth/logout")[0], 200)
        c.extra_headers = {"Origin": "https://evil.example"}
        status, body = c.post("/api/auth/logout")
        self.assertEqual((status, body["error"]["code"]), (403, "csrf"))

    def test_allow_list_edits_apply_without_restart(self):
        import os as _os

        env_file = self.tmp / "test.env"
        env_file.write_text(f"GEMINI_API_KEY=keep-me\nHEALTHLINGO_ALLOWED_EMAILS={ALLOWED}\n", encoding="utf-8")
        self.stop_server()
        self.start_server(env_file=env_file)
        teammate = "teammate@example.com"

        def bump(text):
            env_file.write_text(text, encoding="utf-8")
            stamp = time.time() + 5
            _os.utime(env_file, (stamp, stamp))  # make sure the change is visible even on coarse clocks

        c = self.invited()
        status, body = c.post("/api/auth/google", {"credential": google_token(teammate)})
        self.assertEqual(status, 403)
        self.assertIn(teammate, body["error"]["message"])  # the person sees which address to send to the owner

        bump(f"GEMINI_API_KEY=keep-me\nHEALTHLINGO_ALLOWED_EMAILS={ALLOWED}, Teammate@Example.com\n")
        self.assertEqual(c.post("/api/auth/google", {"credential": google_token(teammate)})[0], 200)
        self.assertEqual(c.get("/api/me")[0], 200)

        bump(f"GEMINI_API_KEY=keep-me\nHEALTHLINGO_ALLOWED_EMAILS={ALLOWED}\n")
        status, body = c.get("/api/me")
        self.assertEqual((status, body["error"]["code"]), (403, "not_invited"))

    def test_without_allow_list_any_google_account_signs_in(self):
        self.stop_server()
        self.start_server(allowed_emails=[])  # invite link still required
        self.assertEqual(Client(self.base).post("/api/auth/google", {"credential": google_token("anyone@example.com")})[1]["error"]["code"], "invite_required")
        c = self.invited()
        for email in ("anyone@example.com", "another.person@gmail.com"):
            status, body = c.post("/api/auth/google", {"credential": google_token(email)})
            self.assertEqual(status, 200, body)
            self.assertEqual(c.get("/api/me")[1]["user"]["email"], email)
        self.assertFalse(c.get("/api/config")[1]["allow_list"])

    def test_unverified_email_gets_its_own_message(self):
        c = self.invited()
        status, body = c.post("/api/auth/google", {"credential": google_token(ALLOWED, verified=False)})
        self.assertEqual(status, 403)
        self.assertIn("인증", body["error"]["message"])

    def test_invite_code_is_hidden_in_request_logs(self):
        from backend.app import INVITE_IN_LOG

        line = f'"GET /?invite={INVITE}&x=1 HTTP/1.1" 303 -'
        self.assertNotIn(INVITE, INVITE_IN_LOG.sub(r"\1***", line))


class ExposureSettingsTests(unittest.TestCase):
    def test_dev_login_turns_off_when_exposed(self):
        self.assertTrue(Settings(dev_login=True).dev_login_allowed)
        self.assertFalse(Settings(dev_login=True, public_url=PUBLIC).dev_login_allowed)
        self.assertFalse(Settings(dev_login=True, invite_code=INVITE).dev_login_allowed)
        self.assertFalse(Settings(dev_login=True, host="0.0.0.0").dev_login_allowed)

    def test_env_helpers_edit_only_their_key(self):
        from backend.config import parse_emails, read_env_value, set_env_value

        path = Path(tempfile.mkdtemp()) / ".env"
        path.write_text("# comment\nGEMINI_API_KEY=secret-value\n# HEALTHLINGO_ALLOWED_EMAILS=old@x.com\nHEALTHLINGO_ALLOWED_EMAILS=\"A@x.com, b@y.com\"\n", encoding="utf-8")
        self.assertEqual(parse_emails(read_env_value(path, "HEALTHLINGO_ALLOWED_EMAILS")), ["a@x.com", "b@y.com"])
        set_env_value(path, "HEALTHLINGO_ALLOWED_EMAILS", "a@x.com,b@y.com,c@z.com")
        text = path.read_text(encoding="utf-8")
        self.assertIn("GEMINI_API_KEY=secret-value", text)
        self.assertIn("# HEALTHLINGO_ALLOWED_EMAILS=old@x.com", text)  # commented line kept as-is
        self.assertEqual(read_env_value(path, "HEALTHLINGO_ALLOWED_EMAILS"), "a@x.com,b@y.com,c@z.com")
        self.assertEqual(text.count("HEALTHLINGO_ALLOWED_EMAILS=a@x.com"), 1)

    def test_public_url_normalized(self):
        from backend.config import normalize_origin

        self.assertEqual(normalize_origin("https://My-PC.tail1234.ts.net/some/path/"), "https://my-pc.tail1234.ts.net")
        self.assertEqual(normalize_origin("not a url"), "")


if __name__ == "__main__":
    unittest.main()
