import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
import zipfile

from transfer_core import export_bundle, restore_bundle, inspect_bundle, scan_sessions, safe_name, remap_cwd, database


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "한글 프로젝트"
        self.home = self.root / "home"
        self.project.mkdir(); (self.home / "sessions").mkdir(parents=True)
        (self.project / "main.py").write_text("print('hello')", encoding="utf-8")
        (self.project / "AGENTS.md").write_text("프로젝트 지침", encoding="utf-8")
        (self.project / ".env").write_text("SECRET=excluded")
        (self.home / "auth.json").write_text("do not copy")
        (self.home / "AGENTS.md").write_text("global instructions")
        self.sid = str(uuid.uuid4())
        self.other = str(uuid.uuid4())
        self.rollout = self.home / "sessions" / "rollout.jsonl"
        self.events = [
            {"type": "session_meta", "payload": {"id": self.sid, "cwd": str(self.project)}},
            {"type": "turn_context", "payload": {"cwd": str(self.project / "src")}},
            {"type": "response_item", "payload": {"role": "user", "content": [{"text": str(self.project)}]}},
        ]
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in self.events), encoding="utf-8")
        with database(self.home / "state_5.sqlite") as c:
            c.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,title TEXT,archived INTEGER,history_mode TEXT,project_id TEXT,thread_section_id TEXT,is_pinned INTEGER)")
            c.execute("CREATE TABLE projects(id TEXT)")
            c.execute("INSERT INTO projects VALUES ('private-other-project')")
            c.execute("INSERT INTO threads VALUES(?,?,?,?,0,'paginated','p','s',1)", (self.sid, str(self.project), str(self.rollout), "대화 제목"))
            c.execute("INSERT INTO threads VALUES(?,?,?,?,0,'legacy',NULL,NULL,0)", (self.other, str(self.root / "other"), "missing", "excluded"))
        self.package = self.root / "project.codextransfer"

    def tearDown(self):
        self.tmp.cleanup()

    def export(self):
        return export_bundle(self.project, self.home, self.package, "다음 작업")

    def test_roundtrip_filter_and_paths(self):
        before = self.rollout.read_bytes()
        m = self.export()
        self.assertEqual(len(m["sessions"]), 1)
        self.assertIn(".env", m["excluded"])
        with zipfile.ZipFile(self.package) as z:
            self.assertNotIn("project/.env", z.namelist())
            self.assertFalse(any("auth.json" in p for p in z.namelist()))
        target = self.root / "새 PC"
        restore_bundle(self.package, target)
        self.assertEqual((target / "project/main.py").read_bytes(), (self.project / "main.py").read_bytes())
        rollout_relative = m["sessions"][0]["archive_path"]
        events = [json.loads(l) for l in (target / rollout_relative).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events[0]["payload"]["cwd"], str(target / "project"))
        self.assertEqual(events[1]["payload"]["cwd"], str(target / "project/src"))
        self.assertEqual(events[2], self.events[2])
        with database(target / "codex/state_5.sqlite") as c:
            rows = c.execute("SELECT id,cwd,rollout_path,project_id FROM threads").fetchall()
            self.assertEqual(rows, [(self.sid, str(target / "project"), str(target / rollout_relative), None)])
            self.assertEqual(c.execute("SELECT count(*) FROM projects").fetchone()[0], 0)
        self.assertEqual(self.rollout.read_bytes(), before)
        sessions, warnings = scan_sessions(target / "codex", target / "project")
        self.assertEqual(len(sessions), 1); self.assertFalse(warnings)

    def test_existing_destination_is_untouched(self):
        self.export()
        target = self.root / "existing"; target.mkdir()
        (target / "keep").write_text("keep")
        with self.assertRaises(FileExistsError): restore_bundle(self.package, target)
        self.assertEqual((target / "keep").read_text(), "keep")

    def test_no_overwrite_package(self):
        self.export(); original = self.package.read_bytes()
        with self.assertRaises(FileExistsError): self.export()
        self.assertEqual(self.package.read_bytes(), original)

    def test_hash_failure_leaves_no_destination(self):
        self.export()
        altered = self.root / "altered.codextransfer"
        with zipfile.ZipFile(self.package) as source, zipfile.ZipFile(altered, "w") as dest:
            for name in source.namelist():
                data = source.read(name)
                if name == "project/main.py": data = b"x" * len(data)
                dest.writestr(name, data)
        target = self.root / "failed"
        with self.assertRaisesRegex(ValueError, "무결성"): restore_bundle(altered, target)
        self.assertFalse(target.exists())

    def test_windows_archive_attacks(self):
        for name in ("../evil", "/abs", "C:/evil", "a\\b", "project/NUL.txt", "project/a:stream", "project/trailing.", "a//b", "a/./b"):
            with self.subTest(name=name), self.assertRaises(ValueError): safe_name(name)

    def test_duplicate_case_attack(self):
        self.export()
        with zipfile.ZipFile(self.package, "a") as z: z.writestr("PROJECT/main.py", b"evil")
        with self.assertRaises(ValueError): inspect_bundle(self.package)

    def test_worktree_rejected(self):
        (self.project / ".git").write_text("gitdir: /external/repo")
        with self.assertRaisesRegex(ValueError, "worktree"): self.export()

    def test_fallback_without_database(self):
        (self.home / "state_5.sqlite").unlink()
        sessions, warnings = scan_sessions(self.home, self.project)
        self.assertEqual(sessions[0]["id"], self.sid)

    def test_component_based_mapping(self):
        self.assertEqual(remap_cwd(r"C:\oldish\app", r"C:\old", r"D:\new"), r"C:\oldish\app")
        self.assertEqual(remap_cwd(r"c:\OLD\src", r"C:\old", self.root), str(self.root / "src"))

    def test_export_not_inside_project(self):
        with self.assertRaises(ValueError): export_bundle(self.project, self.home, self.project / "bundle.codextransfer")


if __name__ == "__main__":
    unittest.main()
