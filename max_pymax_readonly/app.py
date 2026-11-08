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
import secrets
import threading

import qrcode
from qrcode.image.svg import SvgPathImage
from pymax import ExtraConfig, WebClient

from id_store import SeenIds
from history_recovery import recover_history


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
        self.total = 0
        self.recovered = 0
        self.disconnects = 0
        self.recovery_errors = 0
        self.history_pending = False

    def set_status(self, value):
        with self.lock:
            self.status = value

    def set_qr(self, value):
        with self.lock:
            self.qr_svg = value

    def get(self):
        with self.lock:
            return (self.status, self.qr_svg, self.unique, self.duplicates,
                    self.total, self.recovered, self.disconnects,
                    self.recovery_errors, self.history_pending)

    def record_disconnect(self):
        with self.lock:
            self.disconnects += 1

    def record_recovery(self, result):
        with self.lock:
            self.history_pending = not result.complete
            if result.error:
                self.recovery_errors += 1

    def add_recovered(self, count):
        with self.lock:
            self.recovered += count

    def update_counts(self, store):
        with self.lock:
            self.unique = store.unique
            self.duplicates = store.duplicates
            self.total = store.count()


STATE = State()


class Panel(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.client_address[0] != INGRESS_IP:
            self.send_error(403)
            return
        path = self.path.split("?")[0]
        (status, qr_svg, unique, duplicates, total, recovered,
         disconnects, recovery_errors, pending) = STATE.get()
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
                f'<p>Уникальных меток за запуск: {unique}; повторов: {duplicates}; '
                f'уникальных за всё время: {total}</p>'
                f'<p>Восстановлено из истории за запуск: {recovered}</p>'
                f'<p>Обрывов MAX за запуск: {disconnects}; ошибок истории: {recovery_errors}; '
                f'дочитывание истории: {"требуется" if pending else "завершено"}</p>'
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
    STATE.update_counts(store)
    # WARNING is intentional: debug frame logs may contain account tokens.
    logging.getLogger("pymax").setLevel(logging.WARNING)
    client = WebClient(
        work_dir=str(DATA),
        session_name="pymax_session.db",
        extra_config=ExtraConfig(
            telemetry=False, log_level="WARNING", reconnect=True,
            reconnect_delay=10.0, relogin=False, password_max_attempts=0,
        ),
        qr_provider=IngressQr(),
    )
    saved_chat_mac = None
    own_user_id = None
    recovery_chat_id = None  # memory-only; never persists a raw MAX chat ID
    recovery_lock = asyncio.Lock()
    recovery_task = None
    RECOVERY_INTERVAL = 60
    # Private 128-bit challenge scoped to this process. Never saved or logged.
    pairing_marker = "MAX HA PAIR " + secrets.token_hex(16).upper()

    async def find_bound_chat(connected):
        if saved_chat_mac is None or own_user_id is None:
            return None
        cached = connected.chats or []
        candidates = [
            c for c in cached if getattr(c, "is_dialog", False)
        ]
        found = next(
            (c.id for c in candidates if store.is_bound_chat(
                own_user_id, dialog, c.id, saved_chat_mac
            )), None
        )
        if found is not None:
            return found
        try:
            additional = await asyncio.wait_for(
                connected.fetch_chats(), timeout=20
            )
        except Exception:
            return None
        return next(
            (c.id for c in additional if getattr(c, "is_dialog", False)
             and store.is_bound_chat(
                 own_user_id, dialog, c.id, saved_chat_mac
             )), None
        )

    async def background_recovery(connected):
        nonlocal recovery_chat_id
        try:
            while True:
                await asyncio.sleep(RECOVERY_INTERVAL)
                if own_user_id is None or saved_chat_mac is None:
                    continue
                if recovery_chat_id is None:
                    recovery_chat_id = await find_bound_chat(connected)
                if recovery_chat_id is not None:
                    await run_recovery(connected, recovery_chat_id)
        except asyncio.CancelledError:
            return

    def start_background_recovery(connected):
        nonlocal recovery_task
        if recovery_task is not None:
            recovery_task.cancel()
        recovery_task = asyncio.create_task(background_recovery(connected))

    async def run_recovery(connected, chat_id):
        async with recovery_lock:
            try:
                result = await recover_history(
                    connected, store, own_user_id, dialog, chat_id
                )
            except Exception:
                # Exception payloads from unofficial clients may contain secrets.
                STATE.record_recovery(type("Failure", (), {
                    "complete": False, "error": "internal"
                })())
                STATE.set_status(
                    "Временная ошибка восстановления истории. "
                    "Попробую повторить проверку автоматически."
                )
                return
            STATE.record_recovery(result)
            STATE.add_recovered(result.recovered)
            STATE.update_counts(store)
            if result.initialized:
                STATE.set_status(
                    "Начальная точка истории сохранена. Следующая "
                    "перезагрузка сможет восстановить пропущенные метки."
                )
            elif result.complete:
                STATE.set_status(
                    "История тестового чата проверена. "
                    f"Новых меток из истории: {result.recovered}. "
                    "Ожидаю входящую MAX HA TEST."
                )
            else:
                STATE.set_status(
                    "История тестового чата проверена частично "
                    f"({result.scanned} сообщений). "
                    "Промежуточная позиция сохранена. Попробую "
                    "продолжить восстановление автоматически."
                )

    @client.on_start()
    async def started(connected):
        nonlocal saved_chat_mac, own_user_id, recovery_chat_id
        # A personal chat often has no title in PyMax. Prefer the previously
        # authenticated HMAC binding, scoped to this account and dialog setting.
        profile = getattr(connected, "me", None)
        contact = getattr(profile, "contact", None)
        own_user_id = getattr(contact, "id", None)
        STATE.set_qr(b"")
        saved_chat_mac = None
        recovery_chat_id = None
        if own_user_id is None:
            STATE.set_status("Подключено к MAX, но ID тестового аккаунта не определён. Приём отключён.")
            return
        saved_chat_mac = store.get_binding(own_user_id, dialog)
        if saved_chat_mac is not None:
            STATE.set_status(
                "MAX подключён. Привязка тестового диалога восстановлена. "
                "Проверяю пропущенные метки."
            )
            start_background_recovery(connected)
            matching = await find_bound_chat(connected)
            if matching is not None:
                recovery_chat_id = matching
                await run_recovery(connected, matching)
            else:
                STATE.set_status(
                    "Привязка тестового диалога восстановлена, "
                    "но чат не виден в списке MAX. Продолжу поиск "
                    "в фоне и при следующем входящем сообщении."
                )
            return
        chats = connected.chats or []
        matches = [c for c in chats if c.is_dialog and c.title == dialog]
        if len(matches) == 1:
            saved_chat_mac = store.bind_chat(own_user_id, dialog, matches[0].id)
            recovery_chat_id = matches[0].id
            start_background_recovery(connected)
            await run_recovery(connected, recovery_chat_id)
            STATE.set_status(
                "Тестовый диалог найден и привязан. Ожидаю MAX HA TEST."
            )
            return
        STATE.set_status(
            "MAX подключён, тестовый диалог не выбран "
            f"(точных совпадений {len(matches)}). "
            "Отправьте из другого тестового аккаунта в нужный личный диалог "
            "точное сообщение: " + pairing_marker
        )

    @client.on_disconnect()
    async def disconnected(error, reconnect, delay):
        nonlocal saved_chat_mac, own_user_id, recovery_chat_id, recovery_task
        STATE.record_disconnect()
        if recovery_task is not None:
            recovery_task.cancel()
            recovery_task = None
        saved_chat_mac = None
        own_user_id = None
        recovery_chat_id = None
        STATE.set_qr(b"")
        STATE.set_status(
            "Соединение с MAX потеряно. Ожидаю переподключения."
            if reconnect else "Соединение с MAX остановлено."
        )

    @client.on_message()
    async def got_message(message, connected):
        nonlocal saved_chat_mac, recovery_chat_id
        # Fail closed until the logged-in test account identity is verified.
        if own_user_id is None or message.chat_id is None:
            return
        if message.sender is None or message.sender == own_user_id:
            return
        if saved_chat_mac is None:
            # The private one-time challenge can bind only a verified personal
            # chat. No raw chat IDs, contact names or messages are persisted.
            if message.text != pairing_marker:
                return
            try:
                chat = next(
                    (c for c in (connected.chats or [])
                     if c.id == message.chat_id), None
                )
                if chat is None:
                    chat = await connected.get_chat(message.chat_id)
            except Exception:
                STATE.set_status(
                    "Не удалось проверить тип тестового диалога. "
                    "Привязка не выполнена. Повторите метку позже."
                )
                return
            if not getattr(chat, "is_dialog", False):
                return
            saved_chat_mac = store.bind_chat(
                own_user_id, dialog, message.chat_id
            )
            recovery_chat_id = message.chat_id
            start_background_recovery(connected)
            await run_recovery(connected, recovery_chat_id)
            STATE.set_status(
                "Тестовый диалог привязан и сохранён. Отправьте новую "
                "метку MAX HA TEST 8362."
            )
            return
        if not store.is_bound_chat(
            own_user_id, dialog, message.chat_id, saved_chat_mac
        ):
            return
        if recovery_chat_id is None:
            # Login/sync did not contain the chat, but this verified incoming
            # event safely resolves its ID for the pending read-only catch-up.
            recovery_chat_id = message.chat_id
            start_background_recovery(connected)
            await run_recovery(connected, recovery_chat_id)
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
        STATE.set_status(
            "Подключение MAX остановилось. Возможно, требуется повторный QR "
            "или пароль 2FA (в этом тесте не поддерживается). "
            "Никакие данные переписки не записаны в журнал."
        )
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
