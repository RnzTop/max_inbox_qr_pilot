"""Callback security, first-winner arbitration and persistent card-edit tests."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from callbacks import handle_callback
from inbox_db import InboxDB


def click(fingerprint, mid="work-100", chat=-12345, employee=51, bot=999):
    return {
        "update_type": "message_callback",
        "message": {
            "recipient": {"chat_id": chat, "chat_type": "chat"},
            "body": {"mid": mid},
            "sender": {"user_id": bot},
        },
        "callback": {
            "callback_id": "cb-123",
            "payload": "take:" + fingerprint,
            "user": {"user_id": employee, "first_name": "Анна",
                     "last_name": "Смирнова", "is_bot": False},
        },
    }


class CallbackTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="max-claims-"))
        self.db = InboxDB(self.folder)
        self.scope = self.db.scope(100, 999, -12345)
        self.db.enqueue(self.scope, 42, 77, "Обращение")
        self.fingerprint, _ = self.db.reserve(self.scope)
        self.db.finish(self.fingerprint, "sent", "work-100")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.folder)

    def take(self, event):
        return handle_callback(event, self.db, self.scope, -12345, 999)

    def test_first_employee_wins_and_second_cannot_replace(self):
        event = click(self.fingerprint)
        self.assertEqual(self.take(event), ("claimed", "cb-123"))
        next_person = click(self.fingerprint, employee=52)
        self.assertEqual(self.take(next_person)[0], "already_claimed")
        self.assertEqual(self.db.claimed_counts(self.scope), (1, 1))
        edit = self.db.reserve_claim_edit(self.scope)
        self.assertEqual(edit[2:], ("Обращение", "Анна Смирнова"))
        self.assertTrue(self.db.finish_claim_edit(edit[0], "done"))
        self.assertIsNone(self.db.reserve_claim_edit(self.scope))

    def test_forged_chat_message_and_bot_cannot_claim(self):
        for event in [
            click(self.fingerprint, mid="work-999"),
            click(self.fingerprint, chat=-999),
            click(self.fingerprint, bot=998),
            click("a" * 64),
            click(self.fingerprint, employee=0),
        ]:
            with self.subTest(event=event):
                self.assertNotEqual(self.take(event)[0], "claimed")
        self.assertEqual(self.db.claimed_counts(self.scope), (0, 0))

    def test_bot_pressed_is_ignored(self):
        event = click(self.fingerprint)
        event["callback"]["user"]["is_bot"] = True
        self.assertEqual(self.take(event), ("ignored", None))
        self.assertEqual(self.db.claimed_counts(self.scope), (0, 0))

    def test_reboot_resumes_idempotent_edit_without_reassigning(self):
        self.assertEqual(self.take(click(self.fingerprint))[0], "claimed")
        self.db.reserve_claim_edit(self.scope)
        self.db.close()
        self.db = InboxDB(self.folder)
        self.assertEqual(self.db.claimed_counts(self.scope), (1, 1))
        self.assertEqual(self.take(click(self.fingerprint, employee=55))[0],
                         "already_claimed")
        edit = self.db.reserve_claim_edit(self.scope)
        self.assertEqual(edit[1], "work-100")
        self.db.finish_claim_edit(edit[0], "done")
        self.assertEqual(self.db.claimed_counts(self.scope), (1, 0))

    def test_callback_marker_checkpoint_is_durable_and_monotonic(self):
        scope = self.db.callback_scope(999, -12345)
        self.assertIsNone(self.db.callback_marker(scope))
        self.db.save_callback_marker(scope, 30)
        self.db.close()
        self.db = InboxDB(self.folder)
        self.assertEqual(self.db.callback_marker(scope), 30)
        with self.assertRaises(ValueError):
            self.db.save_callback_marker(scope, 29)
        self.db.save_callback_marker(scope, 31)
        self.assertEqual(self.db.callback_marker(scope), 31)
        self.assertIsNone(self.db.callback_marker(
            self.db.callback_scope(999, -55555)
        ))


if __name__ == "__main__":
    unittest.main()
