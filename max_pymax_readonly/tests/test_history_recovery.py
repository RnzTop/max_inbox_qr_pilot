"""Offline, deterministic tests for bounded MAX history catch-up."""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from history_recovery import recover_history
from id_store import SeenIds

NOW = 2_000_000_000_000


def msg(mid, timestamp, text=None, sender=200, chat=42):
    return SimpleNamespace(
        id=mid, time=timestamp, chat_id=chat, sender=sender,
        text=text if text is not None else "MAX HA TEST 8362",
    )


class FakeHistory:
    def __init__(self, items=(), failed=False):
        self.items = list(items)
        self.calls = []
        self.failed = failed

    async def fetch_history(self, **kwargs):
        self.calls.append(kwargs)
        if self.failed:
            raise RuntimeError("network failure with a pretend secret")
        valid = [x for x in self.items
                 if x.chat_id == kwargs["chat_id"]
                 and x.time < kwargs["from_time"]]
        valid.sort(key=lambda x: x.time, reverse=True)
        return valid[:kwargs["backward"]]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pymax-catchup-"))
        self.store = SeenIds(self.dir)
        self.store.bind_chat(100, "Test", 42)

    def tearDown(self):
        self.store.conn.close()
        shutil.rmtree(self.dir)

    def run_recovery(self, client, now, **kw):
        return asyncio.run(recover_history(
            client, self.store, 100, "Test", 42,
            now_ms=now, **kw,
        ))

    def test_first_start_baselines_and_replays_only_missed_messages(self):
        client = FakeHistory([msg(1, NOW - 60_000)])
        first = self.run_recovery(client, NOW)
        self.assertTrue(first.initialized)
        self.assertEqual(first.recovered, 0)
        self.assertEqual(len(client.calls), 0)
        self.assertEqual(self.store.history_cursor(100, "Test"), NOW)
        client.items.extend([
            msg(2, NOW + 10_000),
            msg(3, NOW + 15_000),
            msg(4, NOW + 18_000, sender=100),
            msg(5, NOW + 21_000, text="ordinary private text"),
            msg(6, NOW + 22_000, chat=43),
        ])
        second = self.run_recovery(client, NOW + 60_000)
        self.assertTrue(second.complete)
        self.assertEqual(second.recovered, 2)
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.history_cursor(100, "Test"), NOW + 60_000)
        self.assertEqual(self.run_recovery(client, NOW + 61_000).recovered, 0)
        self.assertEqual(self.store.count(), 2)
        # Only keyed fingerprints, not raw content/IDs, are stored.
        raw = (self.dir / "seen.sqlite3").read_bytes()
        self.assertNotIn(b"MAX HA TEST", raw)
        self.assertNotIn(b"ordinary private text", raw)

    def test_overlap_deduplicates_live_delivery(self):
        self.store.advance_history_cursor(100, "Test", NOW)
        self.assertTrue(self.store.observe(42, 55))
        client = FakeHistory([
            msg(55, NOW + 12_000),
            msg(56, NOW + 15_000),
        ])
        result = self.run_recovery(client, NOW + 20_000)
        self.assertTrue(result.complete)
        self.assertEqual(result.recovered, 1)
        self.assertEqual(self.store.count(), 2)

    def test_pagination_and_truncation_leave_cursor_unchanged(self):
        self.store.advance_history_cursor(100, "Test", NOW)
        data = [msg(mid, NOW + mid * 1000) for mid in range(1, 21)]
        client = FakeHistory(data)
        partial = self.run_recovery(
            client, NOW + 30_000, page_size=3, max_pages=2
        )
        self.assertFalse(partial.complete)
        self.assertEqual(partial.error, "history_limit_reached")
        self.assertEqual(self.store.history_cursor(100, "Test"), NOW)
        complete = self.run_recovery(
            client, NOW + 31_000, page_size=10, max_pages=10
        )
        self.assertTrue(complete.complete)
        self.assertEqual(self.store.count(), 20)
        self.assertEqual(self.store.history_cursor(100, "Test"), NOW + 30_000)

    def test_errors_never_advance_history_cursor(self):
        self.store.advance_history_cursor(100, "Test", NOW)
        client = FakeHistory(failed=True)
        result = self.run_recovery(client, NOW + 60_000)
        self.assertFalse(result.complete)
        self.assertEqual(result.error, "history_request_failed")
        self.assertEqual(self.store.history_cursor(100, "Test"), NOW)

    def test_unverified_chat_is_never_scanned(self):
        client = FakeHistory()
        result = asyncio.run(recover_history(
            client, self.store, 100, "Test", 43, now_ms=NOW,
        ))
        self.assertEqual(result.error, "unverified_dialog")
        self.assertFalse(client.calls)


if __name__ == "__main__":
    unittest.main()
