"""Isolated test-only read-only MAX WebClient. No Bot API or forwarding."""
import asyncio
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import threading

import qrcode
from qrcode.image.svg import SvgPathImage
from pymax import ExtraConfig, WebClient

from id_store import SeenIds


DATA = Path("/data")
MARKER = re.compile(r"^MAX HA TEST [0-9]{4,16}$")
INGRESS_IP = "172.30.32.2"


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "Ожидаю настройки тестового аккаунта."
        self.qr_svg = b""
        self.unique = 0
        self.duplicates = 0

    def set_status(self, value):
        with self.lock:
            self.status = value

    def set_qr(self, value):
        with self.lock:
            self.qr_svg = value

    def get(self):
        with self.lock:
            return self.status, self.qr_svg, self.unique, self.duplicates

    def update_counts(self, store):
        with self.lock:
            self.unique = store.unique
            self.duplicates = store.duplicates


STATE = State()


class Panel(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.client_address[0] != INGRESS_IP:
            self.send_error(403)
            return
        path = self.path.split("?")[0]
        status, qr_svg, unique, duplicates = STATE.get()
        if path.endswith("/qr.svg"):
            if not qr_svg:
                self.send_error(404)
                return
            content_type = "image/svg+xml; charset=utf-8"
            body = qr_svg
        else:
            picture = '<img src="./qr.svg" alt="QR для входа в тестовый MAX">' if qr_svg else ""
            content_type = "text/html; charset=utf-8"
            body = (
                '<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<meta http-equiv="refresh" content="5">'
                '<h1>MAX — PyMax тест (без отправки)</h1>'
                f'<p>{escape(status)}</p>'
                f'<p>Уникальных меток за запуск: {unique}; повторов: {duplicates}</p>'
                '<p>Только тестовый номер. QR и данные сессии никому не пересылайте.</p>'
                f'{picture}</html>'
            ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "private, no-store, max-age=0")
        self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        # Never log URL parameters, QR tokens, remote addresses or usernames.
        pass


class IngressQr:
    """Never log the QR URL: treat it like a one-time bearer credential."""

    async def show_qr(self, qr_url: str) -> None:
        qr = qrcode.QRCode(border=4, box_size=8)
        qr.add_data(qr_url)
        qr.make(fit=True)
        image = qr.make_image(image_factory=SvgPathImage)
        buf = BytesIO()
        image.save(buf)
        STATE.set_qr(buf.getvalue())
        STATE.set_status("Отсканируйте QR из локальной панели тестовым аккаунтом MAX.")


def validate_test_options(options):
    if options.get("test_confirmed") is not True:
        return None
    dialog = options.get("test_dialog")
    account = options.get("test_account_label")
    if not isinstance(dialog, str) or not isinstance(account, str):
        return None
    if not dialog.strip() or not account.strip():
        return None
    return dialog.strip()


async def serve(dialog):
    store = SeenIds(DATA / "pymax_test")
    # WARNING is intentional: debug frame logs may contain account tokens.
    logging.getLogger("pymax").setLevel(logging.WARNING)
    client = WebClient(
        work_dir=str(DATA),
        session_name="pymax_session.db",
        extra_config=ExtraConfig(telemetry=False, log_level="WARNING", reconnect=True),
        qr_provider=IngressQr(),
    )
    target_chat_id = None
    own_user_id = None

    @client.on_start()
    async def started(connected):
        nonlocal target_chat_id, own_user_id
        # The first MAX sync may only contain recent chats. Never fuzzy-match.
        chats = connected.chats or []
        matches = [c for c in chats if c.is_dialog and c.title == dialog]
        target_chat_id = matches[0].id if len(matches) == 1 else None
        own_user_id = connected.me.contact.id if connected.me else None
        STATE.set_qr(b"")
        if target_chat_id is None:
            STATE.set_status(
                "Подключено к MAX, но тестовый диалог не выбран: "
                f"точных совпадений {len(matches)}. "
                "Выведите тестовый чат в последние диалоги и перезапустите."
            )
        else:
            STATE.set_status("Тестовый диалог найден. Ожидаю новую входящую метку MAX HA TEST.")

    @client.on_message()
    async def got_message(message, connected):
        if target_chat_id is None or message.chat_id != target_chat_id:
            return
        if own_user_id is not None and message.sender == own_user_id:
            return
        if not MARKER.fullmatch(message.text or ""):
            return
        store.observe(message.chat_id, message.id)
        STATE.update_counts(store)
        STATE.set_status(
            "Получена тестовая метка. ID проверен локально; сообщения не отправляются."
        )

    try:
        await client.start()
    except Exception:
        STATE.set_qr(b"")
        STATE.set_status("Подключение MAX остановилось. Проверьте тестовую сессию и логи дополнения.")
        # Do not print exception details: a library error can contain secrets.


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING)
    server = ThreadingHTTPServer(("0.0.0.0", 8099), Panel)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        settings = json.loads((DATA / "options.json").read_text())
    except (OSError, ValueError):
        STATE.set_status("Нет корректных параметров дополнения.")
        threading.Event().wait()
        return
    dialog = validate_test_options(settings)
    if dialog is None:
        STATE.set_status("Нужны test_confirmed, метка тестового аккаунта и точное имя тестового диалога.")
        threading.Event().wait()
        return
    asyncio.run(serve(dialog))
    threading.Event().wait()


if __name__ == "__main__":
    main()
