"""Bounded, read-only replay of missed test markers from one verified MAX DM.

The only persisted message data are HMAC fingerprints already managed by SeenIds.
No message content or raw chat IDs are stored or logged.
"""
from dataclasses import dataclass
import re
import time


TEST_MARKER = re.compile(r"^MAX HA TEST [0-9]{4,16}$")
PAGE_SIZE = 50
MAX_PAGES = 10
OVERLAP_MS = 5_000


@dataclass(frozen=True)
class RecoveryResult:
    scanned: int = 0
    recovered: int = 0
    complete: bool = False
    initialized: bool = False
    error: str = ""


def _message_time_ms(value):
    # MAX commonly uses milliseconds; accept seconds if a payload uses them.
    if type(value) is not int:
        return None
    if 1_000_000_000 <= value < 10_000_000_000:
        return value * 1000
    if 10_000_000_000 <= value <= 100_000_000_000_000:
        return value
    return None


async def recover_history(
    client, store, account_id, dialog, chat_id, *, now_ms=None,
    page_size=PAGE_SIZE, max_pages=MAX_PAGES,
):
    """Replay only exact incoming test markers after the last completed sweep.

    Only scan a verified HMAC-bound chat, read backwards from a fixed snapshot,
    and advance the cursor only if the boundary is reached. If the bounded scan
    is incomplete, leave the previous cursor intact to avoid message loss.
    """
    if chat_id is None or not store.is_bound_chat(
        account_id, dialog, chat_id, store.get_binding(account_id, dialog)
    ):
        return RecoveryResult(error="unverified_dialog")
    if not (1 <= page_size <= PAGE_SIZE and 1 <= max_pages <= MAX_PAGES):
        return RecoveryResult(error="invalid_limit")

    end_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    previous = store.history_cursor(account_id, dialog)
    if previous is None:
        # Fail closed on migration/first pairing: never ingest a lifetime of
        # old messages simply because there was no prior high-water mark.
        store.advance_history_cursor(account_id, dialog, end_ms)
        return RecoveryResult(complete=True, initialized=True)
    if previous > end_ms:
        return RecoveryResult(error="clock_moved_back")
    if previous == end_ms:
        return RecoveryResult(complete=True)

    cutoff_ms = max(0, previous - OVERLAP_MS)
    before_ms = end_ms + 1
    inspected = 0
    candidates = []
    complete = False
    for _ in range(max_pages):
        try:
            messages = await client.fetch_history(
                chat_id=chat_id,
                from_time=before_ms,
                backward=page_size,
                forward=0,
                get_messages=True,
            )
        except Exception:
            # Never leak MAX payloads/tokens in logs.
            return RecoveryResult(scanned=inspected, error="history_request_failed")

        if not messages:
            complete = True
            break
        inspected += len(messages)
        times = [_message_time_ms(getattr(m, "time", None)) for m in messages]
        if any(t is None for t in times):
            return RecoveryResult(scanned=inspected, error="invalid_message_time")

        oldest_ms = min(times)
        for m, timestamp in zip(messages, times):
            if cutoff_ms <= timestamp <= end_ms:
                candidates.append((timestamp, m))

        # The server page has crossed the known checkpoint.
        if oldest_ms <= cutoff_ms or len(messages) < page_size:
            complete = True
            break
        if oldest_ms >= before_ms:
            return RecoveryResult(scanned=inspected, error="history_no_progress")
        before_ms = oldest_ms

    recovered = 0
    in_sweep = set()
    for _, msg in sorted(candidates, key=lambda item: item[0]):
        message_id = getattr(msg, "id", None)
        if message_id is None:
            continue
        payload_chat = getattr(msg, "chat_id", None)
        # Missing chat_id is allowed in a response scoped to a requested chat.
        if payload_chat is not None and payload_chat != chat_id:
            continue
        sender = getattr(msg, "sender", None)
        if sender is None or sender == account_id:
            continue
        if not TEST_MARKER.fullmatch(getattr(msg, "text", "") or ""):
            continue
        key = str(message_id)
        if key in in_sweep:
            continue
        in_sweep.add(key)
        if store.observe(chat_id, message_id):
            recovered += 1

    if complete:
        store.advance_history_cursor(account_id, dialog, end_ms)
    return RecoveryResult(
        scanned=inspected,
        recovered=recovered,
        complete=complete,
        error="" if complete else "history_limit_reached",
    )
