"""Channel online: channel.innercity-life.com on Vercel.

The same server.py the Mac app runs, read-only, on the favorites-only copy of
the archive that publish.py puts in R2 under `channel/`:

- Every request needs the hub's login (the `icl_auth` cookie, checked with
  AUTH_PUBLIC_JWK). Without it you go to www.innercity-life.com/login.
- archive.db is downloaded to /tmp and checked for a newer one every
  CHECK_SECONDS, so a publish shows up within a minute.
- /assets/ and /thumbs/ redirect to signed R2 links for files the database
  lists, and nothing else.

Locally: `CHANNEL_STORE=<folder from publish.py --out> python3 api/index.py`
serves that folder at http://127.0.0.1:8770 without a login.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("CHANNEL_DATA", "/tmp/channel"))
os.environ["ARENA_DATABASE"] = str(DATA / "archive.db")
os.environ["ARENA_READONLY"] = "1"
os.environ["CHANNEL_ONLINE"] = "1"
sys.path.insert(0, str(ROOT))

import r2  # noqa: E402
import server  # noqa: E402

CHECK_SECONDS = 30
LOGIN = "https://www.innercity-life.com/login"
ON_VERCEL = bool(os.getenv("VERCEL"))
DATABASE = DATA / "archive.db"


def make_store():
    if os.getenv("CHANNEL_STORE"):
        return r2.DirStore(Path(os.environ["CHANNEL_STORE"]))
    if r2.configured(dict(os.environ)):
        return r2.R2Store(dict(os.environ))
    return None


STORE = make_store()
_lock = threading.Lock()
_state = {"etag": None, "checked": float("-inf")}


def refresh() -> None:
    """Fetches archive.db when it's missing or the published one has changed."""
    with _lock:
        if DATABASE.exists() and time.monotonic() - _state["checked"] < CHECK_SECONDS:
            return
        _state["checked"] = time.monotonic()
        try:
            etag = STORE.etag("archive.db")
            if etag and (etag != _state["etag"] or not DATABASE.exists()):
                DATA.mkdir(parents=True, exist_ok=True)
                _state["etag"] = STORE.download("archive.db", DATABASE)
        except OSError as error:  # keep serving the copy we have
            print(f"Could not check for a newer archive: {error}", file=sys.stderr)


def signed_in(cookie_header: str) -> bool:
    key = os.getenv("AUTH_PUBLIC_JWK")
    if not key:
        return not ON_VERCEL  # locally the site runs open, like the hub
    import session_token  # needs `cryptography`, which only Vercel installs

    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "icl_auth":
            return session_token.verify_session_token(value, key)
    return False


class handler(server.Handler):
    """Vercel calls this class for every path (vercel.json rewrites them all here)."""

    def do_GET(self) -> None:
        if not self.gate():
            return
        path = unquote(urlparse(self.path).path)
        if path.startswith(("/assets/", "/thumbs/", "/_store/")):
            self.send_file(path)
            return
        super().do_GET()

    def do_POST(self) -> None:
        if self.gate():
            super().do_POST()  # read-only: server.py answers 403

    def gate(self) -> bool:
        """Login first, then a database to show. False when it already answered."""
        if not os.getenv("AUTH_PUBLIC_JWK") and ON_VERCEL:
            self.plain(503, "AUTH_PUBLIC_JWK is not set on this project.")
            return False
        if not signed_in(self.headers.get("Cookie", "")):
            host = self.headers.get("Host", "channel.innercity-life.com")
            here = f"https://{host}{self.path}"
            # The hub adds _icl=1 when it sends back someone it considers signed
            # in. Rejecting them again means the keys don't match: say so
            # instead of bouncing between the two sites.
            if "_icl=" in self.path:
                self.plain(401, "Signed in on www.innercity-life.com, but this site does not accept the login.\n"
                                "AUTH_PUBLIC_JWK on the channel project must match the hub's key (run inner-city.life/scripts/set-auth-keys.sh).")
            else:
                self.send_response(307)
                self.send_header("Location", f"{LOGIN}?next={quote(here, safe='')}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            return False
        if STORE is None:
            self.plain(503, "R2 is not set up on this project (R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY).")
            return False
        refresh()
        if not DATABASE.exists():
            self.send_html(server.layout("Channel", "<section class='notice'><h1>Nothing published yet</h1><p>Star channels in the Mac app. It publishes them here after it saves.</p></section>"), 503)
            return False
        return True

    def send_file(self, path: str) -> None:
        if path.startswith("/_store/"):
            if not isinstance(STORE, r2.DirStore):
                self.send_error(404)
                return
            target = STORE._file(path.removeprefix("/_store/"))
            if not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        # Only keys the published database lists get signed, never archive.db.
        connection = server.db()
        if path.startswith("/thumbs/"):
            row = connection.execute("SELECT key FROM thumbs WHERE path = ?", (path,)).fetchone()
        else:
            row = connection.execute("SELECT path AS key FROM assets WHERE path = ?", (path.removeprefix("/"),)).fetchone()
        connection.close()
        if not row:
            self.send_error(404)
            return
        self.send_response(302)
        self.send_header("Location", STORE.presign(row["key"]))
        # Links are signed per day and valid for two, so an hour in the cache is safe.
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()

    def plain(self, status: int, text: str) -> None:
        data = text.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8770"))
    print(f"Channel online at http://127.0.0.1:{port} from {STORE.root if isinstance(STORE, r2.DirStore) else 'R2'}")
    ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
