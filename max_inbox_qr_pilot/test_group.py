"""Transport for a dedicated MAX test bot and one private test group."""
from inbox import claimed_payload, message_payload


class TestGroup:
    def __init__(self, queue, api, chat_id):
        self.queue = queue
        self.api = api
        self.chat_id = int(chat_id)

    def check_destination(self):
        chat = self.api.call("GET", f"/chats/{self.chat_id}")
        if (chat.get("chat_id") != self.chat_id or chat.get("type") != "chat"
                or chat.get("is_public") is not False or chat.get("status") != "active"):
            raise ValueError("Нужна закрытая тестовая группа с добавленным тестовым ботом")
        if self.api.call("GET", "/subscriptions").get("subscriptions"):
            raise ValueError("Для отдельного тестового бота требуется отсутствие Webhook")

    def send_one(self):
        notice = self.queue.reserve_delivery()
        if notice is None:
            return False
        result = self.api.call("POST", "/messages", {"chat_id": self.chat_id}, message_payload(notice))
        message_id = result["message"]["body"]["mid"]
        self.queue.delivered(notice["id"], message_id)
        return True

    def accept_callback(self, update):
        if update.get("update_type") != "message_callback":
            return False
        message = update.get("message") or {}
        recipient = message.get("recipient") or {}
        if recipient.get("chat_id") != self.chat_id or recipient.get("chat_type") != "chat":
            return False
        callback = update.get("callback") or {}
        payload = callback.get("payload") or ""
        message_id = (message.get("body") or {}).get("mid")
        user = callback.get("user") or {}
        employee_id = user.get("user_id")
        if (not payload.startswith("inbox:") or not payload[6:].isascii()
                or not payload[6:].isdigit() or len(payload[6:]) > 18
                or not message_id or not employee_id or user.get("is_bot")):
            return False
        employee = " ".join(str(user.get(k) or "") for k in ("first_name", "last_name")).strip()
        employee = employee or user.get("name") or "Сотрудник MAX"
        claimed = self.queue.claim(int(payload[6:]), message_id, str(employee_id), employee)
        if callback.get("callback_id"):
            self.api.call("POST", "/answers", {"callback_id": callback["callback_id"]},
                          {"notification": "Обращение закреплено" if claimed else "Обращение уже обработано"})
        return claimed

    def edit_one(self):
        change = self.queue.pending_edit()
        if change is None:
            return False
        self.api.call("PUT", "/messages", {"message_id": change["group_message"]}, claimed_payload(change))
        self.queue.edited(change["id"], change["revision"])
        return True
