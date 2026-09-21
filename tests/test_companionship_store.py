from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from steady_companion.store import StaleRevisionError, Store


class CompanionshipStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "state"
        self.store = Store(self.data_dir)

    def test_approved_notes_reopen_with_scope_and_verbatim_evidence(self):
        expected = []
        for kind, text in (
            ("moment", "聊过一部电影的开放结局"),
            ("thread", "小说的结局下次继续聊"),
            ("style", "聊电影时可以多说说不同看法"),
        ):
            expected.append(self.store.remember_note(
                text,
                kind,
                scope="聊电影时",
                evidence="  聊电影时可以多说说不同看法  ",
                source="approved_learning",
                expected_revision=self.store.revision(),
            ))
        self.assertEqual(Store(self.data_dir).list_notes(), expected)
        self.assertEqual(self.store.revision(), 3)
        self.assertEqual(self.store.list_memories(), [])
        self.assertEqual(set(expected[0]), {
            "id", "kind", "text", "scope", "evidence", "source", "created_at", "updated_at", "status",
        })
        self.assertEqual(expected[0]["evidence"], "  聊电影时可以多说说不同看法  ")

    def test_v02_database_opens_without_replacing_memories_or_reminders(self):
        legacy_dir = Path(self.temporary.name) / "legacy"
        legacy_dir.mkdir()
        with sqlite3.connect(legacy_dir / "companion.sqlite3") as conn:
            conn.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT INTO metadata VALUES ('revision', 7);
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('fact','preference','boundary','goal')),
                    text TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO memories VALUES
                    (11, 'boundary', '不要用昵称', '2026-01-01', '2026-01-01');
                CREATE TABLE reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL, at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','delivered','cancelled')),
                    memory_id INTEGER REFERENCES memories(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    delivered_at TEXT, cancelled_at TEXT
                );
                INSERT INTO reminders VALUES
                    (19, '约定的提示', '2100-01-01T00:00:00.000000+00:00',
                     'pending', 11, '2026-01-01', '2026-01-01', NULL, NULL);
            """)
        legacy = Store(legacy_dir)
        self.assertEqual(legacy.revision(), 7)
        self.assertEqual(legacy.list_notes(), [])
        self.assertEqual(legacy.list_memories()[0]["text"], "不要用昵称")
        reminder = legacy.list_reminders()[0]
        self.assertEqual((reminder["id"], reminder["memory_id"], reminder["status"]),
                         (19, 11, "pending"))
        legacy.remember_note("上次聊过小说", "moment", expected_revision=7)
        self.assertEqual(legacy.revision(), 8)
        self.assertEqual(legacy.list_reminders()[0], reminder)
        self.assertGreater(legacy.remember("新事实")["id"], 11)
        with legacy._connection() as conn:
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='memories'"
            ).fetchone()[0]
        self.assertNotIn("moment", schema)

    def test_field_limits_and_types_reject_without_any_write(self):
        invalid = [
            {"text": ""}, {"text": "   "}, {"text": None}, {"text": 12},
            {"text": "字" * 1201}, {"kind": "diagnosis"}, {"kind": []},
            {"scope": "字" * 241}, {"scope": None},
            {"evidence": "字" * 301}, {"evidence": []},
            {"source": "model_inference"}, {"source": []},
            {"expected_revision": True}, {"expected_revision": -1},
            {"expected_revision": "0"}, {"expected_revision": 0.0},
        ]
        for override in invalid:
            args = {"text": "有明确范围的偏好", "kind": "style", **override}
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.store.remember_note(**args)
        self.assertEqual(self.store.revision(), 0)
        self.assertEqual(self.store.list_notes(), [])
        exact = self.store.remember_note(
            "字" * 1200, "style", scope="字" * 240, evidence="字" * 300,
        )
        self.assertEqual(len(exact["text"]), 1200)

    def test_stale_approval_after_external_change_cannot_write(self):
        expected_revision = self.store.revision()
        other = Store(self.data_dir)
        other.remember("新的事实")
        with self.assertRaises(StaleRevisionError):
            self.store.remember_note(
                "旧上下文的提案", "style", source="approved_learning",
                expected_revision=expected_revision,
            )
        self.assertEqual(self.store.list_notes(), [])
        self.assertEqual(self.store.revision(), 1)

    def test_concurrent_approvals_for_same_revision_only_insert_once(self):
        stores = (self.store, Store(self.data_dir))
        ready = threading.Barrier(2)
        expected_revision = self.store.revision()

        def approve(store):
            ready.wait(timeout=5)
            try:
                return store.remember_note(
                    "经过确认的一条共同经历", "moment",
                    expected_revision=expected_revision,
                )
            except StaleRevisionError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(approve, stores))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(len(self.store.list_notes()), 1)
        self.assertEqual(self.store.revision(), expected_revision + 1)

    def test_correction_removes_old_evidence_and_preserves_scope_when_omitted(self):
        marker = "OLD_APPROVED_EXCERPT_59302917048"
        original = self.store.remember_note(
            "聊电影时喜欢长回复", "style", scope="只在聊电影时",
            evidence=marker, source="approved_learning",
        )
        updated = self.store.correct_note(original["id"], "有具体观点时再展开")
        self.assertEqual(updated["scope"], original["scope"])
        self.assertEqual(updated["source"], "user_command")
        self.assertEqual(updated["evidence"], "")
        self.assertEqual(updated["created_at"], original["created_at"])
        self.assertEqual(updated["kind"], "style")
        self.assertEqual(self.store.revision(), 2)
        self.assertNotIn(marker.encode(), self.store.db_path.read_bytes())
        self.assertEqual(self.store.correct_note(
            original["id"], "有具体观点时再展开", scope=""
        )["scope"], "")

    def test_invalid_corrections_and_missing_ids_leave_state_unchanged(self):
        original = self.store.remember_note("某个未完话题", "thread")
        revision = self.store.revision()
        for bad_id in (0, -1, True, "1", 1.1, None):
            for operation in (
                lambda: self.store.correct_note(bad_id, "新内容"),
                lambda: self.store.forget_note(bad_id),
            ):
                with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                    operation()
        for text, kwargs in (("", {}), ("字" * 1201, {}), ("x", {"scope": "字" * 241})):
            with self.subTest(text=text, kwargs=kwargs), self.assertRaises(ValueError):
                self.store.correct_note(original["id"], text, **kwargs)
        with self.assertRaises(ValueError):
            self.store.correct_note(999, "不存在")
        self.assertFalse(self.store.forget_note(999))
        self.assertEqual(self.store.revision(), revision)
        self.assertEqual(self.store.list_notes(), [original])

    def test_forget_removes_note_and_evidence_without_touching_old_memory(self):
        marker = "FORGOTTEN_SHARED_MOMENT_5892919518"
        memory = self.store.remember("保留的事实")
        note = self.store.remember_note(marker, "moment", evidence=marker)
        other = Store(self.data_dir)
        self.assertTrue(other.forget_note(note["id"]))
        self.assertEqual(self.store.list_notes(), [])
        self.assertEqual(self.store.revision(), 3)
        self.assertEqual(self.store.list_memories(), [memory])
        self.assertNotIn(marker.encode(), self.store.db_path.read_bytes())
        self.assertFalse(self.store.forget_note(note["id"]))
        self.assertEqual(self.store.revision(), 3)
        self.assertGreater(self.store.remember_note("另一个话题", "thread")["id"], note["id"])

    def test_wipe_erases_notes_evidence_memories_and_reminders_with_one_revision(self):
        marker = "WIPE_SHARED_EXPERIENCE_95701246015"
        memory = self.store.remember("普通记忆")
        self.store.schedule("提醒", (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                            memory_id=memory["id"])
        self.store.remember_note(marker, "moment", evidence=marker)
        self.store.remember_note("另外的相处记录", "style")
        revision = self.store.revision()
        self.assertEqual(self.store.wipe_all(), {
            "memories": 1, "reminders": 1, "companion_notes": 2,
        })
        self.assertEqual(self.store.revision(), revision + 1)
        reopened = Store(self.data_dir)
        self.assertEqual(reopened.list_notes(), [])
        self.assertEqual(reopened.list_memories(), [])
        self.assertEqual(reopened.list_reminders(), [])
        self.assertNotIn(marker.encode(), self.store.db_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
