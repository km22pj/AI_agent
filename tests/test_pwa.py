"""Installable app (PWA): manifest, icons and service worker, and entering the invite inside an installed app."""
import json
import urllib.error
import urllib.parse
import urllib.request

from tests.test_api import _OPENER, ALLOWED, INVITE, PUBLIC, Client, ServerTestCase


class PwaTests(ServerTestCase):
    extra_settings = {"invite_code": INVITE, "allowed_emails": [ALLOWED], "public_url": PUBLIC}

    def post_form(self, client: Client, code: str):
        req = urllib.request.Request(self.base + "/invite", data=urllib.parse.urlencode({"code": code}).encode(), method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with _OPENER.open(req, timeout=10) as resp:
                status, headers, body = resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            status, headers, body = exc.code, exc.headers, exc.read()
            exc.close()
        for set_cookie in headers.get_all("Set-Cookie") or []:
            name, _, value = set_cookie.split(";", 1)[0].partition("=")
            client.jar[name] = value
        return status, body.decode("utf-8")

    def test_manifest_icons_and_worker_load_without_invite(self):
        c = Client(self.base)  # no invite cookie, like the browser's manifest fetch
        status, raw = c.get("/manifest.webmanifest", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("manifest+json", c.last_headers["Content-Type"])
        manifest = json.loads(raw)
        self.assertEqual((manifest["display"], manifest["start_url"], manifest["scope"]), ("standalone", "/?source=app", "/"))
        sizes = {(i["sizes"], i.get("purpose", "any")) for i in manifest["icons"]}
        self.assertTrue({("192x192", "any"), ("512x512", "any"), ("512x512", "maskable")} <= sizes)
        for icon in manifest["icons"] + [{"src": "/icons/apple-touch-icon.png"}]:
            status, png = c.get(icon["src"], raw=True)
            self.assertEqual((status, png[:8]), (200, b"\x89PNG\r\n\x1a\n"), icon["src"])
        status, sw = c.get("/sw.js", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("javascript", c.last_headers["Content-Type"])
        self.assertIn(b'startsWith("/api/")) return', sw)  # personal data is never cached
        # The app itself is still behind the invite.
        self.assertEqual(c.get("/")[0], 403)
        self.assertEqual(c.get("/app.js")[0], 403)

    def test_invite_can_be_pasted_in_the_installed_app(self):
        c = Client(self.base)
        status, page = c.get("/?source=app")
        self.assertEqual(status, 403)
        self.assertIn('action="/invite"', page.decode("utf-8"))
        self.assertEqual(self.post_form(c, "wrong")[0], 403)
        self.assertEqual(c.get("/")[0], 403)
        status, _ = self.post_form(c, f"{PUBLIC}/?invite={INVITE}")  # the whole link works too
        self.assertEqual(status, 303)
        status, page = c.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b'rel="manifest"', page)
        self.assertIn(b"apple-touch-icon", page)
        other = Client(self.base)
        self.assertEqual(self.post_form(other, INVITE)[0], 303)  # just the code
        self.assertEqual(other.get("/app.js")[0], 200)

    def test_oversized_form_is_rejected(self):
        c = Client(self.base)
        self.assertEqual(self.post_form(c, "x" * 5000)[0], 403)
