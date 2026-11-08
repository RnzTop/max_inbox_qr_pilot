"""Isolated MAX personal-inbox relay: PyMax QR receive -> Bot API group post.

This add-on never consumes bot updates or mutates Home Assistant state.
The working bot's webhook / polling / token ownership are unaffected.
"""
import asyncio
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import threading
import time

import qrcode
from qrcode.image.svg import SvgPathImage
from pymax import ExtraConfig, WebClient

from bot_api import BotApiError, MaxBotAPI, parse_chat_id
from inbox_db import InboxDB
from processor import _timestamp_ms, enqueue_personal_message, recover_chat


DATA = Path("/data")
INGRESS_IP = "172.30.32.2"
SCAN_INTERVAL_SECONDS = 60


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status = "Введите токен бота и ID рабочего чата в настройках."
        self.qr = b""
        self.sent = self.queued = self.uncertain = self.failed = 0
        self.received = self.restored = self.disconnects = 0
        self.errors = 0

    def set_status(self, value):
        with self.lock:
            self.status = value

    def set_qr(self, svg):
        with self.lock:
            self.qr = svg

    def record(self, field, count=1):
        with self.lock:
            setattr(self, field, getattr(self, field) + count)

    def set_counts(self, db, scope):
        c = db.counts(scope)
        with self.lock:
            self.sent, self.queued = c["sent"], c["queued"]
            self.uncertain, self.failed = c["uncertain"], c["failed"]

    def snapshot(self):
        with self.lock:
            return dict(status=self.status, qr=self.qr, sent=self.sent,
                        queued=self.queued, uncertain=self.uncertain,
                        failed=self.failed, received=self.received,
                        restored=self.restored, disconnects=self.disconnects,
                        errors=self.errors)


STATE = State()


class Panel(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.client_address[0] != INGRESS_IP:
            self.send_error(403)
            return
        path = self.path.split("?")[0]
        state = STATE.snapshot()
        if path.endswith("/qr.svg"):
            if not state["qr"]:
                self.send_error(404)
                return
            typ = "image/svg+xml; charset=utf-8"
            payload = state["qr"]
        else:
            typ = "text/html; charset=utf-8"
            image = ('<img src="./qr.svg" alt="QR авторизации MAX">'
                     if state["qr"] else "")
            payload = (
                '<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<meta http-equiv="refresh" content="5">'
                '<title>MAX — Входящие обращения</title>'
                '<h1>MAX — Входящие обращения (PyMax)</h1>'
                f'<p>{escape(state["status"])}</p>'
                f'<p>Отправлено в рабочий чат: {state["sent"]}; '
                f'в очереди: {state["queued"]}; '
                f'неопределённых отправок: {state["uncertain"]}; '
                f'ошибок отправки: {state["failed"]}</p>'
                f'<p>Получено за запуск: {state["received"]}; '
                f'восстановлено: {state["restored"]}; '
                f'обрывов MAX: {state["disconnects"]}; '
                f'ошибок: {state["errors"]}</p>'
                '<p>Содержимое обращений хранится локально в /data. '
                'Данные в Home Assistant не передаются. '
                'QR и токены не пересылайте.</p>'
                f'{image}</html>'
            ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Cache-Control", "private, no-store, max-age=0")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        # No URL, QR, IP, token or customer data in access logs.
        pass


class IngressQr:
    async def show_qr(self, qr_url):
        qr = qrcode.QRCode(border=4, box_size=8)
        qr.add_data(qr_url)
        qr.make(fit=True)
        buf = BytesIO()
        qr.make_image(image_factory=SvgPathImage).save(buf)
        STATE.set_qr(buf.getvalue())
        STATE.set_status(
            "Проверка рабочего чата успешна. Отсканируйте QR нужным аккаунтом MAX."
        )


def read_settings():
    try:
        options = json.loads((DATA / "options.json").read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise ValueError("Нет настроек дополнения") from None
    token = options.get("bot_token")
    chat_id = options.get("work_chat_id")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Укажите токен бота MAX")
    return token, str(parse_chat_id(chat_id))


async def serve(token, chat_id):
    bot = MaxBotAPI(token, chat_id)
    try:
        await asyncio.to_thread(bot.preflight)
    except (BotApiError, ValueError):
        STATE.set_status(
            "Не удалось проверить токен бота или закрытый рабочий чат. "
            "Проверьте права бота и настройки. Отправка не запущена."
        )
        return

    store = InboxDB(DATA / "inbox")
    logging.getLogger("pymax").setLevel(logging.WARNING)
    client = WebClient(
        work_dir=str(DATA), session_name="production_session.db",
        qr_provider=IngressQr(),
        extra_config=ExtraConfig(
            telemetry=False, log_level="WARNING", reconnect=True,
            reconnect_delay=10.0, relogin=False, password_max_attempts=0,
        ),
    )
    own_id = None
    scope = None
    sender_task = None
    scanner_task = None
    wake_sender = asyncio.Event()
    scan_lock = asyncio.Lock()
    name_cache = {}
    chat_cache = {}
    generation = 0

    async def is_personal(connected, chat_id):
        if chat_id in chat_cache:
            return chat_cache[chat_id]
        chat = next((c for c in connected.chats or []
                     if getattr(c, "id", None) == chat_id), None)
        if chat is None:
            try:
                chat = await asyncio.wait_for(
                    connected.get_chat(chat_id), timeout=12
                )
            except Exception:
                return False
        ok = bool(getattr(chat, "is_dialog", False))
        if len(chat_cache) >= 1000:
            chat_cache.clear()
        chat_cache[chat_id] = ok
        return ok

    async def sender_worker(my_scope, my_generation):
        while my_generation == generation:
            notice = store.reserve(my_scope)
            if notice is None:
                wake_sender.clear()
                try:
                    await asyncio.wait_for(wake_sender.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                continue
            fingerprint, body = notice
            try:
                mid = await asyncio.to_thread(bot.send_text, body)
            except BotApiError as exc:
                if exc.kind == "rate_limited":
                    store.finish(fingerprint, "queued")
                    await asyncio.sleep(10)
                    continue
                if exc.kind in ("forbidden", "rejected", "invalid_body"):
                    store.finish(fingerprint, "failed")
                else:
                    store.finish(fingerprint, "uncertain")
                STATE.record("errors")
                STATE.set_status(
                    "Ошибка доставки обращения в рабочий чат. "
                    "Проверьте настройки и счётчики; неоднозначные "
                    "отправки автоматически не повторяются."
                )
            except Exception:
                # Never log exception text, which may contain sensitive data.
                store.finish(fingerprint, "uncertain")
                STATE.record("errors")
                STATE.set_status(
                    "Неизвестен результат отправки. Автоповтор отключён "
                    "во избежание двойного уведомления."
                )
            else:
                store.finish(fingerprint, "sent", mid)
                STATE.set_status("Обращение доставлено в рабочий чат MAX.")
            STATE.set_counts(store, my_scope)
            await asyncio.sleep(1.2)

    async def list_recent_dialogs(connected):
        # MAX returns recent chats sorted by activity; scan up to 500 chats.
        # If list pagination cannot be verified, do not claim exhaustive sync.
        results = {
            c.id: c for c in (connected.chats or [])
            if getattr(c, "is_dialog", False)
        }
        marker = None
        complete = False
        for i in range(10):
            try:
                page = await asyncio.wait_for(
                    connected.fetch_chats(marker=marker), timeout=20
                )
            except Exception:
                return list(results.values()), False
            if not page:
                complete = True
                break
            for c in page:
                if getattr(c, "is_dialog", False):
                    results[c.id] = c
            timestamps = [
                getattr(c, "last_event_time", 0)
                for c in page
                if type(getattr(c, "last_event_time", None)) is int
                and getattr(c, "last_event_time", 0) > 0
            ]
            if not timestamps:
                break
            oldest = min(timestamps)
            if marker is not None and oldest >= marker:
                break
            marker = oldest
            if oldest <= store.baseline(scope) - 5000:
                complete = True
                break
            if len(page) < 50:
                complete = True
                break
        return list(results.values()), complete

    async def scanner_worker(connected, my_scope, my_generation):
        nonlocal scope
        while my_generation == generation:
            async with scan_lock:
                chats, complete_list = await list_recent_dialogs(connected)
                partial = not complete_list
                recovered_this_pass = 0
                for chat in chats[:500]:
                    if my_generation != generation:
                        return
                    try:
                        result = await recover_chat(
                            connected, store, my_scope, own_id,
                            chat.id, name_cache
                        )
                    except Exception:
                        partial = True
                        STATE.record("errors")
                        continue
                    recovered_this_pass += result["new"]
                    if not result["complete"]:
                        partial = True
                        if result["error"] != "page_limit":
                            STATE.record("errors")
                if len(chats) > 500:
                    partial = True
                STATE.record("restored", recovered_this_pass)
                STATE.set_counts(store, my_scope)
                if recovered_this_pass:
                    wake_sender.set()
                if partial:
                    STATE.set_status(
                        "История проверена частично. Дочитывание продолжится "
                        "автоматически; сообщения не теряются из уже "
                        "сохранённой очереди."
                    )
                else:
                    STATE.set_status(
                        "MAX подключён. Входящие личные диалоги проверены. "
                        "Ожидаю обращения."
                    )
                store.redact_old_sent_bodies()
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    @client.on_start()
    async def started(connected):
        nonlocal own_id, scope, sender_task, scanner_task, generation
        generation += 1
        my_generation = generation
        if sender_task:
            sender_task.cancel()
        if scanner_task:
            scanner_task.cancel()
        STATE.set_qr(b"")
        me = getattr(getattr(connected, "me", None), "contact", None)
        own_id = getattr(me, "id", None)
        if own_id is None:
            STATE.set_status("MAX подключён, но ID аккаунта не определён.")
            return
        scope = store.scope(own_id, bot.bot_id, bot.chat_id)
        store.recover_inflight()
        STATE.set_counts(store, scope)
        STATE.set_status(
            "MAX авторизован. Запущен приём личных сообщений "
            "и отправка уведомлений в рабочий чат."
        )
        sender_task = asyncio.create_task(sender_worker(scope, my_generation))
        scanner_task = asyncio.create_task(scanner_worker(connected, scope, my_generation))
        wake_sender.set()

    @client.on_disconnect()
    async def disconnected(error, reconnect, delay):
        nonlocal own_id, scope, sender_task, scanner_task, generation
        generation += 1
        STATE.record("disconnects")
        if sender_task:
            sender_task.cancel()
            sender_task = None
        if scanner_task:
            scanner_task.cancel()
            scanner_task = None
        store.recover_inflight()
        own_id = scope = None
        chat_cache.clear()
        STATE.set_qr(b"")
        STATE.set_status(
            "Связь с MAX потеряна; повторное подключение ожидается."
            if reconnect else "Связь с MAX остановлена."
        )

    @client.on_message()
    async def got_message(message, connected):
        if scope is None or own_id is None:
            return
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            return
        sent_at = _timestamp_ms(getattr(message, "time", None))
        if sent_at is None or sent_at < store.baseline(scope) - 5000:
            return
        if not await is_personal(connected, chat_id):
            return
        try:
            fresh = await enqueue_personal_message(
                connected, store, scope, own_id, message,
                chat_id, name_cache,
            )
        except Exception:
            STATE.record("errors")
            return
        if fresh:
            STATE.record("received")
            STATE.set_counts(store, scope)
            wake_sender.set()

    try:
        await client.start()
    except Exception:
        STATE.set_qr(b"")
        STATE.set_status(
            "MAX отключился без успешного восстановления. "
            "Возможно, потребуется повторная авторизация через QR."
        )
    finally:
        for task in (sender_task, scanner_task):
            if task:
                task.cancel()
        store.recover_inflight()
        store.close()


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING)
    panel = ThreadingHTTPServer(("0.0.0.0", 8099), Panel)
    threading.Thread(target=panel.serve_forever, daemon=True).start()
    try:
        token, chat_id = read_settings()
    except ValueError:
        STATE.set_status(
            "Нужно указать токен бота MAX и числовой ID закрытого рабочего чата."
        )
        threading.Event().wait()
        return
    asyncio.run(serve(token, chat_id))
    threading.Event().wait()


if __name__ == "__main__":
    main()
