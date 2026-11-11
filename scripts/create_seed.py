"""Capture a consistent SQLite snapshot, including committed WAL contents."""
import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def snapshot(source, destination):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("Seed destination must differ from live DB")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="seed-", suffix=".sqlite3", dir=destination.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        original = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(temp)
        try:
            original.backup(target)
            assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            books = target.execute("SELECT count(*) FROM books").fetchone()[0]
            count, first, last = target.execute("SELECT count(*),min(number),max(number) FROM chapters").fetchone()
        finally:
            target.close()
            original.close()
        os.replace(temp, destination)
        manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "books": books, "chapters": count,
                    "first_chapter": first, "last_chapter": last,
                    "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
        destination.with_name("manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return manifest
    finally:
        temp.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "newt.sqlite3")
    parser.add_argument("--destination", type=Path, default=ROOT / "seed" / "newt.sqlite3")
    args = parser.parse_args()
    print(json.dumps(snapshot(args.source, args.destination), ensure_ascii=False, indent=2))
