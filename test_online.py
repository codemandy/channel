"""The online Channel: R2 signing, publish.py and api/index.py.

The login tests need `cryptography` (Vercel installs it from requirements.txt)
and are skipped without it.
"""

import base64
import datetime
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

import publish
import r2
import server
from arena_archive import ArenaClient, import_archive

ROOT = Path(__file__).parent

try:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
except ImportError:
    ec = None


def load_online():
    saved = dict(os.environ)
    spec = importlib.util.spec_from_file_location("channel_online", ROOT / "api" / "index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    os.environ.clear()
    os.environ.update(saved)
    return module


class SignatureTests(unittest.TestCase):
    """AWS's own Signature Version 4 examples for S3 (examplebucket, 2013-05-24)."""

    def store(self):
        store = r2.R2Store({"R2_ACCOUNT_ID": "x", "R2_BUCKET": "examplebucket", "R2_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
                            "R2_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "R2_ENDPOINT": "https://examplebucket.s3.amazonaws.com"}, prefix="")
        store.region = "us-east-1"
        store._path = lambda key: "/" + key  # the examples address the bucket by host name
        return store

    def test_presigned_url(self):
        url = self.store().presign("test.txt", expires=86400, now=datetime.datetime(2013, 5, 24, 15, 0, tzinfo=datetime.timezone.utc))
        self.assertTrue(url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"), url)

    def test_signed_get(self):
        request = self.store()._request("GET", "/test.txt", headers={"Range": "bytes=0-9"}, now=datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc))
        self.assertTrue(request.get_header("Authorization").endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"))

    def test_presign_is_stable_through_the_day(self):
        store = r2.R2Store({"R2_ACCOUNT_ID": "a", "R2_BUCKET": "b", "R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s"})
        morning = datetime.datetime(2026, 10, 8, 1, tzinfo=datetime.timezone.utc)
        self.assertEqual(store.presign("assets/x.jpg", now=morning), store.presign("assets/x.jpg", now=morning.replace(hour=23)))
        self.assertIn("/b/channel/assets/x.jpg?", store.presign("assets/x.jpg", now=morning))


class ArchiveTestCase(unittest.TestCase):
    """An archive with a favorite, a plain channel nested in it, and a file in each."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        import_archive("test", self.root / "archive.db", self.root / "assets", ArenaClient(fixture={"profile": {"data": [], "meta": {}}, "contents": {}}))
        self.saved = (server.DATABASE, server.ASSETS, server.THUMBS)
        server.DATABASE, server.ASSETS, server.THUMBS = self.root / "archive.db", self.root / "assets", self.root / "thumbs"
        connection = server.db()
        self.favorite = server.insert_channel(connection, "Shown", "Art")
        self.hidden = server.insert_channel(connection, "Private", "Diary")
        connection.execute("UPDATE channels SET favorite = 1 WHERE id = ?", (self.favorite,))
        self.text = server.insert_block(connection, self.favorite, "text", "Note", "hello")
        server.append_block(connection, self.favorite, server.channel_block(connection, self.hidden))
        self.secret = server.insert_block(connection, self.hidden, "text", "Secret", "nobody")
        self.picture = self.add_file(connection, self.favorite, "pic.png", "image/png", b"\x89PNG small")
        self.diary = self.add_file(connection, self.hidden, "diary.png", "image/png", b"\x89PNG diary")
        connection.commit()
        connection.close()
        self.store_dir = self.root / "store"

    def add_file(self, connection, channel_id, name, content_type, data):
        block_id = server.insert_block(connection, channel_id, "image", name)
        path = f"assets/channels/{channel_id}/{block_id}-{name}"
        (self.root / path).parent.mkdir(parents=True, exist_ok=True)
        (self.root / path).write_bytes(data)
        connection.execute("INSERT INTO assets VALUES (?, ?, '', ?, ?, 'stored')", (block_id, path, content_type, len(data)))
        return path

    def tearDown(self):
        server.DATABASE, server.ASSETS, server.THUMBS = self.saved
        self.directory.cleanup()

    def publish(self, **options):
        return publish.publish(self.root / "archive.db", self.root / "assets", r2.DirStore(self.store_dir),
                               self.root / "state.json", "test", log=lambda *_: None, **options)


class PublishTests(ArchiveTestCase):
    def test_only_favorites_leave_the_mac(self):
        result = self.publish()
        self.assertEqual((result["channels"], result["blocks"]), (1, 2))
        published = sqlite3.connect(self.store_dir / "archive.db")
        self.assertEqual(published.execute("SELECT title FROM channels").fetchall(), [("Shown",)])
        # The nested non-favorite channel goes, so no link leads nowhere.
        self.assertEqual(sorted(r[0] for r in published.execute("SELECT type FROM blocks")), ["image", "text"])
        self.assertEqual(published.execute("SELECT DISTINCT raw_json FROM blocks").fetchall(), [("{}",)])
        self.assertEqual(published.execute("SELECT key FROM thumbs").fetchall(), [(self.picture,)])
        self.assertEqual(published.execute("SELECT name FROM categories").fetchall(), [("Art",)])
        self.assertTrue((self.store_dir / self.picture).is_file())
        self.assertFalse((self.store_dir / self.diary).exists())

    def test_unchanged_archive_is_not_uploaded_again(self):
        self.publish()
        self.assertEqual(self.publish()["uploaded"], 0)

    def test_unstarring_removes_the_files_online(self):
        self.publish()
        connection = sqlite3.connect(self.root / "archive.db")
        connection.execute("UPDATE channels SET favorite = 0")
        connection.commit()
        connection.close()
        result = self.publish()
        self.assertEqual((result["channels"], result["removed"]), (0, 1))
        self.assertFalse((self.store_dir / self.picture).exists())
        self.assertEqual([key for key, _ in r2.DirStore(self.store_dir).keys()], ["archive.db"])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OnlineTests(ArchiveTestCase):
    def setUp(self):
        super().setUp()
        self.publish()
        self.online = load_online()
        self.online.STORE = r2.DirStore(self.store_dir)
        self.online.DATA = self.root / "data"
        self.online.DATABASE = self.online.DATA / "archive.db"
        self.online._state.update(etag=None, checked=float("-inf"))
        server.DATABASE = self.online.DATABASE
        self.flags = (server.READ_ONLY, server.ONLINE)
        server.READ_ONLY = server.ONLINE = True

        class Quiet(self.online.handler):
            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.opener = build_opener(NoRedirect)

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        server.READ_ONLY, server.ONLINE = self.flags
        os.environ.pop("AUTH_PUBLIC_JWK", None)
        super().tearDown()

    def get(self, path, cookie=None, method="GET"):
        request = Request(self.base + path, method=method, data=b"" if method == "POST" else None)
        if cookie:
            request.add_header("Cookie", f"icl_auth={cookie}")
        try:
            with self.opener.open(request) as response:
                return response.status, response.headers, response.read().decode()
        except HTTPError as error:
            return error.code, error.headers, error.read().decode()

    def test_shows_the_favorites_read_only(self):
        status, _, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Shown", page)
        self.assertNotIn("Private", page)
        self.assertNotIn("CREATE CHANNEL", page)
        self.assertIn("INNERCITY", page)
        self.assertEqual(self.get(f"/channel/{self.hidden}")[0], 404)
        self.assertNotIn("Secret", self.get("/search?q=e")[2])
        self.assertEqual(self.get("/toggle-favorite", method="POST")[0], 403)

    def test_files_redirect_only_when_published(self):
        status, headers, _ = self.get("/" + self.picture)
        self.assertEqual((status, headers["Location"]), (302, "/_store/" + self.picture))
        self.assertEqual(self.get("/thumbs/" + self.picture.removeprefix("assets/"))[0], 302)
        self.assertEqual(self.get("/" + self.diary)[0], 404)
        self.assertEqual(self.get("/assets/archive.db")[0], 404)

    def test_picks_up_a_new_publish(self):
        self.get("/")
        connection = sqlite3.connect(self.root / "archive.db")
        connection.execute("UPDATE channels SET favorite = 1")
        connection.commit()
        connection.close()
        time.sleep(0.01)
        self.publish()
        self.online._state["checked"] = float("-inf")
        self.assertIn("Private", self.get("/")[2])

    @unittest.skipIf(ec is None, "needs cryptography")
    def test_needs_the_hub_login(self):
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()
        os.environ["AUTH_PUBLIC_JWK"] = json.dumps({"kty": "EC", "crv": "P-256", "x": b64(numbers.x.to_bytes(32, "big")), "y": b64(numbers.y.to_bytes(32, "big"))})

        def token(ttl):
            header = b64(json.dumps({"alg": "ES256"}).encode())
            body = b64(json.dumps({"iss": "innercity-life", "aud": "innercity-life", "exp": int(time.time()) + ttl}).encode())
            r, s = decode_dss_signature(key.sign(f"{header}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
            return f"{header}.{body}.{b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

        status, headers, _ = self.get("/channel/1")
        self.assertEqual(status, 307)
        self.assertTrue(headers["Location"].startswith("https://www.innercity-life.com/login?next=https%3A%2F%2F"))
        self.assertEqual(self.get("/" + self.picture)[0], 307)
        self.assertEqual(self.get("/", cookie=token(-10))[0], 307)
        self.assertEqual(self.get("/?_icl=1", cookie="nonsense")[0], 401)
        self.assertEqual(self.get("/", cookie=token(60))[0], 200)


if __name__ == "__main__":
    unittest.main()
