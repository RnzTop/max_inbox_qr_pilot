"""End-to-end handler simulation: one DM -> one private group notification."""
import ast
import asyncio
import logging
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bot_api import BotApiError
from inbox_db import InboxDB
from processor import _timestamp_ms, enqueue_personal_message

code = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
serve_ast = next(
    obj for obj in code.body
    if isinstance(obj, ast.AsyncFunctionDef) and obj.name == "serve"
)


class FakeState:
    def __init__(self):
        self.messages = []
        self.sent = 0
        self.received = 0

    def set_status(self, status):
        self.messages.append(status)

    def set_qr(self, qr):
        pass

    def record(self, field, n=1):
        setattr(self, field, getattr(self, field, 0) + n)

    def set_counts(self, db, scope):
        self.sent = db.counts(scope)["sent"]


class FakeBot:
    published = []

    def __init__(self, token, chat_id):
        self.chat_id = int(chat_id)
        self.bot_id = None
        assert token == "test_token_ignored"

    def preflight(self):
        self.bot_id = 999

    def send_card(self, text, payload):
        assert payload.startswith("take:") and len(payload) == 69
        type(self).published.append((self.chat_id, text, payload))
        return "work-mid-1"

    def get_updates(self, marker=None):
        return [], 1

    def acknowledge(self, callback_id, note):
        pass

    def edit_claimed(self, message_id, body, employee_name):
        pass



class FakeClient:
    def __init__(self, **kwargs):
        self.chats = [
            SimpleNamespace(id=42, is_dialog=True, last_event_time=0),
            SimpleNamespace(id=43, is_dialog=False, last_event_time=0),
        ]
        self.me = SimpleNamespace(contact=SimpleNamespace(id=100))
        self.start_cb = self.message_cb = self.disconnect_cb = None

    def on_start(self):
        def deco(func):
            self.start_cb = func
            return func
        return deco

    def on_message(self):
        def deco(func):
            self.message_cb = func
            return func
        return deco

    def on_disconnect(self):
        def deco(func):
            self.disconnect_cb = func
            return func
        return deco

    async def fetch_chats(self, marker=None):
        return []

    async def get_user(self, sender_id):
        return SimpleNamespace(names=[SimpleNamespace(name="Клиент")])

    async def start(self):
        await self.start_cb(self)
        now = int(time.time() * 1000)
        def m(chat, mid, sender):
            return SimpleNamespace(
                chat_id=chat, id=mid, sender=sender,
                text="Запишите на ремонт", time=now, attaches=[],
            )
        await self.message_cb(m(43, 12, 55), self)
        await self.message_cb(m(42, 13, 100), self)
        await self.message_cb(m(42, 14, 55), self)
        await self.message_cb(m(42, 14, 55), self)
        await asyncio.sleep(0.2)


class LiveRelayTests(unittest.TestCase):
    def test_private_live_message_is_forwarded_once(self):
        FakeBot.published.clear()
        with tempfile.TemporaryDirectory() as tmp:
            state = FakeState()

            async def fake_recover(*args, **kwargs):
                return {"complete": True, "new": 0, "error": ""}

            namespace = {
                "asyncio": asyncio,
                "DATA": Path(tmp),
                "logging": logging,
                "MaxBotAPI": FakeBot,
                "BotApiError": BotApiError,
                "InboxDB": InboxDB,
                "WebClient": FakeClient,
                "IngressQr": lambda: None,
                "ExtraConfig": lambda **opts: opts,
                "STATE": state,
                "SCAN_INTERVAL_SECONDS": 60,
                "_timestamp_ms": _timestamp_ms,
                "enqueue_personal_message": enqueue_personal_message,
                "recover_chat": fake_recover,
            }
            exec(compile(
                ast.Module(body=[serve_ast], type_ignores=[]),
                "app.py", "exec",
            ), namespace)
            asyncio.run(namespace["serve"]("test_token_ignored", "-12345"))
            self.assertEqual(len(FakeBot.published), 1)
            self.assertEqual(FakeBot.published[0][0], -12345)
            self.assertIn("Запишите на ремонт", FakeBot.published[0][1])
            self.assertEqual(state.received, 1)
            self.assertEqual(state.sent, 1)


if __name__ == "__main__":
    unittest.main()
