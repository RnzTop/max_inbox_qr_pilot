"""Synthetic end-to-end tests for ID stability; no real MAX credentials."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ws_binary_probe import scan_binary_frame
from ws_id_stability import IdStability
from test_ws_binary_probe import frame, pack


class StabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ws-stability-"))
        self.store = IdStability(self.tmp)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp)

    def _decode(self, obj, key=None):
        status, payload = scan_binary_frame(
            frame(pack(obj)), fingerprint_key=(key if key is not None else self.store.key)
        )
        self.assertEqual(status, "msgpack")
        return payload

    def test_id_fingerprints_hide_raw_ids_and_are_canonical(self):
        a = self._decode({
            "chatId": 12345678901234,
            "message": {"id": 88888888888, "text": "SECRET_CUSTOMER",
                        "token": "SECRET_ACCOUNT_TOKEN"}
        })
        b = self._decode({"chatId": "12345678901234", "message": {"id": "88888888888"}})
        self.assertEqual(a, b)
        self.assertEqual(len(a["chatId"]), 64)
        self.assertEqual(len(a["message"]["id"]), 64)
        self.assertNotIn("12345678", str(a))
        self.assertNotIn("88888888", str(a))
        self.assertNotIn("SECRET", str(a))
        wrong = self._decode({"chatId": 12345678901235, "message": {"id": 88888888888}})
        self.assertNotEqual(wrong["chatId"], a["chatId"])

    def test_persistent_match_after_restart(self):
        push = self._decode({"chatId": 192, "message": {"id": 7887, "text": "SECRET"}})
        self.store.push(push["chatId"], push["message"]["id"])
        self.assertIn("сохранено отпечатков=1", self.store.summary())
        same_session_history = self._decode({"messages": [{"id": 7887}]})
        self.store.history(push["chatId"], [same_session_history["messages"][0]["id"]])
        self.assertIn("совпадений после перезапуска=0", self.store.summary())
        self.assertIn("совпадений в этой сессии=1", self.store.summary())
        old_key = self.store.key
        self.store.close()
        next_session = IdStability(self.tmp)
        try:
            self.assertEqual(next_session.key, old_key)
            next_session.history(
                push["chatId"], [same_session_history["messages"][0]["id"]]
            )
            self.assertIn("совпадений после перезапуска=1", next_session.summary())
            self.assertIn("подтверждено всего=1", next_session.summary())
            next_session.history(
                push["chatId"], [same_session_history["messages"][0]["id"]]
            )
            self.assertIn("подтверждено всего=1", next_session.summary())
        finally:
            next_session.close()
        self.store = IdStability(self.tmp)

        db = (self.tmp / "fingerprints.sqlite3").read_bytes()
        self.assertNotIn(b"SECRET", db)
        self.assertNotIn(b"7887", db)
        self.assertNotIn(b"192", db)

    def test_different_chat_or_message_cannot_match(self):
        original = self._decode({"chatId": 12, "message": {"id": 30}})
        self.store.push(original["chatId"], original["message"]["id"])
        diff_msg = self._decode({"chatId": 12, "message": {"id": 31}})
        self.store.history(original["chatId"], [diff_msg["message"]["id"]])
        diff_chat = self._decode({"chatId": 13})
        self.store.history(diff_chat["chatId"], [original["message"]["id"]])
        self.assertIn("подтверждено всего=0", self.store.summary())
        self.store.push("not a fingerprint", "also wrong")
        self.assertIn("сохранено отпечатков=1", self.store.summary())

    def test_hmac_depends_on_random_key(self):
        payload = frame(pack({"chatId": 234, "message": {"id": 987}}))
        a = scan_binary_frame(payload, fingerprint_key=self.store.key)[1]
        b = scan_binary_frame(payload, fingerprint_key=b"X" * 32)[1]
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
