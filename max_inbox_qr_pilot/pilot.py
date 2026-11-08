"""Limited QR pilot. Reads only marked incoming text in an explicitly named test dialog."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from cursor import TestCursor
from inbox import Inbox
from run_group_test import MaxAPI
from test_group import TestGroup
from ws_binary_probe import frame_meta, scan_binary_frame
from ws_id_stability import IdStability


DATA = Path("/data")
MARKER = re.compile(r"^MAX HA TEST [0-9]{4,16}$")
status = "Запуск пилота"
qr = b""


class Panel(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.client_address[0] != "172.30.32.2":
            self.send_error(403)
            return
        from html import escape
        image = qr
        if self.path.split("?")[0].endswith("/qr.png"):
            if not image:
                self.send_error(404)
                return
            payload, content_type = image, "image/png"
        else:
            text = escape(status)
            picture = '<img src="./qr.png" alt="Страница браузера MAX">' if image else ""
            payload = (f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="5">'
                       f'<h1>MAX — QR-пилот</h1><p>{text}</p>{picture}').encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def snapshot(page):
    return page.get_by_text(MARKER).evaluate_all("""nodes => nodes.flatMap(el => {
        const bubble = el.closest('.bubbleContent');
        const item = bubble?.closest('[data-index]');
        if (!item || !el.closest('[class*="--left"]')) return [];
        return [[item.getAttribute('data-index'), el.textContent.trim()]];
    })""")



def find_test_dialog(page, name):
    """Only select one *visible* exact-name match, never a fuzzy chat match."""
    labels = page.get_by_text(name, exact=True)
    total = labels.count()
    if total > 50:
        return None, total
    visible = [labels.nth(i) for i in range(total) if labels.nth(i).is_visible()]
    if len(visible) != 1:
        return None, len(visible)

    # Prefer the known chat-row markup, but do not depend on it.
    rows = page.locator("button.cell").filter(has=visible[0])
    if rows.count() == 1 and rows.first.is_visible():
        return rows.first, 1
    if rows.count() > 1:
        return None, len(visible)
    # Clicking the only visible exact-name label also works when MAX uses
    # a div, link, or a differently named chat-row element.
    return visible[0], 1



def show_login_if_needed(page):
    """Detect MAX login without relying on one heading or exposing chat screenshots."""
    global qr, status

    # MAX can display a choice between phone number and QR sign-in.
    switch = page.get_by_text(re.compile(r"^Войти\s+по\s+QR[-\s‑–]?коду$", re.I))
    if switch.count() == 1 and switch.first.is_visible():
        # Never click a chat message by mistake.
        if page.locator("button.cell").count() == 0:
            switch.first.click(timeout=5000)
            page.wait_for_timeout(1500)

    markers = (
        page.get_by_role("heading", name=re.compile(r"(Войдите|Вход).*(MAX|QR)", re.I)),
        page.get_by_text(re.compile(r"^(?:Отсканируйте|Наведите камеру).*QR[-\s‑–]?код", re.I)),
        page.get_by_text(re.compile(r"^Войти\s+по\s+QR[-\s‑–]?коду$", re.I)),
    )
    is_login = any(locator.count() and locator.first.is_visible() for locator in markers)
    if is_login:
        status = "Открыта страница входа MAX. Отсканируйте QR-код в панели дополнения."
        qr = page.screenshot()
        return True

    # A phone-login form is not a chat list, even if the specific QR title changed.
    phone = page.locator('input[type="tel"]')
    if phone.count() and phone.first.is_visible():
        qr = b""
        status = "Встроенный браузер MAX показывает вход по телефону, а не чаты. Нужен вход по QR-коду."
        return True

    qr = b""
    return False



def diagnose_message_ids(page):
    """Check DOM attribute PRESENCE only; never read message or ID values."""
    try:
        probe = page.evaluate("""() => {
            const bubbles = Array.from(document.querySelectorAll('.bubbleContent')).slice(0, 100);
            const keys = ['mid', 'chat', 'seq', 'index', 'html_id'];
            const found = {mid: 0, chat: 0, seq: 0, index: 0, html_id: 0};
            for (const bubble of bubbles) {
                const seen = new Set();
                function inspect(node) {
                    for (const attr of node.attributes || []) {
                        if (!attr.value) continue;
                        const name = attr.name.toLowerCase().replace(/[^a-z0-9]/g, '');
                        if (['mid', 'datamid', 'messageid', 'datamessageid',
                             'msgid', 'datamsgid'].includes(name)) seen.add('mid');
                        if (['chatid', 'datachatid', 'peerid', 'datapeerid',
                             'dialogid', 'datadialogid'].includes(name)) seen.add('chat');
                        if (['seq', 'dataseq', 'sequence', 'datasequence'].includes(name)) seen.add('seq');
                        if (name === 'dataindex') seen.add('index');
                        if (name === 'id') seen.add('html_id');
                    }
                }
                let parent = bubble;
                for (let level = 0; level < 8 && parent; level++, parent = parent.parentElement) {
                    inspect(parent);
                }
                const inside = bubble.querySelectorAll('*');
                for (let i = 0; i < Math.min(inside.length, 50); i++) inspect(inside[i]);
                for (const name of keys) if (seen.has(name)) found[name]++;
            }
            const hasChatAttributeElsewhere = !!document.querySelector(
                '[data-chat-id], [data-chatid], [chat-id], [data-peer-id], [data-dialog-id]'
            );
            const route = location.pathname !== '/' || !!location.search || !!location.hash;
            return {bubbles: bubbles.length, ...found, hasChatAttributeElsewhere, route};
        }""")
        if not isinstance(probe, dict):
            return "DOM: данные диагностики недоступны."
        return (
            "DOM (только наличие атрибутов, не ID): "
            "сообщений={bubbles}; mid={mid}; chat={chat}; seq={seq}; "
            "data-index={index}; обычный HTML id={html_id}; "
            "chat-атрибут вне сообщения={outside}; маршрут в URL={route}."
        ).format(
            bubbles=int(probe.get("bubbles", 0)),
            mid=int(probe.get("mid", 0)),
            chat=int(probe.get("chat", 0)),
            seq=int(probe.get("seq", 0)),
            index=int(probe.get("index", 0)),
            html_id=int(probe.get("html_id", 0)),
            outside="да" if probe.get("hasChatAttributeElsewhere") else "нет",
            route="да" if probe.get("route") else "нет",
        )
    except Exception:
        # The app must keep running even if the DOM changes while probing.
        return "DOM: диагностика временно недоступна."



def diagnose_exact_marker_nodes(page):
    """Use the SAME marker and ancestors as snapshot(); reveal only counts."""
    try:
        result = page.get_by_text(MARKER).evaluate_all("""els => {
            const counts = {
                markers: els.length, bubbles: 0, indexed: 0,
                inbound: 0, deep_index: 0, mid: 0, chat: 0, seq: 0
            };
            for (const el of els.slice(0, 100)) {
                const bubble = el.closest('.bubbleContent');
                if (!bubble) continue;
                counts.bubbles++;
                if (el.closest('[class*="--left"]')) counts.inbound++;
                const index = bubble.closest('[data-index]');
                if (index) counts.indexed++;
                let depth = 0;
                let current = bubble;
                let mid = false, chat = false, seq = false;
                while (current && depth < 40) {
                    if (current === index && depth > 7) counts.deep_index++;
                    for (const attr of current.attributes || []) {
                        const key = attr.name.toLowerCase().replace(/[^a-z0-9]/g, '');
                        if (['mid', 'datamid', 'messageid', 'datamessageid',
                             'msgid', 'datamsgid'].includes(key)) mid = true;
                        if (['chatid', 'datachatid', 'peerid', 'datapeerid',
                             'dialogid', 'datadialogid'].includes(key)) chat = true;
                        if (['seq', 'dataseq', 'sequence', 'datasequence'].includes(key)) seq = true;
                    }
                    current = current.parentElement;
                    depth++;
                }
                if (mid) counts.mid++;
                if (chat) counts.chat++;
                if (seq) counts.seq++;
            }
            return counts;
        }""")
        if not isinstance(result, dict):
            return "Тестовые метки: проверка недоступна."
        return (
            "Метки: найдено={markers}; bubble={bubbles}; входящих={inbound}; "
            "с data-index={indexed}; глубже 8 уровней={deep_index}; "
            "mid={mid}; chat={chat}; seq={seq}."
        ).format(**{name: min(int(result.get(name, 0)), 100)
                   for name in ("markers", "bubbles", "inbound", "indexed",
                                "deep_index", "mid", "chat", "seq")})
    except Exception:
        return "Тестовые метки: проверка временно недоступна."


def diagnose_page_state(page):
    """Only return bounded counters and booleans, never URLs or page text."""
    try:
        info = page.evaluate("""() => {
            const body = document.body;
            const text = (body?.innerText || '');
            return {
                ready: document.readyState,
                elements: document.querySelectorAll('*').length,
                body_chars: text.length,
                chat_rows: document.querySelectorAll('button.cell').length,
                message_bubbles: document.querySelectorAll('.bubbleContent').length,
                message_indices: document.querySelectorAll('[data-index]').length,
                chat_editors: document.querySelectorAll('[contenteditable="true"]').length,
                phone_inputs: document.querySelectorAll('input[type="tel"]').length,
                qr_canvas: document.querySelectorAll('canvas').length,
                qr_words: /QR[\s\-‑–]*код|войти\s+в\s+MAX|вход\s+в\s+MAX|сканируйте/i.test(text),
                root_empty: !body || body.children.length === 0
            };
        }""")
        if not isinstance(info, dict):
            return "Страница: диагностика недоступна."
        ready = info.get("ready") if info.get("ready") in ("loading", "interactive", "complete") else "unknown"
        return (
            "Страница MAX: загрузка={ready}; DOM-элементов={elements}; "
            "символов на странице={body_chars}; строк чатов={chat_rows}; "
            "сообщений по CSS={message_bubbles}; элементов data-index={message_indices}; "
            "полей ввода чата={chat_editors}; телефонных полей={phone_inputs}; "
            "canvas={qr_canvas}; текстовый признак входа={qr_words}; "
            "пустая страница={root_empty}."
        ).format(
            ready=ready,
            elements=min(int(info.get("elements", 0)), 99999),
            body_chars=min(int(info.get("body_chars", 0)), 99999),
            chat_rows=min(int(info.get("chat_rows", 0)), 99999),
            message_bubbles=min(int(info.get("message_bubbles", 0)), 99999),
            message_indices=min(int(info.get("message_indices", 0)), 99999),
            chat_editors=min(int(info.get("chat_editors", 0)), 99999),
            phone_inputs=min(int(info.get("phone_inputs", 0)), 99999),
            qr_canvas=min(int(info.get("qr_canvas", 0)), 99999),
            qr_words="да" if info.get("qr_words") else "нет",
            root_empty="да" if info.get("root_empty") else "нет",
        )
    except Exception:
        return "Страница: диагностика временно недоступна."




class WebSocketIdProbe:
    """Read-only, in-memory counts of ID-shaped fields from MAX WebSocket frames.

    Raw frames, URLs, credentials, message text and identifiers are NEVER
    printed, retained, uploaded, or written to disk.
    """

    MAX_FRAME_BYTES = 2 * 1024 * 1024

    def __init__(self, stability=None):
        self.stability = stability
        self.counts = {
            "sockets": 0, "watched": 0,
            "oneme_sockets": 0, "max_sockets": 0, "other_sockets": 0,
            "frames": 0, "json": 0, "binary": 0, "large": 0, "other": 0,
            "protocol_frames": 0, "msgpack": 0, "lz4": 0, "zstd": 0,
            "header_miss": 0, "binary_decode_failed": 0,
            "lz4_errors": 0, "msgpack_errors": 0,
            "chat_ids": 0, "message_ids": 0, "chat_refs": 0, "pairs": 0,
            "history_requests": 0, "history_responses": 0,
            "matched_history": 0, "history_ids": 0,
            "live_frames": 0, "live_ids": 0, "live_with_chat": 0,
            "login_frames": 0, "login_chat_ids": 0,
            "errors": 0,
        }
        # Only packet sequence numbers and booleans. No chat/message IDs.
        self._history_pending = {}

    def _limit_pending(self):
        if len(self._history_pending) > 256:
            self._history_pending.clear()

    def _on_sent_frame(self, raw):
        """Record ONLY whether a history request included chatId, not its value."""
        try:
            if not isinstance(raw, bytes):
                return
            meta = frame_meta(raw)
            if meta is None:
                return
            cmd, seq, opcode = meta
            if cmd != 0 or opcode != 49:
                return
            kind, payload = scan_binary_frame(
                raw, fingerprint_key=(self.stability.key if self.stability else None))
            if kind not in ("msgpack", "lz4_msgpack"):
                return
            if isinstance(payload, dict) and self._has_id(payload.get("chatId")):
                self.counts["history_requests"] += 1
                self._limit_pending()
                self._history_pending[seq] = (
                    payload.get("chatId") if self.stability else True)
        except Exception:
            self.counts["errors"] += 1

    def attach(self, page):
        # Attach BEFORE page.goto(). No interception/modification of traffic.
        page.on("websocket", self._on_socket)

    def _on_socket(self, ws):
        self.counts["sockets"] += 1
        try:
            host = (urlsplit(ws.url).hostname or "").lower()
            if host == "oneme.ru" or host.endswith(".oneme.ru"):
                self.counts["oneme_sockets"] += 1
            elif host == "max.ru" or host.endswith(".max.ru"):
                self.counts["max_sockets"] += 1
            else:
                self.counts["other_sockets"] += 1

            # One WebSocket was previously detected but was not hosted at the
            # single hard-coded hostname. Observe all sockets initiated by the
            # already-scoped web.max.ru page, without storing raw frames/URLs.
            ws.on("framereceived", self._on_frame)
            ws.on("framesent", self._on_sent_frame)
            self.counts["watched"] += 1
        except Exception:
            # Do not expose an exception: WS URLs may contain credentials.
            self.counts["errors"] += 1

    @staticmethod
    def _has_id(value):
        return (type(value) is int
                or (isinstance(value, str) and bool(value.strip())))

    def _on_frame(self, raw):
        self.counts["frames"] += 1
        try:
            if not isinstance(raw, (str, bytes)) or len(raw) > self.MAX_FRAME_BYTES:
                self.counts["large"] += 1
                return
            if isinstance(raw, bytes):
                self.counts["binary"] += 1
                meta = frame_meta(raw)
                kind, data = scan_binary_frame(
                    raw, fingerprint_key=(self.stability.key if self.stability else None))
                if kind == "zstd":
                    self.counts["zstd"] += 1
                    return
                if kind == "missing_header":
                    self.counts["header_miss"] += 1
                    return
                if kind not in ("msgpack", "lz4_msgpack"):
                    self.counts["binary_decode_failed"] += 1
                    if kind == "lz4_error":
                        self.counts["lz4_errors"] += 1
                    elif kind == "msgpack_error":
                        self.counts["msgpack_errors"] += 1
                    return
                self.counts["msgpack"] += 1
                if kind == "lz4_msgpack":
                    self.counts["lz4"] += 1
                if isinstance(data, dict):
                    self.counts["protocol_frames"] += 1
                    self._observe_payload(data)
                    self._observe_frame_class(meta, data)
                return
            try:
                frame = json.loads(raw)
            except (ValueError, TypeError):
                self.counts["other"] += 1
                return
            if not isinstance(frame, dict):
                self.counts["other"] += 1
                return
            self.counts["json"] += 1
            if type(frame.get("opcode")) is not int or type(frame.get("cmd")) is not int:
                return
            data = frame.get("payload")
            if isinstance(data, dict):
                self.counts["protocol_frames"] += 1
                self._observe_payload(data)
                self._observe_frame_class((frame.get("cmd"),
                                           frame.get("seq"), frame.get("opcode")), data)
        except Exception:
            # Do not include any exceptions: they can embed customer data.
            self.counts["errors"] += 1

    def _observe_payload(self, data):
        # Whitelisted shape-only dicts for binary, regular JSON for text.
        # NEVER emit IDs, secrets or actual message contents.
        chat_ref = self._has_id(data.get("chatId"))
        chat = data.get("chat")
        if isinstance(chat, dict) and self._has_id(chat.get("id")):
            chat_ref = True
            self.counts["chat_ids"] += 1
        if chat_ref:
            self.counts["chat_refs"] += 1

        messages = data.get("messages")
        items = messages[:1000] if isinstance(messages, list) else []
        single = data.get("message")
        if isinstance(single, dict):
            items = items + [single]
        count = sum(isinstance(m, dict) and self._has_id(m.get("id"))
                    for m in items)
        self.counts["message_ids"] += count
        if chat_ref:
            self.counts["pairs"] += count

        chats = data.get("chats")
        if isinstance(chats, list):
            for item in chats[:1000]:
                if not isinstance(item, dict) or not self._has_id(item.get("id")):
                    continue
                self.counts["chat_ids"] += 1
                last_message = item.get("lastMessage")
                if (isinstance(last_message, dict)
                        and self._has_id(last_message.get("id"))):
                    self.counts["message_ids"] += 1
                    self.counts["pairs"] += 1

    def _observe_frame_class(self, meta, data):
        """Count live message IDs separately from login/history, without values."""
        if meta is None or not isinstance(data, dict):
            return
        cmd, seq, opcode = meta
        if opcode == 19 and cmd == 1:
            self.counts["login_frames"] += 1
            chats = data.get("chats")
            if isinstance(chats, list):
                self.counts["login_chat_ids"] += sum(
                    isinstance(c, dict) and self._has_id(c.get("id"))
                    for c in chats[:1000])
        elif opcode == 49 and cmd == 1:
            self.counts["history_responses"] += 1
            messages = data.get("messages")
            if isinstance(messages, list):
                self.counts["history_ids"] += sum(
                    isinstance(m, dict) and self._has_id(m.get("id"))
                    for m in messages[:1000])
            chat_digest = self._history_pending.pop(seq, None)
            if chat_digest:
                self.counts["matched_history"] += 1
                if self.stability and isinstance(messages, list):
                    self.stability.history(
                        chat_digest,
                        [m.get("id") for m in messages[:1000] if isinstance(m, dict)],
                    )
        elif opcode == 128 and cmd == 0:
            # Push-style message notification, NOT message history.
            self.counts["live_frames"] += 1
            message = data.get("message")
            if isinstance(message, dict) and self._has_id(message.get("id")):
                self.counts["live_ids"] += 1
                if self._has_id(data.get("chatId")):
                    self.counts["live_with_chat"] += 1
                    if self.stability:
                        self.stability.push(data.get("chatId"), message.get("id"))

    def summary(self):
        c = {k: min(v, 999999) for k, v in self.counts.items()}
        return (
            "WebSocket MAX (счётчики, без значений): "
            "сокетов={sockets}; наблюдаемых={watched}; "
            "адреса oneme={oneme_sockets}, max={max_sockets}, другие={other_sockets}; "
            "кадров={frames}; JSON={json}; протокол MAX={protocol_frames}; "
            "двоичных={binary}; MessagePack={msgpack}; LZ4={lz4}; "
            "Zstd-пропущено={zstd}; неизвестный заголовок={header_miss}; "
            "не удалось декодировать={binary_decode_failed} "
            "(LZ4={lz4_errors}, MessagePack={msgpack_errors}); "
            "слишком больших={large}; не-JSON={other}; "
            "чатов с id={chat_ids}; сообщений с id={message_ids}; "
            "кадров с chatId={chat_refs}; пар chat+message={pairs}; "
            "LOGIN кадров={login_frames}, чатов={login_chat_ids}; "
            "история запросов={history_requests}, ответов={history_responses}, "
            "связано по seq={matched_history}, message.id={history_ids}; "
            "новых push кадров={live_frames}, message.id={live_ids}, "
            "с chatId={live_with_chat}; ошибок проверки={errors}."
        ).format(**c) + (" " + self.stability.summary() if self.stability else "")

def run(options):
    global status, qr
    if not options.get("test_confirmed"):
        status = "Включите test_confirmed и заполните параметры тестовых аккаунтов."
        return
    dialog = options["test_dialog"].strip()
    account = options["test_account_label"].strip()
    if not dialog or not account or not options["test_bot_token"].strip():
        raise ValueError("Заполните имя тестового диалога, метку аккаунта и токен")
    queue = Inbox(DATA / "pilot.sqlite3")
    api = MaxAPI(options["test_bot_token"].strip())
    bot = api.call("GET", "/me")
    if (bot.get("username") or "").lstrip("@") != options["test_bot_username"].strip().lstrip("@"):
        raise ValueError("Токен не соответствует указанному тестовому боту")
    group = TestGroup(queue, api, options["test_chat_id"])
    group.check_destination()
    binding = DATA / "binding.json"
    identity = [account, dialog, bot["user_id"], group.chat_id]
    if binding.exists() and json.loads(binding.read_text()) != identity:
        raise ValueError("Параметры уже созданного теста изменились")
    binding.write_text(json.dumps(identity, ensure_ascii=False))
    cursor = TestCursor(queue, account, dialog)
    params = {"types": "message_callback", "timeout": 0}
    marker = api.call("GET", "/updates", params).get("marker")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(DATA / "browser"), executable_path="/usr/bin/chromium",
            headless=True, chromium_sandbox=True, accept_downloads=False,
        )
        page = context.pages[0] if context.pages else context.new_page()
        ws_probe = WebSocketIdProbe(IdStability(DATA / "ws_stability"))
        ws_probe.attach(page)
        page.goto("https://web.max.ru/", wait_until="domcontentloaded")
        opened = False
        while True:
            if urlsplit(page.url).hostname != "web.max.ru":
                raise ValueError("Браузер покинул веб-версию MAX")
            if show_login_if_needed(page):
                status += " " + ws_probe.summary()
                opened = False
                time.sleep(5)
                continue
            if not opened:
                target, matches = find_test_dialog(page, dialog)
                if target is None:
                    # Keep the screen visible only to the local Home Assistant
                    # ingress panel. Never log, save, or upload the image.
                    qr = page.screenshot()
                    status = (f"Тестовый чат не найден: точных совпадений {matches}. "
                              "Под диагностикой показан экран встроенного браузера MAX. "
                              "Не присылайте снимок с личными сообщениями. "
                              + diagnose_page_state(page) + " "
                              + diagnose_message_ids(page) + " "
                              + ws_probe.summary())
                    time.sleep(3)
                    continue
                target.click(timeout=5000)
                opened = True
                time.sleep(2)
            events = snapshot(page)
            diagnostics = (diagnose_message_ids(page) + " "
                           + diagnose_exact_marker_nodes(page) + " "
                           + ws_probe.summary())
            if not events:
                status = ("В тестовом диалоге ожидаю начальную метку MAX HA TEST и четыре цифры. "
                          + diagnostics)
                time.sleep(2)
                continue
            cursor.observe(events)
            status = ("Пилот слушает тестовый диалог. Отправьте новую метку MAX HA TEST с цифрами. "
                      + diagnostics)
            if group.send_one():
                time.sleep(0.6)
            if marker is not None:
                params["marker"] = marker
            batch = api.call("GET", "/updates", params)
            for event in batch.get("updates", []):
                group.accept_callback(event)
            marker = batch.get("marker", marker)
            if group.edit_one():
                time.sleep(0.6)
            time.sleep(2)


if __name__ == "__main__":
    os.umask(0o077)
    panel = ThreadingHTTPServer(("0.0.0.0", 8099), Panel)
    threading.Thread(target=panel.serve_forever, daemon=True).start()
    try:
        run(json.loads((DATA / "options.json").read_text()))
    except Exception as exc:
        qr = b""
        status = ("Пилот остановлен: " + str(exc)) if type(exc) in (ValueError, RuntimeError) else (
            "Пилот остановлен на этапе браузера или API. Проверьте настройки и поддержку Chromium sandbox.")
        print(status, flush=True)
    threading.Event().wait()
