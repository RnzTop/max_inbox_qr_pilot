"""Persistence for test-only stability checks of HMAC-obscured MAX IDs.

This database never stores message contents, tokens or raw chat/message IDs.
The only message-level field on disk is HMAC(key, chat_digest || message_digest).
"""
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time


class IdStability:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        key_path = self.directory / "key.bin"
        try:
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
        self.key = key_path.read_bytes()
        if len(self.key) != 32:
            raise ValueError("Invalid test-only stability key length")
        os.chmod(key_path, 0o600)

        db_path = self.directory / "fingerprints.sqlite3"
        self.conn = sqlite3.connect(db_path)
        os.chmod(db_path, 0o600)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS pushes (
            pair_digest TEXT PRIMARY KEY,
            session_tag TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            confirmed_after_restart INTEGER NOT NULL DEFAULT 0
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS pushes_created ON pushes(created_at)")
        self.conn.execute("DELETE FROM pushes WHERE created_at < ?", (int(time.time()) - 14 * 86400,))
        self.conn.commit()
        self.session_tag = secrets.token_hex(12)
        self.pushes_seen = 0
        self.same_run_matches = 0
        self.restart_matches = 0

    def pair(self, chat_fp, message_fp):
        """Opaque 64-character hex digest; inputs must be HMAC fingerprints."""
        if not all(isinstance(v, str) and len(v) == 64
                   and all(c in "0123456789abcdef" for c in v)
                   for v in (chat_fp, message_fp)):
            return None
        return hmac.new(
            self.key,
            b"max-test-pair\0" + bytes.fromhex(chat_fp) + bytes.fromhex(message_fp),
            hashlib.sha256,
        ).hexdigest()

    def push(self, chat_fp, message_fp):
        p = self.pair(chat_fp, message_fp)
        if p is None:
            return
        self.pushes_seen += 1
        self.conn.execute(
            "INSERT OR IGNORE INTO pushes(pair_digest, session_tag, created_at) VALUES (?, ?, ?)",
            (p, self.session_tag, int(time.time())),
        )
        self.conn.commit()

    def history(self, chat_fp, message_fps):
        if not chat_fp or not isinstance(message_fps, list):
            return
        for message_fp in message_fps[:1000]:
            p = self.pair(chat_fp, message_fp)
            if p is None:
                continue
            row = self.conn.execute(
                "SELECT session_tag, confirmed_after_restart FROM pushes WHERE pair_digest=?",
                (p,),
            ).fetchone()
            if row is None:
                continue
            if row[0] == self.session_tag:
                self.same_run_matches += 1
            elif not row[1]:
                self.conn.execute(
                    "UPDATE pushes SET confirmed_after_restart=1 WHERE pair_digest=?",
                    (p,),
                )
                self.conn.commit()
                self.restart_matches += 1

    def summary(self):
        registered, confirmed = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(confirmed_after_restart), 0) FROM pushes"
        ).fetchone()
        return (
            "Стабильность ID (только HMAC-отпечатки): "
            "новых push за запуск={}; сохранено отпечатков={}; "
            "совпадений в этой сессии={}; "
            "совпадений после перезапуска={}; "
            "подтверждено всего={}.").format(
                self.pushes_seen, registered, self.same_run_matches,
                self.restart_matches, confirmed,
            )

    def close(self):
        self.conn.close()
