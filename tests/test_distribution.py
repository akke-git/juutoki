import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app as service
from scripts.create_seed import snapshot
from scripts.create_source_archive import create_archive


class DistributionTests(unittest.TestCase):
    def test_seed_snapshot_includes_committed_wal_and_manifest_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            source, seed = Path(folder) / "live.db", Path(folder) / "seed" / "newt.sqlite3"
            db = sqlite3.connect(source)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE books(id INTEGER)")
                db.execute("CREATE TABLE chapters(number INTEGER,body TEXT)")
                db.execute("INSERT INTO books VALUES (1)")
                db.execute("INSERT INTO chapters VALUES (299,'WAL에 저장된 본문')")
                db.commit()
                manifest = snapshot(source, seed)
                restored = sqlite3.connect(seed)
                try:
                    self.assertEqual(restored.execute("SELECT body FROM chapters").fetchone()[0], "WAL에 저장된 본문")
                finally:
                    restored.close()
                self.assertEqual(manifest["chapters"], 1)
                self.assertEqual(manifest["last_chapter"], 299)
                self.assertEqual(manifest["sha256"], hashlib.sha256(seed.read_bytes()).hexdigest())
            finally:
                db.close()

    def test_source_archive_includes_seed_and_workflow_but_excludes_live_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("app.py", ".env", "data/newt.sqlite3", ".venv/private", ".browsers/private",
                         "seed/newt.sqlite3", ".github/workflows/windows-build.yml", "tests/__pycache__/a.pyc"):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("test")
            archive = create_archive(root)
            with zipfile.ZipFile(archive) as packed:
                self.assertEqual(set(packed.namelist()), {"app.py", "seed/newt.sqlite3", ".github/workflows/windows-build.yml"})

    def test_first_run_restores_history_and_txt_without_overwriting_later_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            live = root / "original"
            with patch.multiple(service, DATA=live, DB=live / "newt.sqlite3"):
                service.initialize()
                book = {"id": "book", "url": "https://example.com/novel/1", "title": "테스트 작품"}
                chapter = {"number": 1, "title": "첫 회차", "url": "https://example.com/novel/1/1"}
                service.save_chapter(book, chapter, "보관된 본문")
                service.save_job({"id": "old", "title": book["title"], "url": book["url"], "status": "completed",
                                  "items": [{**chapter, "status": "saved"}], "export_count": 1})
                snapshot(service.DB, root / "seed" / "newt.sqlite3")
            fresh = root / "user"
            with patch.multiple(service, BASE=root, DATA=fresh, DB=fresh / "newt.sqlite3"), patch.object(service.runtime_paths, "is_bundled", return_value=True):
                service.initialize()
                self.assertEqual(service.find_chapter(chapter["url"])["body"], "보관된 본문")
                txt = (fresh / "exports" / "old.txt").read_text(encoding="utf-8-sig")
                self.assertIn("보관된 본문", txt)
                self.assertEqual(service.get_job("old")["export_count"], 1)
                service.save_chapter(book, chapter, "사용자가 새로 저장한 본문")
                service.initialize()
                self.assertEqual(service.find_chapter(chapter["url"])["body"], "사용자가 새로 저장한 본문")
