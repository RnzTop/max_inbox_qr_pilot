"""Synthetic WebSocket frames; no live MAX session, secrets or Playwright."""
import ast
import json
import unittest
from pathlib import Path
from urllib.parse import urlsplit


SCRIPT = Path(__file__).resolve().parents[1] / "pilot.py"
tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
probe_cls = next(
    node for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name == "WebSocketIdProbe"
)
scope = {"json": json, "urlsplit": urlsplit}
exec(compile(ast.Module(body=[probe_cls], type_ignores=[]), str(SCRIPT), "exec"), scope)
WebSocketIdProbe = scope["WebSocketIdProbe"]


class FakePage:
    def __init__(self):
        self.events = {}

    def on(self, name, callback):
        self.events[name] = callback

    def connect(self, ws):
        self.events["websocket"](ws)


class FakeSocket:
    def __init__(self, url):
        self.url = url
        self.events = {}

    def on(self, name, callback):
        self.events[name] = callback

    def receive(self, payload):
        callback = self.events.get("framereceived")
        if callback:
            callback(payload)


class WebSocketProbeTests(unittest.TestCase):
    def test_observes_all_page_sockets_and_only_counts_max_shaped_frames(self):
        probe = WebSocketIdProbe()
        page = FakePage()
        probe.attach(page)
        irrelevant = FakeSocket("wss://unrelated.example/chat?token=SECRET_TOKEN")
        page.connect(irrelevant)
        irrelevant.receive(json.dumps({
            "payload": {"chatId": 1, "message": {"id": "SECRET_OTHER_ID"}}
        }))
        ws = FakeSocket("wss://ws-api.oneme.ru/websocket?key=SECRET_URL")
        page.connect(ws)
        ws.receive(json.dumps({
            "cmd": 1, "opcode": 19,
            "payload": {
                "chats": [{"id": 42, "lastMessage": {
                    "id": "SECRET_FIRST_ID", "text": "SECRET_CUSTOMER_TEXT"}}],
                "token": "SECRET_LOGIN_TOKEN"
            }
        }))
        ws.receive(json.dumps({
            "cmd": 1, "opcode": 49,
            "payload": {"chatId": 42, "messages": [
                {"id": "SECRET_SECOND_ID", "text": "SECRET_CUSTOMER_TEXT"}
            ]}
        }))
        ws.receive(json.dumps({
            "cmd": 0, "opcode": 128,
            "payload": {"chatId": 42, "message": {
                "id": "SECRET_THIRD_ID", "text": "SECRET_CUSTOMER_TEXT"}}
        }))
        summary = probe.summary()
        self.assertIn("сокетов=2", summary)
        self.assertIn("наблюдаемых=2", summary)
        self.assertIn("адреса oneme=1, max=0, другие=1", summary)
        self.assertIn("кадров=4", summary)
        self.assertIn("JSON=4", summary)
        self.assertIn("протокол MAX=3", summary)
        self.assertIn("чатов с id=1", summary)
        self.assertIn("сообщений с id=3", summary)
        self.assertIn("кадров с chatId=2", summary)
        self.assertIn("пар chat+message=3", summary)
        self.assertNotIn("SECRET", summary)
        self.assertNotIn("42", summary)

    def test_bytes_json_invalid_and_no_values_leaked(self):
        probe = WebSocketIdProbe()
        ws = FakeSocket("wss://ws-api.oneme.ru/websocket")
        probe._on_socket(ws)
        ws.receive(b"\xff\xfe\x81")
        ws.receive(b'{"payload":{"message":{"id":"secret"}}}')
        ws.receive("not json with SECRET_URL")
        ws.receive(None)
        self.assertIn("кадров=4", probe.summary())
        self.assertIn("двоичных=2", probe.summary())
        self.assertIn("JSON=1", probe.summary())
        self.assertIn("не-JSON=1", probe.summary())
        self.assertIn("слишком больших=1", probe.summary())
        self.assertNotIn("secret", probe.summary())

    def test_protocol_field_locations_need_parent_chat_for_pair(self):
        probe = WebSocketIdProbe()
        probe._on_frame(json.dumps({
            "cmd": 1, "opcode": 49, "payload": {"messages": [{"id": 1}]}
        }))
        self.assertIn("сообщений с id=1", probe.summary())
        self.assertIn("пар chat+message=0", probe.summary())
        # Request/response matching by seq would need separate handling:
        # We must not invent a chat_id for this response.

    def test_unknown_host_can_be_real_web_client_protocol(self):
        probe = WebSocketIdProbe()
        page = FakePage()
        probe.attach(page)
        socket = FakeSocket("wss://new-websocket.example.test/socket?token=SECRET")
        page.connect(socket)
        socket.receive(json.dumps({
            "cmd": 0, "opcode": 128,
            "payload": {"chatId": 123, "message": {
                "id": "SECRET_MESSAGE_ID", "text": "SECRET_BODY"
            }}
        }))
        summary = probe.summary()
        self.assertIn("адреса oneme=0, max=0, другие=1", summary)
        self.assertIn("кадров=1", summary)
        self.assertIn("протокол MAX=1", summary)
        self.assertIn("пар chat+message=1", summary)
        self.assertNotIn("SECRET", summary)
        self.assertNotIn("123", summary)

    def test_does_not_interfere_with_page_handlers(self):
        probe = WebSocketIdProbe()
        page = FakePage()
        probe.attach(page)
        self.assertEqual(set(page.events), {"websocket"})


if __name__ == "__main__":
    unittest.main()
