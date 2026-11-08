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
        self.conn.commit()
        self.unique = 0
        self.duplicates = 0

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
