"""SQLite outbox correctness, privacy and outage resilience tests."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inbox_db import InboxDB


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="max-inbox-"))
        self.db = InboxDB(self.dir)
        self.scope = self.db.scope(100, 200, -12345, baseline_ms=2000000000000)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir)

    def test_dedupe_and_delivery(self):
        self.assertTrue(self.db.enqueue(self.scope, 333, 444, "New customer message"))
        self.assertFalse(self.db.enqueue(self.scope, 333, 444, "Duplicate"))
        self.assertTrue(self.db.enqueue(self.scope, 333, 445, "Second message"))
        item = self.db.reserve(self.scope)
        self.assertIsNotNone(item)
        self.assertEqual(item[1], "New customer message")
        self.assertTrue(self.db.finish(item[0], "sent", "outbound-mid"))
        self.assertFalse(self.db.finish(item[0], "sent", "outbound-mid"))
        self.assertEqual(self.db.counts(self.scope)["sent"], 1)
        self.assertEqual(self.db.counts(self.scope)["queued"], 1)
        # Raw SQLite bytes can coincidentally contain a 3-digit sequence.
        # Verify the actual stored schema/data rather than scanning noise.
        columns = {
            row[1] for row in self.db.conn.execute("PRAGMA table_info(outbox)")
        }
        self.assertNotIn("chat_id", columns)
        self.assertNotIn("message_id", columns)
        hashes = [
            row[0] for row in self.db.conn.execute("SELECT fingerprint FROM outbox")
        ]
        self.assertTrue(all(len(value) == 64 for value in hashes))

    def test_interrupted_post_is_uncertain_and_not_resent(self):
        self.db.enqueue(self.scope, 333, 444, "Important notice")
        sent = self.db.reserve(self.scope)
        self.assertIsNotNone(sent)
        self.db.close()
        self.db = InboxDB(self.dir)
        self.assertEqual(self.db.counts(self.scope)["uncertain"], 1)
        self.assertIsNone(self.db.reserve(self.scope))

    def test_scope_protects_existing_work_chat_and_bot(self):
        self.db.enqueue(self.scope, 1, 2, "Private message")
        new_target = self.db.scope(100, 200, -99999, baseline_ms=2000000000000)
        new_bot = self.db.scope(100, 201, -12345, baseline_ms=2000000000000)
        self.assertNotEqual(self.scope, new_target)
        self.assertNotEqual(self.scope, new_bot)
        self.assertIsNone(self.db.reserve(new_target))
        self.assertIsNone(self.db.reserve(new_bot))

    def test_backfill_cursor_resumes_across_processes(self):
        self.assertEqual(self.db.scan_position(self.scope, 777)[0], 2000000000000)
        state = self.db.begin_scan(self.scope, 777, 2000000100000)
        self.assertEqual(state[1], 2000000100000)
        self.db.progress_scan(self.scope, 777, state[1], 2000000080000)
        self.db.close()
        self.db = InboxDB(self.dir)
        self.assertEqual(self.db.scan_position(self.scope, 777)[3], 2000000080000)
        self.db.complete_scan(self.scope, 777, state[1])
        self.assertEqual(self.db.scan_position(self.scope, 777),
                         (2000000100000, None, None, None))


if __name__ == "__main__":
    unittest.main()
