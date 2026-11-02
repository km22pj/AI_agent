"""Checks the real Anthropic SDK call path against a local stand-in for the API.

No request leaves this machine: ANTHROPIC_BASE_URL points at a tiny local server
that records the request and returns canned Messages API responses. This proves
the request shape and our error mapping, not the behavior of the real model.
"""
from __future__ import annotations

import json
import os
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from backend.ai import AIError, AnthropicJSONClient, AIService

RECOGNITION = {"status": "recognized", "message": "보여요", "items": [{"name": "비빔밥", "estimated_amount": "1그릇", "amount_certain": False, "cooking_method": "비빔", "cooking_certain": True, "confidence": "high"}], "questions": []}


class StubAPI(BaseHTTPRequestHandler):
    reply = None  # (status, body dict)
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        StubAPI.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        status, payload = StubAPI.reply
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def message(text: str, stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}] if text else [],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


class AnthropicClientShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubAPI)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls._env = {k: os.environ.get(k) for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")}
        os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{cls.server.server_address[1]}"
        os.environ["ANTHROPIC_API_KEY"] = "test-key-not-real"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for k, v in cls._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        StubAPI.seen = []
        client = AnthropicJSONClient("claude-opus-5-5", "medium")
        client.client = client.client.with_options(max_retries=0)
        self.service = AIService(client)

    def test_request_shape_and_parsing(self):
        StubAPI.reply = (200, message(json.dumps(RECOGNITION, ensure_ascii=False)))
        result = self.service.recognize(b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")
        self.assertEqual(result["items"][0]["name"], "비빔밥")
        req = StubAPI.seen[0]
        self.assertTrue(req["path"].startswith("/v1/messages"))
        body = req["body"]
        self.assertEqual(body["model"], "claude-opus-5-5")
        self.assertEqual(body["fallbacks"], "default")
        self.assertIn("server-side-fallback-2026-07-01", req["headers"].get("anthropic-beta", ""))
        self.assertEqual(body["output_config"]["effort"], "medium")
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        image = body["messages"][0]["content"][0]
        self.assertEqual((image["type"], image["source"]["media_type"]), ("image", "image/jpeg"))
        self.assertNotIn("thinking", body)
        self.assertNotIn("temperature", body)

    def test_error_mapping(self):
        cases = [
            ((401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}), "ai_auth", False),
            ((404, {"type": "error", "error": {"type": "not_found_error", "message": "model"}}), "ai_model", False),
            ((429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}}), "ai_rate_limited", True),
            ((529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}), "ai_unavailable", True),
            ((200, message("", "refusal")), "ai_refusal", False),
            ((200, message('{"status": "recog', "max_tokens")), "ai_truncated", True),
            ((200, message("not json")), "ai_invalid", True),
        ]
        for reply, code, retryable in cases:
            with self.subTest(code):
                StubAPI.reply = reply
                with self.assertRaises(AIError) as ctx:
                    self.service.recognize(b"\xff\xd8\xff\xe0x", "image/jpeg")
                self.assertEqual((ctx.exception.code, ctx.exception.retryable), (code, retryable))
                self.assertNotIn("test-key", ctx.exception.message)


class GeminiStub(BaseHTTPRequestHandler):
    reply = None
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        GeminiStub.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        status, payload = GeminiStub.reply
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def interaction(text: str | None, status: str = "completed", errors=None) -> dict:
    out = {
        "id": "v1_test", "object": "interaction", "model": "gemini-3.8-flash", "status": status,
        "created": "2026-10-30T00:00:00Z", "updated": "2026-10-30T00:00:00Z",
        "steps": [{"type": "model_output", "content": [{"type": "text", "text": text}]}] if text is not None else [],
        "usage": {"total_input_tokens": 10, "total_output_tokens": 10, "total_tokens": 20},
    }
    if errors:
        out["errors"] = errors
    return out


def google_error(code: int, status: str, message: str) -> dict:
    return {"error": {"code": code, "message": message, "status": status}}


class GeminiClientShapeTests(unittest.TestCase):
    """Same idea as above for the Gemini Interactions API (google-genai SDK)."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), GeminiStub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        from backend.ai import GeminiJSONClient

        GeminiStub.seen = []
        self.service = AIService(GeminiJSONClient("gemini-3.8-flash", "test-key-not-real", base_url=self.base_url))

    def test_request_shape_and_parsing(self):
        GeminiStub.reply = (200, interaction(json.dumps(RECOGNITION, ensure_ascii=False)))
        result = self.service.recognize(b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg")
        self.assertEqual(result["items"][0]["name"], "비빔밥")
        req = GeminiStub.seen[0]
        self.assertIn("interactions", req["path"])
        self.assertEqual(req["headers"].get("x-goog-api-key"), "test-key-not-real")
        body = req["body"]
        self.assertEqual(body["model"], "gemini-3.8-flash")
        self.assertIn("identify the foods", body["system_instruction"])
        self.assertEqual(body["response_format"]["mime_type"], "application/json")
        self.assertEqual(body["response_format"]["schema"]["required"], ["status", "message", "items", "questions"])
        self.assertIs(body["store"], False)
        # The SDK wraps our parts in one user_input step.
        self.assertEqual(body["input"][0]["type"], "user_input")
        image, prompt = body["input"][0]["content"]
        self.assertEqual((image["type"], image["mime_type"]), ("image", "image/jpeg"))
        self.assertEqual(prompt["type"], "text")

    def test_error_mapping(self):
        cases = [
            ((400, google_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.")), "ai_auth", False),
            ((403, google_error(403, "PERMISSION_DENIED", "denied")), "ai_permission", False),
            ((404, google_error(404, "NOT_FOUND", "model not found")), "ai_model", False),
            ((429, google_error(429, "RESOURCE_EXHAUSTED", "quota")), "ai_rate_limited", True),
            ((503, google_error(503, "UNAVAILABLE", "overloaded")), "ai_unavailable", True),
            ((200, interaction('{"status": "recog', status="incomplete")), "ai_truncated", True),
            ((200, interaction(None, status="failed", errors=[{"code": "safety", "message": "blocked by safety"}])), "ai_refusal", False),
            ((200, interaction(None, status="failed", errors=[{"code": "internal", "message": "oops"}])), "ai_unavailable", True),
            ((200, interaction("not json")), "ai_invalid", True),
        ]
        for reply, code, retryable in cases:
            with self.subTest(code):
                GeminiStub.reply = reply
                with self.assertRaises(AIError) as ctx:
                    self.service.recognize(b"\xff\xd8\xff\xe0x", "image/jpeg")
                self.assertEqual((ctx.exception.code, ctx.exception.retryable), (code, retryable))
                self.assertNotIn("test-key", ctx.exception.message)

    def test_network_error(self):
        from backend.ai import GeminiJSONClient

        dead = AIService(GeminiJSONClient("gemini-3.8-flash", "k", base_url="http://127.0.0.1:9"))
        with self.assertRaises(AIError) as ctx:
            dead.recognize(b"\xff\xd8\xff\xe0x", "image/jpeg")
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("ai_network", True))


class ModelRoutingStub(BaseHTTPRequestHandler):
    """Answers 503 'high demand' for overloaded models and a normal result for others."""
    overloaded: set = set()
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ModelRoutingStub.seen.append(body)
        if body["model"] in ModelRoutingStub.overloaded:
            status, payload = 503, google_error(503, "UNAVAILABLE", f"{body['model']} is currently experiencing high demand")
        else:
            status, payload = 200, interaction(json.dumps(RECOGNITION, ensure_ascii=False))
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class GeminiFallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), ModelRoutingStub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def make(self, timeout=45.0):
        from backend.ai import GeminiJSONClient

        ModelRoutingStub.seen = []
        client = GeminiJSONClient("main-model", "k", base_url=self.base_url, fallback_models=["backup-model"], timeout=timeout)
        return client, AIService(client)

    def test_overloaded_model_falls_back_and_cools_down(self):
        ModelRoutingStub.overloaded = {"main-model"}
        client, service = self.make()
        result = service.recognize(b"\xff\xd8\xff\xe0x", "image/jpeg")
        self.assertEqual(result["model"], "backup-model")
        self.assertEqual([b["model"] for b in ModelRoutingStub.seen], ["main-model", "backup-model"])
        self.assertEqual(ModelRoutingStub.seen[0]["generation_config"], {"thinking_level": "minimal"})
        # The overloaded model is skipped for a while instead of being waited on again.
        ModelRoutingStub.seen = []
        service.recognize(b"\xff\xd8\xff\xe0x", "image/jpeg")
        self.assertEqual([b["model"] for b in ModelRoutingStub.seen], ["backup-model"])

    def test_all_models_overloaded_gives_clear_retryable_error(self):
        ModelRoutingStub.overloaded = {"main-model", "backup-model"}
        _, service = self.make()
        with self.assertRaises(AIError) as ctx:
            service.recognize(b"\xff\xd8\xff\xe0x", "image/jpeg")
        self.assertTrue(ctx.exception.retryable)
        self.assertIn("사용자가 많아", ctx.exception.message)

    def test_slow_model_times_out_quickly_and_falls_back(self):
        import time as _time

        class Slow(ModelRoutingStub):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if body["model"] == "main-model":
                    _time.sleep(15)
                    return
                self.rfile = __import__("io").BytesIO(json.dumps(body).encode())
                self.headers.replace_header("Content-Length", str(len(json.dumps(body).encode())))
                ModelRoutingStub.do_POST(self)

        slow = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
        threading.Thread(target=slow.serve_forever, daemon=True).start()
        try:
            from backend.ai import GeminiJSONClient

            ModelRoutingStub.overloaded = set()
            client = GeminiJSONClient("main-model", "k", base_url=f"http://127.0.0.1:{slow.server_address[1]}", fallback_models=["backup-model"], timeout=2)
            started = _time.monotonic()
            result = AIService(client).recognize(b"\xff\xd8\xff\xe0x", "image/jpeg")
            self.assertEqual(result["model"], "backup-model")
            self.assertLess(_time.monotonic() - started, 10)
        finally:
            slow.shutdown()
            slow.server_close()


class ProviderSelectionTests(unittest.TestCase):
    ENV_KEYS = ("HEALTHLINGO_AI_PROVIDER", "HEALTHLINGO_AI_MODEL", "GEMINI_API_KEY", "ANTHROPIC_API_KEY")

    def settings(self, **env):
        from unittest import mock

        from backend.config import Settings

        clean = {k: v for k, v in os.environ.items() if k not in self.ENV_KEYS}
        clean.update(env)
        # Never read the developer's real .env in tests.
        with mock.patch.dict(os.environ, clean, clear=True), mock.patch("backend.config.load_dotenv"):
            return Settings.from_env()

    def test_gemini_is_default(self):
        from backend.ai import GeminiJSONClient, build_ai_service

        s = self.settings(GEMINI_API_KEY="test-key-not-real")
        self.assertEqual((s.ai_provider, s.ai_model, s.ai_key_name), ("gemini", "gemini-3.6-flash", "GEMINI_API_KEY"))
        self.assertEqual((s.ai_fallback_models, s.ai_timeout), (["gemini-3.5-flash-lite"], 45.0))
        with unittest.mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-key-not-real"}):
            service = build_ai_service(s)
        self.assertIsInstance(service.client, GeminiJSONClient)

    def test_anthropic_key_alone_does_not_enable_gemini(self):
        from backend.ai import AINotConfigured, build_ai_service

        service = build_ai_service(self.settings(ANTHROPIC_API_KEY="x"))
        self.assertFalse(service.configured)
        with self.assertRaises(AINotConfigured) as ctx:
            service.recognize(b"x", "image/jpeg")
        self.assertIn("GEMINI_API_KEY", ctx.exception.message)

    def test_dotenv_wins_over_stale_terminal_copy(self):
        """VS Code copies .env into terminals when they open; the file must still win afterwards."""
        import tempfile
        from pathlib import Path
        from unittest import mock

        from backend import config

        root = Path(tempfile.mkdtemp())
        (root / ".env").write_text(
            "GEMINI_API_KEY=new-key\n# HEALTHLINGO_ALLOWED_EMAILS=old@x.com\nHEALTHLINGO_INVITE_CODE=new-code-123456789\n",
            encoding="utf-8",
        )
        stale = {"GEMINI_API_KEY": "old-key", "HEALTHLINGO_ALLOWED_EMAILS": "old@x.com", "HEALTHLINGO_INVITE_CODE": "old-code"}
        clean = {k: v for k, v in os.environ.items() if k not in self.ENV_KEYS and not k.startswith("HEALTHLINGO_")}
        with mock.patch.dict(os.environ, {**clean, **stale}, clear=True), mock.patch.object(config, "PROJECT_ROOT", root):
            s = config.Settings.from_env()
            self.assertEqual(os.environ["GEMINI_API_KEY"], "new-key")
        self.assertEqual(s.allowed_emails, [])  # commented out in .env -> really off
        self.assertEqual(s.invite_code, "new-code-123456789")
        self.assertEqual(s.env_file, root / ".env")
        self.assertEqual(s.env_notes, ["GEMINI_API_KEY", "HEALTHLINGO_ALLOWED_EMAILS", "HEALTHLINGO_INVITE_CODE"])

    def test_claude_still_selectable(self):
        s = self.settings(HEALTHLINGO_AI_PROVIDER="anthropic", ANTHROPIC_API_KEY="x")
        self.assertEqual((s.ai_provider, s.ai_model, s.ai_key_name), ("anthropic", "claude-opus-5-5", "ANTHROPIC_API_KEY"))


if __name__ == "__main__":
    unittest.main()
