"""Resumable, bounded read-only replay for one verified MAX test dialogue.

Only HMAC message fingerprints, a binding MAC and timestamp cursors persist.
History content, raw chat identifiers and account identifiers are never logged.
"""
import asyncio
from dataclasses import dataclass
import re
import time


TEST_MARKER = re.compile(r"^MAX HA TEST [0-9]{4,16}$")
PAGE_SIZE = 50
MAX_PAGES = 10
OVERLAP_MS = 5_000
REQUEST_TIMEOUT_SEC = 20


@dataclass(frozen=True)
class RecoveryResult:
    scanned: int = 0
    recovered: int = 0
    complete: bool = False
    initialized: bool = False
    error: str = ""


def _message_time_ms(value):
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
    request_timeout=REQUEST_TIMEOUT_SEC,
):
    """Continue a persisted snapshot, up to 500 messages per invocation.

    Read backwards and persist a page cursor *only after* every accepted
    message was durably fingerprinted. Network errors retain the checkpoint.
    A new snapshot begins only when the earlier snapshot is complete.
    """
    if chat_id is None or not store.is_bound_chat(
        account_id, dialog, chat_id, store.get_binding(account_id, dialog)
    ):
        return RecoveryResult(error="unverified_dialog")
    if not (1 <= page_size <= PAGE_SIZE and 1 <= max_pages <= MAX_PAGES):
        return RecoveryResult(error="invalid_limit")
    if not (0 < request_timeout <= REQUEST_TIMEOUT_SEC):
        return RecoveryResult(error="invalid_timeout")

    now = int(time.time() * 1000) if now_ms is None else int(now_ms)
    previous = store.history_cursor(account_id, dialog)
    if previous is None:
        # First run / migration: no historic import without an explicit
        # trusted checkpoint. Future outages can be replayed.
        store.advance_history_cursor(account_id, dialog, now)
        return RecoveryResult(complete=True, initialized=True)
    if previous > now:
        return RecoveryResult(error="clock_moved_back")

    scan = store.history_scan(account_id, dialog)
    if scan is None:
        if previous == now:
            return RecoveryResult(complete=True)
        scan = store.start_history_scan(
            account_id, dialog, now, max(0, previous - OVERLAP_MS)
        )
    snapshot_ms, cutoff_ms, before_ms = scan
    if snapshot_ms > now or snapshot_ms < previous:
        return RecoveryResult(error="clock_moved_back")

    inspected = 0
    recovered = 0
    for _ in range(max_pages):
        try:
            messages = await asyncio.wait_for(
                client.fetch_history(
                    chat_id=chat_id, from_time=before_ms,
                    backward=page_size, forward=0, get_messages=True,
                ),
                timeout=request_timeout,
            )
        except Exception:
            # Never include MAX exception text in the UI or logs.
            return RecoveryResult(
                scanned=inspected, recovered=recovered,
                error="history_request_failed",
            )

        if not isinstance(messages, list) or len(messages) > page_size:
            return RecoveryResult(
                scanned=inspected, recovered=recovered,
                error="unexpected_page",
            )
        if not messages:
            store.complete_history_scan(account_id, dialog, snapshot_ms)
            return RecoveryResult(
                scanned=inspected, recovered=recovered, complete=True,
            )

        times = [_message_time_ms(getattr(m, "time", None)) for m in messages]
        if any(t is None for t in times):
            return RecoveryResult(
                scanned=inspected, recovered=recovered,
                error="invalid_message_time",
            )

        inspected += len(messages)
        oldest = min(times)
        if oldest >= before_ms:
            return RecoveryResult(
                scanned=inspected, recovered=recovered,
                error="history_no_progress",
            )

        for message, timestamp in sorted(
            zip(messages, times), key=lambda entry: entry[1]
        ):
            if not (cutoff_ms <= timestamp <= snapshot_ms):
                continue
            mid = getattr(message, "id", None)
            from_chat = getattr(message, "chat_id", None)
            sender = getattr(message, "sender", None)
            if mid is None or (from_chat is not None and from_chat != chat_id):
                continue
            if sender is None or sender == account_id:
                continue
            if not TEST_MARKER.fullmatch(getattr(message, "text", "") or ""):
                continue
            if store.observe(chat_id, mid, count_duplicate=False):
                recovered += 1

        # If the page is shorter than requested, or the oldest item is
        # earlier than the known boundary, the snapshot is fully scanned.
        if len(messages) < page_size or oldest <= cutoff_ms:
            store.complete_history_scan(account_id, dialog, snapshot_ms)
            return RecoveryResult(
                scanned=inspected, recovered=recovered, complete=True,
            )

        # +1 retains an overlap of the oldest millisecond to avoid dropping
        # messages on page boundaries. If a timestamp is too crowded to
        # advance, stop safely instead of silently skipping messages.
        next_before = oldest + 1
        if not cutoff_ms < next_before < before_ms:
            return RecoveryResult(
                scanned=inspected, recovered=recovered,
                error="history_timestamp_collision",
            )
        store.save_history_scan_position(
            account_id, dialog, snapshot_ms, next_before
        )
        before_ms = next_before

    return RecoveryResult(
        scanned=inspected, recovered=recovered,
        error="history_limit_reached",
    )
