import os
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_paths


class RuntimePathTests(unittest.TestCase):
    def test_source_uses_existing_project_data(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime_paths.sys, "frozen", False, create=True):
            self.assertEqual(runtime_paths.data_dir(), runtime_paths.resource_root() / "data")

    def test_frozen_windows_uses_local_appdata(self):
        with patch.dict(os.environ, {"LOCALAPPDATA": "C:/Users/test/AppData/Local"}, clear=True), patch.object(runtime_paths.sys, "frozen", True, create=True), patch.object(runtime_paths.sys, "platform", "win32"), patch.object(runtime_paths.Path, "home", side_effect=RuntimeError("No home available")):
            self.assertEqual(runtime_paths.data_dir(), Path("C:/Users/test/AppData/Local/juutoki"))

    def test_explicit_data_directory_is_respected(self):
        with tempfile.TemporaryDirectory() as folder:
            expected = Path(folder) / "custom"
            with patch.dict(os.environ, {"NEWT_DATA_DIR": str(expected)}):
                self.assertEqual(runtime_paths.data_dir(), expected.resolve())

    def test_bundled_browser_path_is_separate_from_data(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(runtime_paths.sys, "frozen", True, create=True):
            self.assertEqual(runtime_paths.browser_dir(), runtime_paths.resource_root() / "browsers")

    def test_seed_restores_rows_but_never_overwrites_existing_db(self):
        with tempfile.TemporaryDirectory() as folder:
            seed = Path(folder) / "seed.sqlite3"
            dest = Path(folder) / "user" / "newt.sqlite3"
            with closing(sqlite3.connect(seed)) as db, db:
                db.execute("CREATE TABLE saved(value TEXT)")
                db.execute("INSERT INTO saved VALUES ('기존 수집 내용')")
            self.assertTrue(runtime_paths.install_seed(seed, dest))
            with closing(sqlite3.connect(dest)) as db, db:
                self.assertEqual(db.execute("SELECT value FROM saved").fetchone()[0], "기존 수집 내용")
                db.execute("INSERT INTO saved VALUES ('사용자가 이어 받은 내용')")
            self.assertFalse(runtime_paths.install_seed(seed, dest))
            with closing(sqlite3.connect(dest)) as db, db:
                self.assertEqual(db.execute("SELECT count(*) FROM saved").fetchone()[0], 2)

    def test_missing_seed_leaves_new_install_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder) / "newt.sqlite3"
            self.assertFalse(runtime_paths.install_seed(Path(folder) / "absent", dest))
            self.assertFalse(dest.exists())

    def test_invalid_seed_does_not_leave_broken_destination(self):
        with tempfile.TemporaryDirectory() as folder:
            seed = Path(folder) / "bad.sqlite3"
            seed.write_bytes(b"not a database")
            dest = Path(folder) / "user.sqlite3"
            with self.assertRaises(sqlite3.DatabaseError):
                runtime_paths.install_seed(seed, dest)
            self.assertFalse(dest.exists())

    def test_destination_is_not_published_until_backup_finishes(self):
        with tempfile.TemporaryDirectory() as folder:
            seed, dest = Path(folder) / "seed.sqlite3", Path(folder) / "user.sqlite3"
            connection = sqlite3.connect(seed)
            connection.execute("CREATE TABLE saved(value TEXT)")
            connection.commit()
            connection.close()
            observed = []
            real_connect = sqlite3.connect

            class ObservedConnection(sqlite3.Connection):
                def backup(self, target, *args, **kwargs):
                    observed.append(dest.exists())
                    return super().backup(target, *args, **kwargs)

            def connect(*args, **kwargs):
                return real_connect(*args, factory=ObservedConnection, **kwargs)

            with patch.object(runtime_paths.sqlite3, "connect", side_effect=connect):
                runtime_paths.install_seed(seed, dest)
            self.assertEqual(observed, [False])
            self.assertTrue(dest.exists())
