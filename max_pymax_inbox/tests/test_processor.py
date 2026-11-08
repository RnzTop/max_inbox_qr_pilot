"""Offline MAX message normalization and >1000-message recovery tests."""
import asyncio
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from inbox_db import InboxDB
from processor import enqueue_personal_message, recover_chat, sender_label

NOW = 2000000000000


def msg(mid, timestamp, text="Заявка на ремонт", sender=55, chat=77, attaches=None):
    return SimpleNamespace(
        id=mid, time=timestamp, text=text, sender=sender,
        chat_id=chat, attaches=attaches or [],
    )


class FakeClient:
    def __init__(self, messages=(), broken=False):
        self.messages = list(messages)
        self.broken = broken
        self.calls = []

    async def get_user(self, sender):
        return SimpleNamespace(
            names=[SimpleNamespace(name="Клиент MAX", first_name=None, last_name=None)]
        )

    async def fetch_history(self, **kwargs):
        self.calls.append(kwargs)
        if self.broken:
            raise ConnectionError("pretend private token should not appear in UI")
        older = [
            m for m in self.messages
            if m.chat_id == kwargs["chat_id"] and m.time < kwargs["from_time"]
        ]
        older.sort(key=lambda x: x.time, reverse=True)
        return older[:kwargs["backward"]]


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="max-prod-"))
        self.db = InboxDB(self.folder)
        self.scope = self.db.scope(100, 200, 500, baseline_ms=NOW)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.folder)

    def test_live_personal_message_dedupe_and_content(self):
        c = FakeClient()
        m = msg(1, NOW + 1000)
        first = asyncio.run(enqueue_personal_message(
            c, self.db, self.scope, 100, m, 77, {}
        ))
        second = asyncio.run(enqueue_personal_message(
            c, self.db, self.scope, 100, m, 77, {}
        ))
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIn("Клиент MAX", self.db.reserve(self.scope)[1])
        self.assertEqual(self.db.counts(self.scope)["sending"], 1)

    def test_ignores_self_and_invalid_and_truncates_long_text(self):
        c = FakeClient()
        self.assertFalse(asyncio.run(enqueue_personal_message(
            c, self.db, self.scope, 100,
            msg(1, NOW, sender=100), 77, {}
        )))
        self.assertFalse(asyncio.run(enqueue_personal_message(
            c, self.db, self.scope, 100,
            msg(1, NOW, sender=None), 77, {}
        )))
        self.assertTrue(asyncio.run(enqueue_personal_message(
            c, self.db, self.scope, 100,
            msg(2, NOW + 1000, text="a" * 5000), 77, {}
        )))
        body = self.db.reserve(self.scope)[1]
        self.assertLessEqual(len(body), 4000)
        self.assertIn("сокращён", body)

    def test_attachment_summary_does_not_download_or_forward_media(self):
        c = FakeClient()
        attachment = SimpleNamespace(type="PHOTO")
        m = msg(1, NOW + 1000, text="", attaches=[attachment])
        self.assertTrue(asyncio.run(enqueue_personal_message(
            c, self.db, self.scope, 100, m, 77, {}
        )))
        self.assertIn("Фото", self.db.reserve(self.scope)[1])

    def test_catch_up_1200_messages_in_three_resumable_passes(self):
        c = FakeClient([
            msg(i, NOW + i * 1000) for i in range(1, 1201)
        ])
        async def run(now):
            return await recover_chat(
                c, self.db, self.scope, 100, 77, {},
                now_ms=now,
            )
        one = asyncio.run(run(NOW + 1500000))
        self.assertFalse(one["complete"])
        self.assertEqual(one["error"], "page_limit")
        self.db.close()
        self.db = InboxDB(self.folder)
        two = asyncio.run(run(NOW + 1600000))
        self.assertFalse(two["complete"])
        three = asyncio.run(run(NOW + 1700000))
        self.assertTrue(three["complete"])
        self.assertEqual(one["new"] + two["new"] + three["new"], 1200)
        self.assertEqual(self.db.counts(self.scope)["queued"], 1200)
        four = asyncio.run(run(NOW + 1800000))
        self.assertTrue(four["complete"])
        self.assertEqual(four["new"], 0)
        self.assertEqual(self.db.counts(self.scope)["queued"], 1200)

    def test_network_fault_does_not_advance_cursor(self):
        c = FakeClient(broken=True)
        result = asyncio.run(recover_chat(
            c, self.db, self.scope, 100, 77, {},
            now_ms=NOW + 100000,
        ))
        self.assertEqual(result["error"], "history_failed")
        scan = self.db.scan_position(self.scope, 77)
        self.assertEqual(scan[0], NOW)
        self.assertIsNotNone(scan[1])

    def test_same_millisecond_full_page_is_fail_closed(self):
        c = FakeClient([
            msg(i, NOW + 1000) for i in range(1, 57)
        ])
        result = asyncio.run(recover_chat(
            c, self.db, self.scope, 100, 77, {},
            now_ms=NOW + 200000,
        ))
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"], "timestamp_collision")


if __name__ == "__main__":
    unittest.main()
