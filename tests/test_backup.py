"""Server bundle and backup: consistent DB snapshot, nothing secret or stale left in by mistake."""
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from backend.backup import make_backup, make_server_bundle
from backend.db import Database


def build_project(root: Path) -> Path:
    (root / "backend" / "__pycache__").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "run.py").write_text("print('run')\n", encoding="utf-8")
    (root / "app.js").write_text("// app\n", encoding="utf-8")
    (root / "backend" / "x.py").write_text("X = 1\n", encoding="utf-8")
    (root / "backend" / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"junk")
    (root / "deploy" / "setup_server.sh").write_bytes(b"#!/usr/bin/env bash\r\necho hi\r\n")
    (root / ".env").write_text("GEMINI_API_KEY=placeholder\n", encoding="utf-8")
    data = root / "data"
    (data / "uploads" / "u1").mkdir(parents=True)
    (data / "uploads" / "u1" / "p.jpg").write_bytes(b"\xff\xd8photo")
    (data / "secret.key").write_text("k" * 64, encoding="utf-8")
    db = Database(data / "healthlingo.db")
    with db.connect() as conn:  # the connection stays open (WAL), like a running server
        conn.execute("INSERT INTO users (google_sub, email, name, picture, created_at, last_login_at)"
                     " VALUES ('sub-1', 'a@example.com', 'A', '', 'now', 'now')")
    return data, db


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data, db = build_project(self.root)
        self.addCleanup(db.close_thread_connection)

    def extract(self, archive: Path) -> Path:
        dest = self.root / "out"
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
        return dest

    def test_full_bundle_has_code_env_and_readable_db_snapshot(self):
        summary = make_server_bundle(self.root, self.data, self.root / "dist" / "b.tar.gz")
        self.assertTrue(summary["env"])
        self.assertTrue(summary["database"])
        out = self.extract(self.root / "dist" / "b.tar.gz") / "healthlingo"
        self.assertTrue((out / "run.py").is_file())
        self.assertTrue((out / ".env").is_file())
        self.assertFalse(any("__pycache__" in p.parts for p in out.rglob("*")))
        self.assertEqual((out / "deploy" / "setup_server.sh").read_bytes(), b"#!/usr/bin/env bash\necho hi\n")
        self.assertEqual((out / "data" / "uploads" / "u1" / "p.jpg").read_bytes(), b"\xff\xd8photo")
        self.assertTrue((out / "data" / "secret.key").is_file())
        self.assertFalse((out / "data" / "healthlingo.db-wal").exists())
        conn = sqlite3.connect(out / "data" / "healthlingo.db")
        try:
            self.assertEqual(conn.execute("SELECT email FROM users").fetchall(), [("a@example.com",)])
        finally:
            conn.close()

    def test_code_only_bundle_leaves_out_data_and_env(self):
        summary = make_server_bundle(self.root, self.data, self.root / "c.tar.gz", include_data=False, include_env=False)
        self.assertFalse(summary["env"])
        with tarfile.open(self.root / "c.tar.gz") as tar:
            names = tar.getnames()
        self.assertIn("healthlingo/run.py", names)
        self.assertFalse(any(n.startswith("healthlingo/data") or n.endswith(".env") for n in names))

    def test_backup_contains_data_only(self):
        summary = make_backup(self.data, self.root / "backups" / "b.tar.gz")
        self.assertEqual(summary["files"], 2)  # photo + secret.key
        with tarfile.open(self.root / "backups" / "b.tar.gz") as tar:
            names = set(tar.getnames())
        self.assertEqual(names, {"data/healthlingo.db", "data/secret.key", "data/uploads/u1/p.jpg"})


if __name__ == "__main__":
    unittest.main()
