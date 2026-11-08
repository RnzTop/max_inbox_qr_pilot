"""Simulated live events prove safe exact-challenge pairing, no real MAX access."""
import ast
import asyncio
import logging
from pathlib import Path
import re
import secrets
import unittest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
code = (ROOT / "app.py").read_text()
parsed = ast.parse(code)
serve_fn = next(n for n in parsed.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "serve")


class MockState:
    def __init__(self):
        self.statuses = []
        self.unique = 0
        self.duplicates = 0

    def set_status(self, msg):
        self.statuses.append(msg)

    def set_qr(self, val):
        pass

    def update_counts(self, store):
        self.unique = store.unique
        self.duplicates = store.duplicates


class FakeStore:
    def __init__(self, directory):
        self.seen = set()
        self.unique = 0
        self.duplicates = 0

    def observe(self, chat, msg):
        p = (chat, msg)
        if p in self.seen:
            self.duplicates += 1
        else:
            self.unique += 1
            self.seen.add(p)


class FakeClient:
    def __init__(self, **kwargs):
        self.chats = [SimpleNamespace(id=42, is_dialog=True, title=None)]
        self.me = SimpleNamespace(contact=SimpleNamespace(id=100))
        self._started = self._disconnected = self._message = None
        self.updates = []

    def on_start(self):
        def deco(callback):
            self._started = callback
            return callback
        return deco

    def on_disconnect(self):
        def deco(callback):
            self._disconnected = callback
            return callback
        return deco

    def on_message(self):
        def deco(callback):
            self._message = callback
            return callback
        return deco

    async def start(self):
        await self._started(self)
        challenge = self.state.statuses[-1].split("точное сообщение: ", 1)[1]
        assert re.fullmatch(r"MAX HA PAIR [0-9A-F]{32}", challenge)
        msg = lambda chat, sender, text, mid: SimpleNamespace(
            chat_id=chat, sender=sender, text=text, id=mid,
        )
        # No text including a subset of challenge may bind the dialog.
        await self._message(msg(42, 200, challenge + " ", 10), self)
        await self._message(msg(42, 100, challenge, 11), self)
        await self._message(msg(42, 200, "MAX HA TEST 8361", 12), self)
        assert self.state.unique == 0
        # Only exact challenge may bind; no identifiers are printed.
        await self._message(msg(42, 200, challenge, 13), self)
        assert "диалог привязан" in self.state.statuses[-1].lower()
        # Other chats and malformed markers never count.
        await self._message(msg(43, 200, "MAX HA TEST 8361", 14), self)
        await self._message(msg(42, 200, "MAX HA TEST 8361!", 15), self)
        await self._message(msg(42, 100, "MAX HA TEST 8361", 16), self)
        assert self.state.unique == 0
        await self._message(msg(42, 200, "MAX HA TEST 8361", 17), self)
        await self._message(msg(42, 200, "MAX HA TEST 8361", 17), self)
        assert (self.state.unique, self.state.duplicates) == (1, 1)


class PairingTests(unittest.TestCase):
    def test_only_exact_challenge_binds_one_personal_chat(self):
        state = MockState()
        class BoundClient(FakeClient):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.state = state

        namespace = {
            "DATA": Path("/tmp"),
            "SeenIds": FakeStore,
            "WebClient": BoundClient,
            "ExtraConfig": lambda **kw: kw,
            "IngressQr": lambda: None,
            "MARKER": re.compile(r"^MAX HA TEST [0-9]{4,16}$"),
            "STATE": state,
            "logging": logging,
            "secrets": secrets,
        }
        exec(compile(ast.Module(body=[serve_fn], type_ignores=[]), "app.py", "exec"), namespace)
        asyncio.run(namespace["serve"]("Expected test name"))
        self.assertEqual((state.unique, state.duplicates), (1, 1))
        self.assertNotIn("MAX HA PAIR", state.statuses[-1])


if __name__ == "__main__":
    unittest.main()
