from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile
import threading
import unittest

from steady_companion.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / "state"
        self.store = Store(self.data_dir)
        self.future = datetime.now(timezone.utc) + timedelta(days=3)
        self.at = self.future.isoformat()
        self.after = (self.future + timedelta(hours=1)).isoformat()

    def test_only_explicit_memories_persist_across_reopening(self):
        self.assertEqual(self.store.list_memories(), [])
        memory = self.store.remember("不要叫我宝贝", "boundary")
        with Store(self.data_dir) as reopened:
            self.assertEqual(reopened.list_memories(), [memory])
            self.assertEqual(reopened.revision(), 1)
        self.assertEqual(set(memory), {"id", "kind", "text", "created_at", "updated_at", "source", "scope", "status"})

    def test_correct_replaces_old_text_without_history(self):
        marker = "OLD_CONFIDENTIAL_MEMORY_3479367521338"
        memory = self.store.remember(marker)
        corrected = self.store.correct(memory["id"], "新的内容")
        self.assertEqual(corrected["created_at"], memory["created_at"])
        self.assertEqual(corrected["text"], "新的内容")
        self.assertEqual(self.store.revision(), 2)
        self.assertNotIn(marker.encode(), self.store.db_path.read_bytes())
        self.assertFalse(Path(str(self.store.db_path) + "-journal").exists())
        self.assertFalse(Path(str(self.store.db_path) + "-wal").exists())

    def test_forget_removes_data_and_does_not_reuse_identifiers(self):
        marker = "DELETED_CONFIDENTIAL_MEMORY_2688134792121"
        memory = self.store.remember(marker)
        self.assertTrue(self.store.forget(memory["id"]))
        rev = self.store.revision()
        self.assertFalse(self.store.forget(memory["id"]))
        self.assertEqual(self.store.revision(), rev)
        self.assertEqual(self.store.list_memories(), [])
        self.assertNotIn(marker.encode(), self.store.db_path.read_bytes())
        self.assertGreater(self.store.remember("新记忆")["id"], memory["id"])

    def test_invalid_ids_and_contents_fail_without_writing(self):
        for bad_id in (0, -1, True, "1", 1.1, None):
            operations = [
                lambda: self.store.correct(bad_id, "x"),
                lambda: self.store.forget(bad_id),
                lambda: self.store.cancel_reminder(bad_id),
                lambda: self.store.deliver_reminder(bad_id),
            ]
            if bad_id is not None:  # None explicitly means an unlinked reminder.
                operations.append(lambda: self.store.schedule("x", self.at, bad_id))
            for operation in operations:
                with self.subTest(bad_id=bad_id), self.assertRaises(ValueError):
                    operation()
        for text in (None, "", "   ", 42):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.store.remember(text)
        for kind in ("diagnosis", [], None):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.store.remember("x", kind)
        with self.assertRaises(ValueError):
            self.store.correct(999, "x")
        with self.assertRaises(ValueError):
            self.store.schedule("x", self.at, 999)
        self.assertEqual(self.store.revision(), 0)

    def test_sql_metacharacters_are_stored_as_content(self):
        text = "'); DROP TABLE memories; --"
        self.store.remember(text)
        self.assertEqual(self.store.list_memories()[0]["text"], text)
        self.store.remember("仍然可以保存")
        self.assertEqual(len(self.store.list_memories()), 2)

    def test_schedule_requires_explicit_timezone_and_future(self):
        for at in ("2035-01-01T12:00:00", "not-a-date", "2000-01-01T00:00:00Z", "", None):
            with self.subTest(at=at), self.assertRaises(ValueError):
                self.store.schedule("联系我", at)
        local_at = self.future.astimezone(timezone(timedelta(hours=8))).isoformat()
        reminder = self.store.schedule("联系我", local_at)
        self.assertEqual(datetime.fromisoformat(reminder["at"]), self.future)
        self.assertTrue(reminder["at"].endswith("+00:00"))
        self.assertEqual(reminder["status"], "pending")
        with self.assertRaises(ValueError):
            self.store.due_reminders("2035-01-01T00:00:00")

    def test_due_pending_delivery_is_once_and_never_early(self):
        reminder = self.store.schedule("约定的提醒", self.at)
        self.assertIsNone(self.store.deliver_reminder(reminder["id"]))
        self.assertEqual(self.store.due_reminders(), [])
        self.assertEqual(self.store.due_reminders(self.after), [reminder])
        delivered = self.store.deliver_reminder(reminder["id"], self.after)
        self.assertEqual(delivered["status"], "delivered")
        self.assertIsNotNone(delivered["delivered_at"])
        self.assertEqual(self.store.due_reminders(self.after), [])
        self.assertIsNone(self.store.deliver_reminder(reminder["id"], self.after))
        self.assertFalse(self.store.cancel_reminder(reminder["id"]))

    def test_cancellation_is_rechecked_after_due_list_was_read(self):
        reminder = self.store.schedule("已撤销的约定", self.at)
        candidate = self.store.due_reminders(self.after)[0]
        other_process = Store(self.data_dir)
        self.assertTrue(other_process.cancel_reminder(reminder["id"]))
        self.assertIsNone(self.store.deliver_reminder(candidate["id"], self.after))
        cancelled = self.store.list_reminders()[0]
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNotNone(cancelled["cancelled_at"])
        self.assertFalse(self.store.cancel_reminder(reminder["id"]))

    def test_atomic_claim_prevents_concurrent_duplicate_delivery(self):
        reminder = self.store.schedule("只显示一次", self.at)
        other_store = Store(self.data_dir)
        ready = threading.Barrier(2)

        def claim(store):
            ready.wait(timeout=5)
            return store.deliver_reminder(reminder["id"], self.after)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (self.store, other_store)))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_correction_cancels_and_redacts_all_linked_reminder_text(self):
        marker = "STALE_LINKED_MEMORY_3839391882391"
        memory = self.store.remember(marker)
        pending = self.store.schedule(marker, self.at, memory["id"])
        delivered = self.store.schedule(marker, self.at, memory["id"])
        self.store.deliver_reminder(delivered["id"], self.after)
        unrelated = self.store.schedule("与记忆无关的独立提醒", self.at)
        self.store.correct(memory["id"], "更正后的事实")
        reminders = {row["id"]: row for row in self.store.list_reminders()}
        self.assertEqual(reminders[pending["id"]]["status"], "cancelled")
        self.assertEqual(reminders[pending["id"]]["text"], "")
        self.assertEqual(reminders[delivered["id"]]["text"], "")
        self.assertEqual(reminders[unrelated["id"]]["status"], "pending")
        self.assertIsNone(self.store.deliver_reminder(pending["id"], self.after))
        self.assertNotIn(marker.encode(), self.store.db_path.read_bytes())

    def test_forget_cancels_linked_reminder_but_retains_unrelated_one(self):
        memory = self.store.remember("可删除的记忆")
        linked = self.store.schedule("依赖这条记忆", self.at, memory["id"])
        unrelated = self.store.schedule("独立约定", self.at)
        self.store.forget(memory["id"])
        reminders = {row["id"]: row for row in self.store.list_reminders()}
        self.assertEqual(reminders[linked["id"]]["status"], "cancelled")
        self.assertEqual(reminders[linked["id"]]["text"], "")
        self.assertIsNone(reminders[linked["id"]]["memory_id"])
        self.assertEqual(self.store.due_reminders(self.after), [unrelated])

    def test_forget_all_and_wipe_have_distinct_scope(self):
        memory = self.store.remember("第一条")
        self.store.remember("第二条")
        self.store.schedule("关联提醒", self.at, memory["id"])
        self.store.schedule("独立提醒", self.at)
        self.assertEqual(self.store.forget_all(), 2)
        self.assertEqual(self.store.forget_all(), 0)
        self.assertEqual(len(self.store.due_reminders(self.after)), 1)
        revision = self.store.revision()
        self.assertEqual(self.store.wipe_all(), {"memories": 0, "reminders": 2})
        self.assertGreater(self.store.revision(), revision)
        self.assertEqual(Store(self.data_dir).list_reminders(), [])

    def test_revision_changes_are_visible_across_instances(self):
        other = Store(self.data_dir)
        original = other.revision()
        self.store.remember("明确授权保存")
        self.assertGreater(other.revision(), original)
        original = self.store.revision()
        other.wipe_all()
        self.assertGreater(self.store.revision(), original)

    @unittest.skipIf(os.name == "nt", "POSIX modes are not supported on Windows")
    def test_private_permissions_and_database_settings(self):
        self.assertEqual(self.data_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.db_path.stat().st_mode & 0o777, 0o600)
        with self.store._connection() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
            self.assertEqual(conn.execute("PRAGMA secure_delete").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
