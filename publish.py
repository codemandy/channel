#!/usr/bin/env python3
"""Publish the favorite channels to channel.innercity-life.com.

Copies the channels you starred, their blocks and their files into the shared
R2 bucket under `channel/`. The online Channel (api/index.py) reads only that,
so nothing else in the archive leaves the Mac. Unstarring a channel removes
its files online on the next publish.

    python3 publish.py                    # from the iCloud archive (or ./archive.db)
    python3 publish.py --out some/folder  # write to a folder instead of R2
    python3 publish.py --force            # check every file even if nothing changed

The Mac app runs this after it writes changes back to iCloud. R2 keys come
from `.env.local` (scripts/set-r2-keys.sh writes it) or the environment.
Thumbnails are drawn with macOS `sips` and Quick Look, like the app does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICLOUD = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs/CHANNEL"
SUPPORT = Path.home() / "Library/Application Support/ArenaArchive"
IMMUTABLE = "public, max-age=31536000, immutable"
NOT_CONFIGURED = 2

# The tables the online server reads, copied with the source's own schema.
TABLES = ("channels", "blocks", "channel_blocks", "assets", "categories")


def default_database() -> Path:
    return ICLOUD / "archive.db" if (ICLOUD / "archive.db").exists() else ROOT / "archive.db"


def snapshot(source: Path, target: Path) -> None:
    """A consistent copy, even while the app is writing to the source."""
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    writer = sqlite3.connect(target)
    reader.backup(writer)
    writer.close()
    reader.close()


def build(source: Path, target: Path) -> dict[str, int]:
    """Writes the favorites-only database to `target` and returns counts.

    Raw Are.na JSON is left out. A nested channel block stays only when the
    channel it opens is a favorite too, so every link online leads somewhere.
    """
    target.unlink(missing_ok=True)
    out = sqlite3.connect(target)
    out.execute("ATTACH DATABASE ? AS src", (str(source),))
    for (sql,) in out.execute("SELECT sql FROM src.sqlite_master WHERE type = 'table' AND name IN (%s)" % ",".join("?" * len(TABLES)), TABLES).fetchall():
        out.execute(sql)
    for (sql,) in out.execute("SELECT sql FROM src.sqlite_master WHERE type = 'index' AND sql IS NOT NULL AND tbl_name IN (%s)" % ",".join("?" * len(TABLES)), TABLES).fetchall():
        out.execute(sql)
    out.execute("CREATE TEMP TABLE fav AS SELECT id FROM src.channels WHERE favorite = 1")
    out.execute("INSERT INTO channels SELECT * FROM src.channels WHERE id IN (SELECT id FROM fav)")
    out.execute("""INSERT INTO channel_blocks SELECT cb.* FROM src.channel_blocks cb JOIN src.blocks b ON b.id = cb.block_id
                   WHERE cb.channel_id IN (SELECT id FROM fav)
                   AND (b.type != 'channel' OR b.source_url IN (SELECT '/channel/' || id FROM fav))""")
    out.execute("INSERT INTO blocks SELECT * FROM src.blocks WHERE id IN (SELECT block_id FROM channel_blocks)")
    out.execute("INSERT INTO assets SELECT * FROM src.assets WHERE block_id IN (SELECT id FROM blocks)")
    out.execute("INSERT INTO categories SELECT DISTINCT category FROM channels WHERE category != ''")
    for table in ("channels", "blocks", "channel_blocks"):
        out.execute(f"UPDATE {table} SET raw_json = '{{}}'")
    # /thumbs/<path> → the stored key it shows. Filled in by publish().
    out.execute("CREATE TABLE thumbs (path TEXT PRIMARY KEY, key TEXT NOT NULL, content_type TEXT NOT NULL)")
    counts = {
        "channels": out.execute("SELECT COUNT(*) FROM channels").fetchone()[0],
        "blocks": out.execute("SELECT COUNT(*) FROM blocks").fetchone()[0],
    }
    out.commit()
    out.execute("DETACH DATABASE src")
    out.close()
    return counts


def thumb_for(server, source: Path, content_type: str) -> tuple[Path, str] | None:
    """The picture /thumbs/ shows for an asset, as server.py draws it locally."""
    if content_type.startswith("image/"):
        thumb = server.thumbnail(source, content_type)
        if thumb == source:
            return source, content_type
        return thumb, "image/png" if thumb.suffix == ".png" else "image/jpeg"
    drawn = server.preview(source)
    return (drawn, "image/png") if drawn else None


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish(database: Path, assets: Path, store, state_file: Path | None, store_id: str, force: bool = False, log=print) -> dict[str, int]:
    import server  # after ARENA_THUMBS is set, so it draws into the right cache

    with tempfile.TemporaryDirectory(prefix="channel-publish-") as scratch_dir:
        scratch = Path(scratch_dir)
        snapshot(database, scratch / "source.db")
        published = scratch / "archive.db"
        counts = build(scratch / "source.db", published)

        connection = sqlite3.connect(published)
        rows = connection.execute("SELECT path, content_type FROM assets ORDER BY path").fetchall()
        wanted: dict[str, tuple[Path, str]] = {}
        thumbs: list[tuple[str, str, str]] = []
        missing = 0
        for path, content_type in rows:
            relative = path.removeprefix("assets/")
            local = assets / relative
            if not local.is_file():
                missing += 1
                continue
            content_type = content_type or "application/octet-stream"
            wanted[path] = (local, content_type)
            thumb = thumb_for(server, local, content_type)
            if thumb is None:
                continue
            thumb_path, thumb_type = thumb
            key = path if thumb_path == local else "thumbs/" + thumb_path.name
            wanted.setdefault(key, (thumb_path, thumb_type))
            thumbs.append(("/thumbs/" + relative, key, thumb_type))
        connection.executemany("INSERT OR REPLACE INTO thumbs VALUES (?, ?, ?)", thumbs)
        connection.commit()
        connection.execute("VACUUM")
        connection.close()

        digest = file_hash(published)
        state = json.loads(state_file.read_text()) if state_file and state_file.is_file() else {}
        if not force and state.get(store_id) == digest:
            log(f"Nothing changed since the last publish ({counts['channels']} channels).")
            return {**counts, "uploaded": 0, "removed": 0, "missing": missing}

        existing = dict(store.keys())
        uploaded = 0
        for key, (local, content_type) in sorted(wanted.items()):
            if existing.get(key) == local.stat().st_size:
                continue
            log(f"Uploading {key}")
            store.upload(key, local, content_type, IMMUTABLE)
            uploaded += 1
        # The database goes last, so it never points at a file that isn't there yet.
        store.upload("archive.db", published, "application/vnd.sqlite3", "no-store")
        removed = 0
        for key in sorted(existing):
            if key != "archive.db" and key not in wanted:
                store.delete(key)
                removed += 1
        if state_file:
            state[store_id] = digest
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(state))
    return {**counts, "uploaded": uploaded, "removed": removed, "missing": missing}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", type=Path, default=None, help="archive.db to publish from (default: iCloud Drive › CHANNEL, else ./archive.db)")
    parser.add_argument("--assets", type=Path, default=None, help="its assets folder (default: next to the database)")
    parser.add_argument("--thumbs", type=Path, default=None, help="thumbnail cache (default: the Mac app's)")
    parser.add_argument("--env", type=Path, default=ROOT / ".env.local", help="file with the R2_* keys")
    parser.add_argument("--out", type=Path, default=None, help="publish into this folder instead of R2")
    parser.add_argument("--state", type=Path, default=SUPPORT / "publish-state.json", help="remembers what was published last")
    parser.add_argument("--force", action="store_true", help="publish even if nothing seems to have changed")
    args = parser.parse_args(argv)

    database = args.database or default_database()
    assets = args.assets or database.parent / "assets"
    thumbs = args.thumbs or (SUPPORT / "thumbs" if SUPPORT.exists() else database.parent / "thumbs")
    if not database.is_file():
        print(f"No archive at {database}.", file=sys.stderr)
        return 1
    os.environ["ARENA_THUMBS"] = str(thumbs)
    os.environ["ARENA_ASSETS"] = str(assets)
    sys.path.insert(0, str(ROOT))
    import r2

    if args.out:
        store, store_id = r2.DirStore(args.out), f"dir:{args.out.resolve()}"
    else:
        env = {**r2.read_env_file(args.env), **{k: v for k, v in os.environ.items() if k.startswith("R2_")}}
        if not r2.configured(env):
            print(f"R2 is not set up: run scripts/set-r2-keys.sh (looked in {args.env}).", file=sys.stderr)
            return NOT_CONFIGURED
        store = r2.R2Store(env)
        store_id = f"r2:{store.bucket}/{store.prefix}"
    result = publish(database, assets, store, args.state, store_id, force=args.force)
    note = f", {result['missing']} files missing locally" if result["missing"] else ""
    print(f"Published {result['channels']} favorite channels, {result['blocks']} blocks "
          f"({result['uploaded']} files uploaded, {result['removed']} removed{note}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
