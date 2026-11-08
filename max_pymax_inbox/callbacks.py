"""Validate bot callback events before assigning an existing group card.

Never trust user-supplied callback payload alone: the destination group,
exact original bot message ID and durable outbox fingerprint must all match.
"""
import re


PAYLOAD_RE = re.compile(r"^take:([0-9a-f]{64})$")


def _employee_name(user):
    first = user.get("first_name") or ""
    last = user.get("last_name") or ""
    fallback = user.get("name") or ""
    raw = " ".join(str(x) for x in (first, last) if x) or str(fallback)
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", raw).strip()
    if not clean:
        clean = "Сотрудник MAX"
    return clean[:100]


def handle_callback(update, store, scope, work_chat_id, bot_id):
    """Return (status, callback_id), no Bot API calls or plaintext logging."""
    if not isinstance(update, dict) or update.get("update_type") != "message_callback":
        return "ignored", None
    callback = update.get("callback")
    message = update.get("message")
    if not isinstance(callback, dict) or not isinstance(message, dict):
        return "ignored", None
    recipient = message.get("recipient")
    body = message.get("body")
    user = callback.get("user")
    if not all(isinstance(part, dict) for part in (recipient, body, user)):
        return "ignored", None
    if (recipient.get("chat_id") != work_chat_id
            or recipient.get("chat_type") != "chat"):
        return "ignored", None
    author = message.get("sender")
    if isinstance(author, dict) and author.get("user_id") != bot_id:
        return "ignored", None
    payload = callback.get("payload")
    if not isinstance(payload, str) or len(payload) != 69:
        return "ignored", None
    match = PAYLOAD_RE.fullmatch(payload)
    if not match:
        return "ignored", None
    original_mid = body.get("mid")
    callback_id = callback.get("callback_id")
    user_id = user.get("user_id")
    if (not isinstance(original_mid, str) or not original_mid
            or type(user_id) is not int or user_id <= 0
            or user.get("is_bot") is True):
        return "ignored", None
    if (not isinstance(callback_id, str)
            or not 1 <= len(callback_id) <= 1024):
        callback_id = None
    status = store.claim(
        scope, match.group(1), original_mid,
        user_id, _employee_name(user),
    )
    return status, callback_id
