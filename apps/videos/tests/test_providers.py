import base64
import hashlib
import hmac
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.test import SimpleTestCase, override_settings

from apps.videos.providers import (
    BunnyStreamProvider,
    DemoProvider,
    ProviderNotConfigured,
    ProviderPermanentError,
    ProviderTemporaryError,
    get_provider,
)

from .helpers import API_KEY, BUNNY, TOKEN_KEY, URLError, bunny_http, guid

NOW = 1_800_000_000
VIDEO_ID = "5b3f6c7e-0d1a-4b2c-9e8f-112233445566"


def provider():
    return BunnyStreamProvider(library_id="12345", api_key=API_KEY, cdn_hostname="https://vz-test.b-cdn.net/",
                               token_key=TOKEN_KEY, api_base="https://video.bunnycdn.com",
                               embed_base="https://player.mediadelivery.net/embed", timeout=10)


@mock.patch("apps.videos.providers.bunny.time.time", return_value=NOW)
class SigningTests(SimpleTestCase):
    def test_tus_upload_signature(self, _time):
        auth = provider().upload_authorization(VIDEO_ID, expires_in=3600, title="Clip", filetype="video/mp4")
        expires = NOW + 3600
        expected = hashlib.sha256(f"12345{API_KEY}{expires}{VIDEO_ID}".encode()).hexdigest()
        self.assertEqual(auth.endpoint, "https://video.bunnycdn.com/tusupload")
        self.assertEqual(auth.headers, {"AuthorizationSignature": expected, "AuthorizationExpire": str(expires),
                                        "VideoId": VIDEO_ID, "LibraryId": "12345"})
        self.assertEqual(auth.metadata, {"filetype": "video/mp4", "title": "Clip"})
        self.assertNotIn(API_KEY, repr(auth))

    def test_embed_token_uses_token_key_not_api_key(self, _time):
        playback = provider().playback(VIDEO_ID, expires_in=600)
        url = urlparse(playback.url)
        query = parse_qs(url.query)
        self.assertEqual(playback.kind, "iframe")
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}",
                         f"https://player.mediadelivery.net/embed/12345/{VIDEO_ID}")
        self.assertEqual(query["expires"], [str(NOW + 600)])
        self.assertEqual(query["token"], [hashlib.sha256(f"{TOKEN_KEY}{VIDEO_ID}{NOW + 600}".encode()).hexdigest()])
        self.assertEqual(playback.expires_at.timestamp(), NOW + 600)
        self.assertNotIn(API_KEY, playback.url)
        self.assertNotIn(TOKEN_KEY, playback.url)

    def test_cdn_advanced_token(self, _time):
        url = provider().thumbnail_url(VIDEO_ID, "thumbnail.jpg", expires_in=3600)
        path = f"/{VIDEO_ID}/thumbnail.jpg"
        digest = hmac.new(TOKEN_KEY.encode(), f"{path}{NOW + 3600}".encode(), hashlib.sha256).digest()
        token = "HS256-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")
        parsed = urlparse(url)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "vz-test.b-cdn.net", path))
        self.assertEqual(parse_qs(parsed.query), {"token": [token], "expires": [str(NOW + 3600)]})

    def test_unsafe_ids_and_filenames_are_refused(self, _time):
        p = provider()
        for bad in ("../etc", "", "demo-abc", VIDEO_ID + "/x"):
            with self.subTest(bad=bad), self.assertRaises(ProviderPermanentError):
                p.playback(bad, expires_in=60)
        self.assertEqual(p.thumbnail_url(VIDEO_ID, "../secret", expires_in=60), "")


class ConfigurationTests(SimpleTestCase):
    def test_missing_settings_and_repr(self):
        with self.assertRaises(ProviderNotConfigured) as caught:
            BunnyStreamProvider(library_id="", api_key="", cdn_hostname="x", token_key="y",
                                api_base="https://video.bunnycdn.com", embed_base="e", timeout=1)
        self.assertIn("BUNNY_STREAM_LIBRARY_ID", str(caught.exception))
        self.assertIn("BUNNY_STREAM_API_KEY", str(caught.exception))
        with self.assertRaises(ProviderNotConfigured):
            BunnyStreamProvider(library_id="abc", api_key="k", cdn_hostname="x", token_key="y",
                                api_base="b", embed_base="e", timeout=1)
        text = repr(provider()) + str(vars(provider()).get("library_id"))
        self.assertNotIn(API_KEY, repr(provider()))
        self.assertNotIn(TOKEN_KEY, text)

    @override_settings(DEBUG=False, VIDEO_DEMO_ALLOWED=False)
    def test_demo_refused_outside_debug(self):
        with self.assertRaises(ProviderNotConfigured):
            get_provider("DEMO")
        with self.assertRaises(ProviderNotConfigured):
            get_provider("OTHER")

    def test_demo_provider_never_uploads(self):
        demo = DemoProvider(sample_url="/static/demo/sample.mp4")
        with self.assertRaises(ProviderPermanentError):
            demo.upload_authorization("demo-1")
        self.assertEqual(demo.playback("demo-1", expires_in=60).kind, "video")
        self.assertEqual(DemoProvider().playback("demo-1", expires_in=60).kind, "placeholder")
        self.assertEqual(demo.get_video("demo-1").processing, "READY")


@BUNNY
class HttpTests(SimpleTestCase):
    def test_create_and_status_mapping(self):
        remote = guid()
        with bunny_http({"guid": remote}) as http:
            self.assertEqual(get_provider("BUNNY").create_video("A title"), remote)
        request = http.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://video.bunnycdn.com/library/12345/videos")
        self.assertEqual(request.get_header("Accesskey"), API_KEY)
        self.assertEqual(http.call_args.kwargs["timeout"], 10)
        cases = {0: "UPLOADING", 1: "PROCESSING", 3: "PROCESSING", 4: "READY", 5: "FAILED", 6: "FAILED",
                 7: "PROCESSING", 8: "READY", 99: "PROCESSING"}
        for code, expected in cases.items():
            with self.subTest(code=code), bunny_http({"status": code, "length": 61, "thumbnailFileName": "t.jpg"}):
                info = get_provider("BUNNY").get_video(VIDEO_ID)
                self.assertEqual(info.processing, expected)
                self.assertEqual((info.duration_seconds, info.thumbnail_file), (61, "t.jpg"))

    def test_error_classification(self):
        temporary = (429, 500, 503, 408, URLError("down"), socket.timeout(), TimeoutError())
        for item in temporary:
            with self.subTest(item=item), bunny_http(item), self.assertRaises(ProviderTemporaryError) as caught:
                get_provider("BUNNY").get_video(VIDEO_ID)
            self.assertNotIn(API_KEY, str(caught.exception))
        for code in (400, 401, 403, 404):
            with self.subTest(code=code), bunny_http(code), self.assertRaises(ProviderPermanentError):
                get_provider("BUNNY").get_video(VIDEO_ID)
        with bunny_http({"guid": "not-a-guid"}), self.assertRaises(ProviderTemporaryError):
            get_provider("BUNNY").create_video("x")

    def test_redirects_are_refused_and_never_receive_the_api_key(self):
        """Real local HTTP servers (no internet): Bunny's address redirects elsewhere."""
        seen = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status": 4}')

            def log_message(self, *args):
                pass

        target = HTTPServer(("127.0.0.1", 0), Target)

        class Redirect(Target):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/steal")
                self.end_headers()

        origin = HTTPServer(("127.0.0.1", 0), Redirect)
        for server in (target, origin):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        client = BunnyStreamProvider(library_id="12345", api_key=API_KEY, cdn_hostname="vz-test.b-cdn.net",
                                     token_key=TOKEN_KEY, api_base=f"http://127.0.0.1:{origin.server_port}",
                                     embed_base="https://player.mediadelivery.net/embed", timeout=5)
        with self.assertRaises(ProviderPermanentError) as caught:
            client.get_video(VIDEO_ID)
        self.assertIn("HTTP 302", str(caught.exception))
        self.assertNotIn(API_KEY, str(caught.exception))
        self.assertEqual(seen, [])  # The redirect target was never contacted.

    def test_delete_is_idempotent(self):
        with bunny_http(404):
            get_provider("BUNNY").delete_video(VIDEO_ID)
        with bunny_http(401), self.assertRaises(ProviderPermanentError):
            get_provider("BUNNY").delete_video(VIDEO_ID)
