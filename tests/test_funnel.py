"""Funnel auto-registration: runs reset then --bg <port>, waits for Tailscale, never crashes the server."""
import json
import subprocess
import unittest
from unittest import mock

from backend import funnel
from backend.config import Settings


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def status(state="Running", dns="node.tail1234.ts.net."):
    return done(json.dumps({"BackendState": state, "Self": {"DNSName": dns}}))


class FunnelTests(unittest.TestCase):
    def settings(self, **kw):
        base = {"public_url": "https://node.tail1234.ts.net", "port": 8000}
        base.update(kw)
        return Settings(**base)

    def run_refresh(self, responses, settings=None):
        calls = []

        def fake_run(exe, *args, timeout=30):
            calls.append(args)
            return responses.pop(0)

        with mock.patch.object(funnel, "find_tailscale", return_value="tailscale"), \
                mock.patch.object(funnel, "_run", side_effect=fake_run):
            ok = funnel.refresh_funnel(settings or self.settings(), wait_seconds=0.05, poll=0.01)
        return ok, calls

    def test_wanted_only_for_ts_net_and_when_enabled(self):
        self.assertTrue(funnel.wanted(self.settings()))
        self.assertFalse(funnel.wanted(self.settings(auto_funnel=False)))
        self.assertFalse(funnel.wanted(self.settings(public_url="https://example.com")))
        self.assertFalse(funnel.wanted(self.settings(public_url="")))

    def test_resets_then_reenables_on_configured_port(self):
        ok, calls = self.run_refresh([status(), done(), done()], self.settings(port=8123))
        self.assertTrue(ok)
        self.assertEqual(calls, [("status", "--json"), ("funnel", "reset"), ("funnel", "--bg", "8123")])

    def test_waits_while_tailscale_is_starting(self):
        ok, calls = self.run_refresh([status("Starting"), status("NoState"), status(), done(), done()])
        self.assertTrue(ok)
        self.assertEqual(calls.count(("status", "--json")), 3)

    def test_gives_up_without_touching_funnel_if_tailscale_never_connects(self):
        with self.assertLogs("healthlingo.funnel", "WARNING"):
            ok, calls = self.run_refresh([status("Stopped")] * 50)
        self.assertFalse(ok)
        self.assertNotIn(("funnel", "reset"), calls)

    def test_failed_reset_stops_and_logs(self):
        with self.assertLogs("healthlingo.funnel", "WARNING") as logs:
            ok, calls = self.run_refresh([status(), done(returncode=1, stderr="access denied")])
        self.assertFalse(ok)
        self.assertEqual(calls[-1], ("funnel", "reset"))
        self.assertIn("access denied", "\n".join(logs.output))

    def test_warns_when_machine_name_differs_from_public_url(self):
        with self.assertLogs("healthlingo.funnel", "WARNING") as logs:
            ok, _ = self.run_refresh([status(dns="other.tail1234.ts.net."), done(), done()])
        self.assertTrue(ok)
        self.assertIn("other.tail1234.ts.net", "\n".join(logs.output))

    def test_missing_tailscale_is_skipped(self):
        with mock.patch.object(funnel, "find_tailscale", return_value=None), \
                self.assertLogs("healthlingo.funnel", "WARNING"):
            self.assertFalse(funnel.refresh_funnel(self.settings()))

    def test_env_flag_default_on_and_can_be_turned_off(self):
        from backend.config import _flag
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("HEALTHLINGO_AUTO_FUNNEL", None)
            self.assertTrue(_flag("HEALTHLINGO_AUTO_FUNNEL", True))
            os.environ["HEALTHLINGO_AUTO_FUNNEL"] = "0"
            self.assertFalse(_flag("HEALTHLINGO_AUTO_FUNNEL", True))


if __name__ == "__main__":
    unittest.main()
