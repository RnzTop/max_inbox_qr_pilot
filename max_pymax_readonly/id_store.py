"""Store only keyed HMAC fingerprints, never MAX IDs or message texts."""
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time


class SeenIds:
    def __init__(self, directory):
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        keypath = folder / "key.bin"
        try:
            fd = os.open(str(keypath), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
        self.key = keypath.read_bytes()
        if len(self.key) != 32:
            raise RuntimeError("Invalid local test key")
        os.chmod(keypath, 0o600)
        db = folder / "seen.sqlite3"
        self.conn = sqlite3.connect(db)
        os.chmod(db, 0o600)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS seen (
            pair_hash TEXT PRIMARY KEY, first_seen INTEGER NOT NULL
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS test_binding (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            scope_mac TEXT NOT NULL,
            chat_mac TEXT NOT NULL
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS history_progress (
            slot INTEGER PRIMARY KEY CHECK (slot = 1),
            scope_mac TEXT NOT NULL,
            scanned_through_ms INTEGER NOT NULL
        )""")
        self.conn.commit()
        self.unique = 0
        self.duplicates = 0

    def _binding_mac(self, purpose, *values):
        """Fingerprint scoped to one test account and one configured dialog."""
        digest = hmac.new(self.key, purpose.encode("ascii"), hashlib.sha256)
        for value in values:
            encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return digest.hexdigest()

    def get_binding(self, account_id, dialog):
        """Get only a keyed digest; never reveal a raw chat ID."""
        if account_id is None or not dialog:
            return None
        scope = self._binding_mac("pymax-scope-v1", account_id, dialog)
        row = self.conn.execute(
            "SELECT scope_mac, chat_mac FROM test_binding WHERE slot = 1"
        ).fetchone()
        if row is None or not hmac.compare_digest(row[0], scope):
            return None
        return row[1]

    def bind_chat(self, account_id, dialog, chat_id):
        """Persist a verified test-dialog fingerprint in private SQLite."""
        if account_id is None or chat_id is None or not dialog:
            raise ValueError("Cannot bind an unverified test dialog")
        scope = self._binding_mac("pymax-scope-v1", account_id, dialog)
        chat = self._binding_mac("pymax-chat-v1", account_id, dialog, chat_id)
        self.conn.execute(
            "INSERT INTO test_binding(slot, scope_mac, chat_mac) VALUES (1, ?, ?) "
            "ON CONFLICT(slot) DO UPDATE SET "
            "scope_mac = excluded.scope_mac, chat_mac = excluded.chat_mac",
            (scope, chat),
        )
        self.conn.commit()
        return chat

    def is_bound_chat(self, account_id, dialog, chat_id, expected_mac):
        if expected_mac is None or chat_id is None or account_id is None:
            return False
        actual = self._binding_mac("pymax-chat-v1", account_id, dialog, chat_id)
        return hmac.compare_digest(actual, expected_mac)

    def history_cursor(self, account_id, dialog):
        """Last successfully scanned time, scoped to a verified test binding."""
        if self.get_binding(account_id, dialog) is None:
            return None
        scope = self._binding_mac("pymax-scope-v1", account_id, dialog)
        row = self.conn.execute(
            "SELECT scope_mac, scanned_through_ms FROM history_progress WHERE slot = 1"
        ).fetchone()
        if row is None or not hmac.compare_digest(row[0], scope):
            return None
        return row[1]

    def advance_history_cursor(self, account_id, dialog, millis):
        """Persist a completed sweep boundary, never for an unbound account."""
        if self.get_binding(account_id, dialog) is None:
            raise ValueError("Test dialogue must be verified before history use")
        timestamp = int(millis)
        if timestamp <= 0:
            raise ValueError("Invalid cursor")
        previous = self.history_cursor(account_id, dialog)
        if previous is not None and timestamp < previous:
            raise ValueError("History checkpoint cannot move backwards")
        scope = self._binding_mac("pymax-scope-v1", account_id, dialog)
        self.conn.execute(
            "INSERT INTO history_progress(slot, scope_mac, scanned_through_ms) "
            "VALUES (1, ?, ?) ON CONFLICT(slot) DO UPDATE SET "
            "scope_mac = excluded.scope_mac, "
            "scanned_through_ms = excluded.scanned_through_ms",
            (scope, timestamp),
        )
        self.conn.commit()

    def observe(self, chat_id, message_id):
        if chat_id is None or message_id is None:
            return False
        # Canonical string form; no string/number precision loss for large IDs.
        digest = hmac.new(
            self.key,
            b"max-pymax-test\0" + str(chat_id).encode("ascii") + b"\0"
            + str(message_id).encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        result = self.conn.execute(
            "INSERT OR IGNORE INTO seen(pair_hash, first_seen) VALUES (?, ?)",
            (digest, int(time.time())),
        )
        self.conn.commit()
        fresh = result.rowcount == 1
        if fresh:
            self.unique += 1
        else:
            self.duplicates += 1
        return fresh

    def count(self):
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
