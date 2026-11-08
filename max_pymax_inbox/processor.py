"""Privacy-scoped incoming MAX personal message normalization and replay."""
import asyncio
import re
import time


PAGE_SIZE = 50
MAX_PAGES = 10
OVERLAP_MS = 5000
HISTORY_TIMEOUT_SEC = 20
MAX_BODY = 4000


def clean_single_line(value, limit=160):
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    return text[:limit]


def _timestamp_ms(value):
    if type(value) is not int:
        return None
    if 1_000_000_000 <= value < 10_000_000_000:
        return value * 1000
    if 10_000_000_000 <= value < 100_000_000_000_000:
        return value
    return None


def _attachment_summary(attachments):
    names = {
        "PHOTO": "Фото", "IMAGE": "Фото", "VIDEO": "Видео",
        "AUDIO": "Аудио", "VOICE": "Голосовое сообщение",
        "FILE": "Файл", "DOCUMENT": "Документ",
        "CONTACT": "Контакт", "STICKER": "Стикер",
        "LOCATION": "Геопозиция", "POLL": "Опрос",
    }
    result = []
    for attachment in (attachments or [])[:20]:
        kind = getattr(attachment, "type", "unknown")
        kind = getattr(kind, "value", kind)
        label = names.get(str(kind).upper(), "Вложение")
        result.append(label)
    return result


async def sender_label(client, sender_id, cache):
    if sender_id in cache:
        return cache[sender_id]
    name = ""
    try:
        user = await asyncio.wait_for(client.get_user(sender_id), timeout=10)
        for part in getattr(user, "names", []) or []:
            rendered = getattr(part, "name", None)
            if not rendered:
                rendered = " ".join(filter(None, [
                    getattr(part, "first_name", None),
                    getattr(part, "last_name", None),
                ]))
            if rendered:
                name = clean_single_line(rendered, 100)
                break
    except Exception:
        pass
    if not name:
        name = "Пользователь MAX"
    if len(cache) >= 500:
        cache.clear()
    cache[sender_id] = name
    return name


async def enqueue_personal_message(
    client, store, scope, own_id, message, chat_id, name_cache,
):
    if chat_id is None or getattr(message, "chat_id", chat_id) not in (None, chat_id):
        return False
    sender = getattr(message, "sender", None)
    mid = getattr(message, "id", None)
    if sender is None or sender == own_id or mid is None:
        return False
    text = getattr(message, "text", "") or ""
    attachments = _attachment_summary(getattr(message, "attaches", []))
    if not text and not attachments:
        return False
    name = await sender_label(client, sender, name_cache)
    title = (
        "Новое обращение из MAX\n"
        f"От: {name}\n"
        f"ID отправителя: {clean_single_line(sender, 32)}\n\n"
    )
    if text:
        suffix = "\n\n"
        if attachments:
            suffix += "Вложения: " + ", ".join(attachments) + (
                "\nМедиафайлы пока не копируются: откройте исходный диалог MAX."
            )
        body = title + text[:MAX_BODY - len(title) - len(suffix) - 40] + suffix
        if len(text) > MAX_BODY - len(title) - len(suffix) - 40:
            body += "\n[Длинный текст сокращён]"
    else:
        body = title + "Вложения: " + ", ".join(attachments) + (
            "\nМедиафайлы пока не копируются: откройте исходный диалог MAX."
        )
    return store.enqueue(scope, chat_id, mid, body[:MAX_BODY])


async def recover_chat(
    client, store, scope, own_id, chat_id, name_cache, *, now_ms=None,
    pages=MAX_PAGES, page_size=PAGE_SIZE,
):
    """One bounded, resumable backfill sweep for a single verified personal chat."""
    if not 1 <= pages <= MAX_PAGES or not 1 <= page_size <= PAGE_SIZE:
        return {"complete": False, "new": 0, "error": "invalid_limit"}
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    try:
        cursor, snapshot, cutoff, before = store.begin_scan(
            scope, chat_id, now_ms
        )
    except ValueError:
        return {"complete": False, "new": 0, "error": "invalid_cursor"}
    if snapshot is None:
        return {"complete": True, "new": 0, "error": ""}
    enqueued = 0
    for _ in range(pages):
        try:
            chunk = await asyncio.wait_for(
                client.fetch_history(
                    chat_id=chat_id, from_time=before,
                    backward=page_size, forward=0, get_messages=True,
                ),
                timeout=HISTORY_TIMEOUT_SEC,
            )
        except Exception:
            return {"complete": False, "new": enqueued, "error": "history_failed"}
        if not isinstance(chunk, list) or len(chunk) > page_size:
            return {"complete": False, "new": enqueued, "error": "invalid_page"}
        if not chunk:
            store.complete_scan(scope, chat_id, snapshot)
            return {"complete": True, "new": enqueued, "error": ""}
        times = [_timestamp_ms(getattr(m, "time", None)) for m in chunk]
        if any(t is None for t in times):
            return {"complete": False, "new": enqueued, "error": "invalid_time"}
        oldest = min(times)
        if oldest >= before:
            return {"complete": False, "new": enqueued, "error": "no_progress"}
        for ts, message in sorted(zip(times, chunk), key=lambda x: x[0]):
            if cutoff <= ts <= snapshot:
                if await enqueue_personal_message(
                    client, store, scope, own_id, message, chat_id, name_cache,
                ):
                    enqueued += 1
        if len(chunk) < page_size or oldest <= cutoff:
            store.complete_scan(scope, chat_id, snapshot)
            return {"complete": True, "new": enqueued, "error": ""}
        next_before = oldest + 1
        if not cutoff < next_before < before:
            return {
                "complete": False, "new": enqueued,
                "error": "timestamp_collision",
            }
        store.progress_scan(scope, chat_id, snapshot, next_before)
        before = next_before
    return {"complete": False, "new": enqueued, "error": "page_limit"}
