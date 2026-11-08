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
    paired = None

    def get_binding(self, owner, dialog):
        if type(self).paired and type(self).paired[:2] == (owner, dialog):
            return "paired-digest"
        return None

    def bind_chat(self, owner, dialog, chat):
        type(self).paired = (owner, dialog, chat)
        return "paired-digest"

    def is_bound_chat(self, owner, dialog, chat, digest):
        return digest == "paired-digest" and type(self).paired == (
            owner, dialog, chat
        )

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

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, is_dialog=(chat_id == 42))

    async def start(self):
        await self._started(self)
        msg = lambda chat, sender, text, mid: SimpleNamespace(
            chat_id=chat, sender=sender, text=text, id=mid,
        )
        if "Сохранённая привязка" in self.state.statuses[-1]:
            await self._message(msg(43, 200, "MAX HA TEST 8362", 17), self)
            await self._message(msg(42, 200, "MAX HA TEST 8362", 18), self)
            assert self.state.unique == 1
            return
        challenge = self.state.statuses[-1].split("точное сообщение: ", 1)[1]
        assert re.fullmatch(r"MAX HA PAIR [0-9A-F]{32}", challenge)
        # No text including a subset of challenge may bind the dialog.
        await self._message(msg(43, 200, challenge, 9), self)
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
        FakeStore.paired = None
        state = MockState()
        class BoundClient(FakeClient):
            current_state = None

            def __init__(self, **kw):
                super().__init__(**kw)
                self.state = type(self).current_state

        BoundClient.current_state = state

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
        # A new process restores the saved HMAC and never asks for pairing.
        state2 = MockState()
        BoundClient.current_state = state2
        namespace["STATE"] = state2
        exec(compile(ast.Module(body=[serve_fn], type_ignores=[]), "app.py", "exec"), namespace)
        asyncio.run(namespace["serve"]("Expected test name"))
        self.assertEqual(state2.unique, 1)
        self.assertNotIn("MAX HA PAIR", " ".join(state2.statuses))


if __name__ == "__main__":
    unittest.main()
