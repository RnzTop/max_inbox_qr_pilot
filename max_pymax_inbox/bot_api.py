"""MAX Bot API: test-bot cards, callbacks and edits; never changes Webhooks."""
import json
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener


CHAT_ID_RE = re.compile(r"^-?[0-9]{1,19}$")


class BotApiError(Exception):
    """Sanitized API error: never exposes tokens, response bodies or URLs."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def parse_chat_id(value):
    if not isinstance(value, str) or not CHAT_ID_RE.fullmatch(value.strip()):
        raise ValueError("Нужен числовой ID рабочего группового чата MAX")
    number = int(value)
    if not (-2**63 <= number < 2**63) or number == 0:
        raise ValueError("ID рабочего чата выходит за допустимый диапазон")
    return number


class MaxBotAPI:
    BASE = "https://platform-api2.max.ru"

    def __init__(self, token, chat_id):
        if not isinstance(token, str) or not token.strip() or len(token.strip()) > 4096:
            raise ValueError("Введите действующий токен бота MAX")
        self._token = token.strip()
        self.chat_id = parse_chat_id(chat_id)
        context = ssl.create_default_context()
        for cert in (Path(__file__).parent / "certs").glob("*.pem"):
            context.load_verify_locations(cafile=str(cert))
        self._opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
        self.bot_id = None

    def _request(self, method, path, params=None, body=None):
        # All routes and target host are hardcoded; no URL from MAX payloads.
        url = self.BASE + path
        if params is not None:
            url += "?" + urlencode(params)
        data = None if body is None else json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        req = Request(url, method=method, data=data, headers={
            "Authorization": self._token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with self._opener.open(req, timeout=15) as response:
                payload = json.load(response)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise BotApiError("forbidden") from None
            if exc.code == 429:
                raise BotApiError("rate_limited") from None
            if exc.code in (400, 404, 422):
                raise BotApiError("rejected") from None
            # 5xx and redirects are uncertain after an outbound POST.
            raise BotApiError("uncertain") from None
        except (URLError, TimeoutError, OSError, ValueError, ssl.SSLError):
            raise BotApiError("uncertain") from None
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise BotApiError("uncertain")
        return payload

    def preflight(self):
        bot = self._request("GET", "/me")
        bot_id = bot.get("user_id")
        if type(bot_id) is not int:
            raise BotApiError("invalid_bot")
        chat = self._request("GET", f"/chats/{self.chat_id}")
        if (chat.get("chat_id") != self.chat_id
                or chat.get("type") != "chat"
                or chat.get("status") != "active"
                or chat.get("is_public") is not False):
            raise BotApiError("invalid_destination")
        membership = self._request(
            "GET", f"/chats/{self.chat_id}/members/me"
        )
        if (membership.get("user_id") != bot_id
                or membership.get("is_admin") is not True):
            # MAX does not deliver group updates reliably to non-admin bots.
            raise BotApiError("bot_not_admin")
        subscriptions = self._request("GET", "/subscriptions")
        if (not isinstance(subscriptions.get("subscriptions"), list)
                or subscriptions["subscriptions"]):
            # Only a dedicated bot without an existing Webhook can be polled.
            # Never delete/migrate a preexisting Webhook subscription.
            raise BotApiError("webhook_in_use")
        self.bot_id = bot_id
        return bot_id

    def get_updates(self, marker=None):
        """Only for a dedicated test bot. Durable cursor is stored by the caller."""
        if self.bot_id is None:
            raise BotApiError("not_ready")
        params = {"types": "message_callback", "limit": 100, "timeout": 10}
        if marker is not None:
            if type(marker) is not int or marker < 0:
                raise BotApiError("invalid_marker")
            params["marker"] = marker
        data = self._request("GET", "/updates", params)
        events, next_marker = data.get("updates"), data.get("marker")
        if (not isinstance(events, list)
                or (next_marker is not None
                    and (type(next_marker) is not int or next_marker < 0))):
            raise BotApiError("invalid_updates")
        return events, next_marker

    def send_card(self, text, payload):
        if not isinstance(payload, str) or not re.fullmatch(
            r"take:[0-9a-f]{64}", payload
        ):
            raise BotApiError("invalid_payload")
        return self._send(text, {
            "attachments": [{
                "type": "inline_keyboard",
                "payload": {"buttons": [[{
                    "type": "callback",
                    "text": "Взять в работу",
                    "payload": payload,
                }]]},
            }]
        })

    def send_text(self, text):
        return self._send(text, {})

    def _send(self, text, additions):
        if not self.bot_id:
            raise BotApiError("not_ready")
        if not isinstance(text, str) or not 1 <= len(text) <= 4000:
            raise BotApiError("invalid_body")
        result = self._request(
            "POST", "/messages",
            {"chat_id": self.chat_id, "disable_link_preview": "true"},
            {"text": text, **additions},
        )
        msg = result.get("message")
        mid = (msg.get("body") or {}).get("mid") if isinstance(msg, dict) else None
        if not mid:
            # Accepted HTTP response but unknown status; never send twice.
            raise BotApiError("uncertain")
        return str(mid)

    def acknowledge(self, callback_id, note):
        if not isinstance(callback_id, str) or not callback_id.strip():
            return
        self._request(
            "POST", "/answers", {"callback_id": callback_id},
            {"notification": note[:180]},
        )

    def edit_claimed(self, message_id, body, employee_name):
        if not isinstance(message_id, str) or not message_id:
            raise BotApiError("invalid_message")
        if not isinstance(employee_name, str) or not employee_name:
            raise BotApiError("invalid_assignee")
        suffix = "\n\n✅ Взял в работу: " + employee_name[:100]
        base = body or "Новое обращение из MAX [текст уже удалён]"
        text = base[:4000 - len(suffix)] + suffix
        self._request(
            "PUT", "/messages", {"message_id": message_id},
            {"text": text, "attachments": []},
        )
