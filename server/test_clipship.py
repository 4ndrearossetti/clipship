"""End-to-end tests for the receiver and the web UI.

Run with: python -m unittest test_clipship.py
Requires the same dependencies as the server, plus pypdf for the PDF test
(skipped gracefully if missing).

Each test writes into a per-test temporary OUTPUT_DIR so they don't collide
with a real inbox. The config module is monkey-patched in setUp.
"""
from __future__ import annotations

import base64
import hmac
import hashlib
import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

THIS_DIR = Path(__file__).parent
sys.path.insert(0, str(THIS_DIR))


def _stub_config(tmpdir: str, **extra):
    """Create a synthetic config module with safe defaults for tests."""
    mod = type(sys)("config")
    mod.SECRET_KEY = "testsecret"
    mod.OUTPUT_DIR = tmpdir
    mod.HOST = "127.0.0.1"
    mod.PORT = 5050
    mod.MAX_CLOCK_SKEW = 300
    mod.MAX_BODY_BYTES = 10 * 1024 * 1024
    mod.DOWNLOAD_ASSETS = True
    mod.ASSETS_SUBDIR = "assets"
    mod.MAX_ASSET_BYTES = 25 * 1024 * 1024
    mod.MAX_ASSETS_PER_CLIP = 100
    mod.ASSET_TIMEOUT = 10
    mod.ASSET_USER_AGENT = "Clipship-test"
    mod.DOWNLOAD_PDFS = True
    mod.MAX_PDF_BYTES = 100 * 1024 * 1024
    mod.PDF_TIMEOUT = 30
    mod.EXTRACT_PDF_TEXT = False  # don't depend on pypdf for most tests
    mod.WEB_UI_ENABLED = True
    mod.WEB_UI_USERNAME = "admin"
    mod.WEB_UI_PASSWORD = "testpw"
    mod.WEB_UI_HOST = "127.0.0.1"
    mod.WEB_UI_PORT = 5051
    mod.WEB_UI_PAGE_SIZE = 30
    for k, v in extra.items():
        setattr(mod, k, v)
    sys.modules["config"] = mod
    return mod


def _signed_post(client, body: bytes, secret: bytes = b"testsecret"):
    ts = str(int(time.time()))
    sig = hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return client.post(
        "/clip",
        data=body,
        content_type="application/json",
        headers={
            "X-Clipship-Timestamp": ts,
            "X-Clipship-Signature": sig,
        },
    )


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clipship-test-")
        _stub_config(self.tmp)
        for m in list(sys.modules):
            if m == "receiver":
                del sys.modules[m]
        self.receiver = importlib.import_module("receiver")
        self.client = self.receiver.app.test_client()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json, {"status": "ok"})

    def test_plain_clip(self):
        body = json.dumps({
            "filename": "2026-05-31-test.md",
            "content": '---\ntitle: "Hi"\n---\n\nHello.\n',
        }).encode()
        r = _signed_post(self.client, body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["file"], "2026-05-31-test.md")
        self.assertTrue((Path(self.tmp) / "2026-05-31-test.md").exists())

    def test_bad_signature(self):
        body = json.dumps({"filename": "x.md", "content": "x"}).encode()
        ts = str(int(time.time()))
        r = self.client.post("/clip", data=body, content_type="application/json",
            headers={"X-Clipship-Timestamp": ts, "X-Clipship-Signature": "deadbeef"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json["error"], "invalid signature")

    def test_stale_timestamp(self):
        body = json.dumps({"filename": "x.md", "content": "x"}).encode()
        ts = str(int(time.time()) - 9999)
        sig = hmac.new(b"testsecret", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        r = self.client.post("/clip", data=body, content_type="application/json",
            headers={"X-Clipship-Timestamp": ts, "X-Clipship-Signature": sig})
        self.assertEqual(r.status_code, 403)

    def test_path_traversal_sanitized(self):
        body = json.dumps({"filename": "../../etc/evil.md", "content": "x"}).encode()
        r = _signed_post(self.client, body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["file"], "evil.md")
        self.assertTrue((Path(self.tmp) / "evil.md").exists())
        self.assertFalse(Path("/etc/evil.md").exists())

    def test_collision_autosuffix(self):
        body = json.dumps({"filename": "dup.md", "content": "first"}).encode()
        r1 = _signed_post(self.client, body)
        body2 = json.dumps({"filename": "dup.md", "content": "second"}).encode()
        r2 = _signed_post(self.client, body2)
        self.assertEqual(r1.json["file"], "dup.md")
        self.assertEqual(r2.json["file"], "dup-1.md")

    def test_pdf_payload_rejects_non_pdf_content(self):
        # Mock the URL fetcher to return obviously-not-a-PDF bytes.
        with mock.patch.object(
            self.receiver, "_fetch_url_verbose",
            return_value=(b"<html>not a pdf</html>", "text/html", ""),
        ):
            body = json.dumps({
                "filename": "not-a-pdf.md",
                "pdf_url": "https://example.com/foo.pdf",
                "title": "Not a PDF",
            }).encode()
            r = _signed_post(self.client, body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("non-PDF", r.json["error"])

    def test_pdf_payload_accepts_pdf_magic_number(self):
        # Server lies about Content-Type but the body has the PDF magic
        # number — accept it.
        fake_pdf = b"%PDF-1.4\n" + b"\x00" * 200 + b"%%EOF"
        with mock.patch.object(
            self.receiver, "_fetch_url_verbose",
            return_value=(fake_pdf, "application/octet-stream", ""),
        ):
            body = json.dumps({
                "filename": "magic.md",
                "pdf_url": "https://example.com/foo",
                "title": "PDF",
            }).encode()
            r = _signed_post(self.client, body)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json.get("pdf"))

    def test_pdf_payload_fetch_failure_surfaces_reason(self):
        with mock.patch.object(
            self.receiver, "_fetch_url_verbose",
            return_value=(None, "", "upstream HTTP 404"),
        ):
            body = json.dumps({
                "filename": "missing.md",
                "pdf_url": "https://example.com/missing.pdf",
            }).encode()
            r = _signed_post(self.client, body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("HTTP 404", r.json["error"])

    def test_ssrf_url_validation(self):
        # Direct IP literal blocked
        self.assertFalse(self.receiver._url_is_safe("http://127.0.0.1/foo"))
        self.assertFalse(self.receiver._url_is_safe("http://10.0.0.1/foo"))
        self.assertFalse(self.receiver._url_is_safe("http://169.254.169.254/foo"))
        self.assertFalse(self.receiver._url_is_safe("http://[::1]/foo"))
        # Wrong scheme blocked
        self.assertFalse(self.receiver._url_is_safe("file:///etc/passwd"))
        self.assertFalse(self.receiver._url_is_safe("ftp://example.com/x"))
        # No host blocked
        self.assertFalse(self.receiver._url_is_safe("http://"))

    def test_asset_localization_with_mocked_fetch(self):
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c63600100000005000128b50c5b0000000049454e44ae426082"
        )
        with mock.patch.object(self.receiver, "_fetch_asset", return_value=(png, "png")):
            body = json.dumps({
                "filename": "img.md",
                "content": "---\ntitle: x\n---\n\n![a](https://example.com/a.png)\n",
            }).encode()
            r = _signed_post(self.client, body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json["assets_downloaded"], 1)
        md = (Path(self.tmp) / "img.md").read_text()
        self.assertIn("assets/img-img1.png", md)
        self.assertTrue((Path(self.tmp) / "assets" / "img-img1.png").exists())


class WebUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="clipship-web-test-")
        _stub_config(self.tmp)
        for m in list(sys.modules):
            if m in ("receiver", "web"):
                del sys.modules[m]
        self.web = importlib.import_module("web")
        self.client = self.web.app.test_client()

        # Seed the inbox with one plain clip and one PDF stub.
        (Path(self.tmp) / "2026-05-31-plain.md").write_text(
            '---\ntitle: "Plain"\nsource: "https://example.com"\nclipped: "2026-05-31T00:00:00Z"\ntags: ["a", "b"]\n---\n\nBody.\n'
        )
        (Path(self.tmp) / "2026-05-31-pdf.md").write_text(
            '---\ntitle: "PDF"\nsource: "https://example.com/foo.pdf"\nclipped: "2026-05-31T00:00:00Z"\ntype: "pdf"\npdf: "assets/2026-05-31-pdf.pdf"\n---\n\n[Open PDF](assets/2026-05-31-pdf.pdf)\n'
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_auth_required(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 401)
        self.assertIn("WWW-Authenticate", r.headers)

    def test_auth_wrong(self):
        r = self.client.get("/", headers={"Authorization": "Basic YWRtaW46d3Jvbmc="})
        self.assertEqual(r.status_code, 401)

    def _auth(self):
        # admin:testpw -> base64
        return {"Authorization": "Basic " + base64.b64encode(b"admin:testpw").decode()}

    def test_index_lists_clips(self):
        r = self.client.get("/", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Plain", r.data)
        self.assertIn(b"PDF", r.data)

    def test_clip_view_plain(self):
        r = self.client.get("/clip/2026-05-31-plain.md", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Body.", r.data)

    def test_pdf_badge_present(self):
        r = self.client.get("/", headers=self._auth())
        self.assertIn(b'class="badge">pdf<', r.data)

    def test_tag_filter(self):
        r = self.client.get("/tag/a", headers=self._auth())
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"2026-05-31-plain.md", r.data)
        self.assertNotIn(b"2026-05-31-pdf.md", r.data)

    def test_search_by_title(self):
        r = self.client.get("/?q=plain", headers=self._auth())
        self.assertIn(b"2026-05-31-plain.md", r.data)
        self.assertNotIn(b"2026-05-31-pdf.md", r.data)

    def test_proxy_fix_honours_prefix(self):
        r = self.client.get(
            "/", headers={**self._auth(), "X-Forwarded-Prefix": "/ui"}
        )
        self.assertEqual(r.status_code, 200)
        # url_for should now generate /ui/-prefixed links
        self.assertIn(b'href="/ui/clip/', r.data)

    def test_clip_404_traversal(self):
        r = self.client.get("/clip/../../etc/passwd", headers=self._auth())
        self.assertEqual(r.status_code, 404)

    def test_asset_404_traversal(self):
        r = self.client.get("/assets/../../etc/passwd", headers=self._auth())
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
