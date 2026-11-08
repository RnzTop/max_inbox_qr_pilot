"""Synthetic WebSocket frames; no live MAX session, secrets or Playwright."""
import ast
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit


SCRIPT = Path(__file__).resolve().parents[1] / "pilot.py"
tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
probe_cls = next(
    node for node in tree.body
    if isinstance(node, ast.ClassDef) and node.name == "WebSocketIdProbe"
)
sys.path.insert(0, str(SCRIPT.parent))
from ws_binary_probe import frame_meta, scan_binary_frame
from ws_id_stability import IdStability
from test_ws_binary_probe import pack, frame

scope = {"json": json, "urlsplit": urlsplit, "scan_binary_frame": scan_binary_frame, "frame_meta": frame_meta}
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

    def send(self, payload):
        callback = self.events.get("framesent")
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
        ws.receive('{"cmd":0,"opcode":128,"payload":{"chatId":1,"message":{"id":"secret"}}}')
        ws.receive("not json with SECRET_URL")
        ws.receive(None)
        self.assertIn("кадров=4", probe.summary())
        self.assertIn("двоичных=1;", probe.summary())
        self.assertIn(" JSON=1;", probe.summary())
        self.assertIn("протокол MAX=1;", probe.summary())
        self.assertIn("не-JSON=1;", probe.summary())
        self.assertIn("неизвестный заголовок=1;", probe.summary())
        self.assertIn("слишком больших=1;", probe.summary())
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

    def test_classifies_live_history_and_login_without_extracting_ids(self):
        probe = WebSocketIdProbe()
        page = FakePage()
        probe.attach(page)
        ws = FakeSocket("wss://client.oneme.ru/websocket")
        page.connect(ws)

        # This binary history request contains a chatId, but we retain only
        # its packet seq number as correlation state, not the chatId value.
        request = frame(pack({"chatId": 811, "getMessages": 1}),
                        opcode=49)
        ws.send(request)
        self.assertEqual(len(probe._history_pending), 1)
        self.assertTrue(all(x is True for x in probe._history_pending.values()))

        # Response with the same seq, even without a chatId of its own.
        history = bytearray(frame(pack({
            "messages": [{"id": 833, "text": "SECRET_PRIVATE_TEXT"}]
        }), opcode=49))
        history[1] = 1
        ws.receive(bytes(history))

        login = bytearray(frame(pack({
            "chats": [{"id": 881, "lastMessage": {"id": 882}}]
        }), opcode=19))
        login[1] = 1
        ws.receive(bytes(login))

        ws.receive(frame(pack({
            "chatId": 991, "message": {"id": 992, "text": "SECRET_TEXT"}
        }), opcode=128))

        summary = probe.summary()
        self.assertIn("история запросов=1, ответов=1, связано по seq=1, message.id=1", summary)
        self.assertIn("LOGIN кадров=1, чатов=1", summary)
        self.assertIn("новых push кадров=1, message.id=1, с chatId=1", summary)
        self.assertEqual(probe._history_pending, {})
        self.assertNotIn("SECRET", summary)
        self.assertNotIn("991", summary)

    def test_message_id_in_history_is_not_live_event(self):
        probe = WebSocketIdProbe()
        ws = FakeSocket("wss://service.oneme.ru/socket")
        probe._on_socket(ws)
        binary = bytearray(frame(pack({
            "messages": [{"id": 555}]
        }), opcode=49))
        binary[1] = 1
        ws.receive(bytes(binary))
        summary = probe.summary()
        self.assertIn("история запросов=0, ответов=1, связано по seq=0, message.id=1", summary)
        self.assertIn("новых push кадров=0, message.id=0, с chatId=0", summary)

    def test_websocket_push_then_history_after_restart_matches(self):
        from pathlib import Path
        folder = Path(tempfile.mkdtemp(prefix="max-end-to-end-"))
        first_store = IdStability(folder)
        try:
            page = FakePage()
            first_probe = WebSocketIdProbe(stability=first_store)
            first_probe.attach(page)
            ws = FakeSocket("wss://client.oneme.ru/ws")
            page.connect(ws)
            ws.receive(frame(pack({
                "chatId": 888,
                "message": {"id": 999, "text": "DO_NOT_STORE_CUSTOMER_TEXT"}
            }), opcode=128))
            self.assertIn("сохранено отпечатков=1", first_probe.summary())
            first_store.close()
            second_store = IdStability(folder)
            try:
                page2 = FakePage()
                second_probe = WebSocketIdProbe(stability=second_store)
                second_probe.attach(page2)
                ws2 = FakeSocket("wss://client.oneme.ru/ws")
                page2.connect(ws2)
                ws2.send(frame(pack({"chatId": 888}), opcode=49))
                incoming = bytearray(frame(pack({
                    "messages": [{"id": 999, "text": "DO_NOT_STORE_CUSTOMER_TEXT"}]
                }), opcode=49))
                incoming[1] = 1
                ws2.receive(bytes(incoming))
                result = second_probe.summary()
                self.assertIn("связано по seq=1", result)
                self.assertIn("совпадений после перезапуска=1", result)
                self.assertNotIn("DO_NOT_STORE", result)
                second_store.close()
            except Exception:
                second_store.close()
                raise
        finally:
            shutil.rmtree(folder)

    def test_does_not_interfere_with_page_handlers(self):
        probe = WebSocketIdProbe()
        page = FakePage()
        probe.attach(page)
        self.assertEqual(set(page.events), {"websocket"})


if __name__ == "__main__":
    unittest.main()
