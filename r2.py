"""The shared Cloudflare R2 bucket over its S3 API, with the standard library only.

Used on the Mac by publish.py (upload) and on Vercel by api/index.py (download
the database, sign links to files). Channel keeps everything under `channel/`
in the innercity-life bucket, next to the other apps' prefixes.

    R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    R2_ENDPOINT (optional, overrides https://<account>.r2.cloudflarestorage.com)
    R2_PREFIX   (optional, default "channel/")

`DirStore` does the same against a folder, for tests and local runs.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import shutil
import urllib.error
import urllib.request
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Iterator
from urllib.parse import quote, urlsplit

PREFIX = "channel/"
UNSIGNED = "UNSIGNED-PAYLOAD"
EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=value lines, as written by scripts/set-r2-keys.sh. Missing file: nothing."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def configured(env: dict[str, str]) -> bool:
    return all(env.get(name) for name in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"))


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _encode(value: str, safe: str = "-_.~") -> str:
    return quote(value, safe=safe)


class R2Store:
    def __init__(self, env: dict[str, str], prefix: str | None = None) -> None:
        self.bucket = env["R2_BUCKET"]
        self.access_key = env["R2_ACCESS_KEY_ID"]
        self.secret_key = env["R2_SECRET_ACCESS_KEY"]
        self.endpoint = (env.get("R2_ENDPOINT") or f"https://{env['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com").rstrip("/")
        self.host = urlsplit(self.endpoint).netloc
        self.prefix = prefix if prefix is not None else env.get("R2_PREFIX", PREFIX)
        self.region = "auto"

    # Signature Version 4, path-style (https://<host>/<bucket>/<key>).

    def _path(self, key: str) -> str:
        return "/" + _encode(self.bucket) + ("/" + _encode(self.prefix + key, safe="-_.~/") if key is not None else "")

    def _scope(self, date: str) -> str:
        return f"{date}/{self.region}/s3/aws4_request"

    def _signature(self, stamp: str, canonical_request: str) -> str:
        date = stamp[:8]
        to_sign = "\n".join(("AWS4-HMAC-SHA256", stamp, self._scope(date), hashlib.sha256(canonical_request.encode()).hexdigest()))
        key = _sign(_sign(_sign(_sign(("AWS4" + self.secret_key).encode(), date), self.region), "s3"), "aws4_request")
        return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _query(params: dict[str, str]) -> str:
        return "&".join(f"{_encode(k)}={_encode(v)}" for k, v in sorted(params.items()))

    def _request(self, method: str, path: str, params: dict[str, str] | None = None, body=None, payload_hash: str = EMPTY_HASH,
                 headers: dict[str, str] | None = None, now: datetime.datetime | None = None) -> urllib.request.Request:
        stamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        query = self._query(params or {})
        signed = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp}
        signed.update({k.lower(): v for k, v in (headers or {}).items()})
        names = sorted(signed)
        canonical = "\n".join((method, path, query, "".join(f"{n}:{signed[n].strip()}\n" for n in names), ";".join(names), payload_hash))
        authorization = (f"AWS4-HMAC-SHA256 Credential={self.access_key}/{self._scope(stamp[:8])}, "
                         f"SignedHeaders={';'.join(names)}, Signature={self._signature(stamp, canonical)}")
        request = urllib.request.Request(self.endpoint + path + ("?" + query if query else ""), data=body, method=method)
        for name, value in signed.items():
            if name != "host":
                request.add_header(name, value)
        request.add_header("Authorization", authorization)
        return request

    def presign(self, key: str, expires: int = 2 * 86400, now: datetime.datetime | None = None) -> str:
        """A GET link that works without credentials until it expires.

        Signed as of midnight UTC, so the link stays the same all day and the
        browser can cache what it points to; `expires` must cover a full day.
        """
        moment = now or datetime.datetime.now(datetime.timezone.utc)
        stamp = moment.strftime("%Y%m%dT000000Z")
        path = self._path(key)
        params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{self._scope(stamp[:8])}",
            "X-Amz-Date": stamp,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        canonical = "\n".join(("GET", path, self._query(params), f"host:{self.host}\n", "host", UNSIGNED))
        return f"{self.endpoint}{path}?{self._query(params)}&X-Amz-Signature={self._signature(stamp, canonical)}"

    # Operations

    def etag(self, key: str) -> str | None:
        try:
            with urllib.request.urlopen(self._request("HEAD", self._path(key)), timeout=20) as response:
                return response.headers.get("ETag", "")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def download(self, key: str, target: Path) -> str | None:
        """Writes the object to `target` (atomically) and returns its etag; None when missing."""
        scratch = target.with_name(f".{target.name}.{os.getpid()}")
        try:
            with urllib.request.urlopen(self._request("GET", self._path(key)), timeout=60) as response, scratch.open("wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
                etag = response.headers.get("ETag", "")
        except urllib.error.HTTPError as error:
            scratch.unlink(missing_ok=True)
            if error.code == 404:
                return None
            raise
        scratch.replace(target)
        return etag

    def upload(self, key: str, source: Path, content_type: str, cache_control: str = "") -> None:
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        headers = {"content-type": content_type, "content-length": str(source.stat().st_size)}
        if cache_control:
            headers["cache-control"] = cache_control
        with source.open("rb") as body:
            with urllib.request.urlopen(self._request("PUT", self._path(key), body=body, payload_hash=digest.hexdigest(), headers=headers), timeout=600):
                pass

    def delete(self, key: str) -> None:
        try:
            with urllib.request.urlopen(self._request("DELETE", self._path(key)), timeout=20):
                pass
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise

    def keys(self) -> Iterator[tuple[str, int]]:
        """Every (key, size) under the prefix, with the prefix taken off."""
        token = ""
        while True:
            params = {"list-type": "2", "prefix": self.prefix}
            if token:
                params["continuation-token"] = token
            with urllib.request.urlopen(self._request("GET", self._path(None), params=params), timeout=60) as response:
                tree = ElementTree.fromstring(response.read())
            ns = {"s3": tree.tag.split("}")[0].strip("{")} if tree.tag.startswith("{") else {}
            find = (lambda node, name: node.find(f"s3:{name}", ns)) if ns else (lambda node, name: node.find(name))
            for item in tree.findall("s3:Contents", ns) if ns else tree.findall("Contents"):
                yield find(item, "Key").text[len(self.prefix):], int(find(item, "Size").text)
            truncated = find(tree, "IsTruncated")
            next_token = find(tree, "NextContinuationToken")
            if truncated is None or truncated.text != "true" or next_token is None:
                return
            token = next_token.text


class DirStore:
    """A folder standing in for the bucket (tests, `publish.py --out`, local runs)."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _file(self, key: str) -> Path:
        target = (self.root / key).resolve()
        if self.root.resolve() not in target.parents:
            raise ValueError("key outside the store")
        return target

    def etag(self, key: str) -> str | None:
        target = self._file(key)
        if not target.is_file():
            return None
        stat = target.stat()
        return f'"{stat.st_mtime_ns}-{stat.st_size}"'

    def download(self, key: str, target: Path) -> str | None:
        etag = self.etag(key)
        if etag is None:
            return None
        scratch = target.with_name(f".{target.name}.{os.getpid()}")
        shutil.copyfile(self._file(key), scratch)
        scratch.replace(target)
        return etag

    def upload(self, key: str, source: Path, content_type: str, cache_control: str = "") -> None:
        target = self._file(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        scratch = target.with_name(f".{target.name}.{os.getpid()}")
        shutil.copyfile(source, scratch)
        scratch.replace(target)

    def delete(self, key: str) -> None:
        self._file(key).unlink(missing_ok=True)

    def keys(self) -> Iterator[tuple[str, int]]:
        if not self.root.exists():
            return
        for path in self.root.rglob("*"):
            if path.is_file() and not path.name.startswith("."):
                yield path.relative_to(self.root).as_posix(), path.stat().st_size

    def presign(self, key: str, expires: int = 0, now=None) -> str:
        return "/_store/" + quote(key)
