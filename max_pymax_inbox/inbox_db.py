"""Durable, private outbox for MAX customer messages.

Source IDs are HMAC digests. Message text is retained locally only until
operator deletes the add-on data; protect HA /data and backups accordingly.
"""
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time


class InboxDB:
    def __init__(self, directory):
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
        key_path = folder / "key.bin"
        try:
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(secrets.token_bytes(32))
        self.key = key_path.read_bytes()
        if len(self.key) != 32:
            raise RuntimeError("Invalid inbox HMAC key")
        os.chmod(key_path, 0o600)
        db_path = folder / "inbox.sqlite3"
        self.conn = sqlite3.connect(db_path, timeout=20)
        os.chmod(db_path, 0o600)
        self.conn.execute("PRAGMA busy_timeout=20000")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS scopes (
            scope TEXT PRIMARY KEY, baseline_ms INTEGER NOT NULL
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS chat_scans (
            scope TEXT NOT NULL,
            chat_mac TEXT NOT NULL,
            complete_ms INTEGER NOT NULL,
            snapshot_ms INTEGER,
            cutoff_ms INTEGER,
            before_ms INTEGER,
            PRIMARY KEY (scope, chat_mac)
        )""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS outbox (
            fingerprint TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            body TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('queued','sending','sent','uncertain','failed')
            ),
            created_ms INTEGER NOT NULL,
            sent_mid TEXT
        )""")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS inbox_queued ON outbox(scope,state,created_ms)"
        )
        self.conn.commit()
        # An interrupted outbound POST might already have succeeded.
        # Never automatically retry ambiguous interrupted requests.
        self.conn.execute(
            "UPDATE outbox SET state='uncertain' WHERE state='sending'"
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    def fingerprint(self, purpose, *parts):
        digest = hmac.new(self.key, purpose.encode("ascii"), hashlib.sha256)
        for p in parts:
            raw = str(p).encode("utf-8")
            digest.update(len(raw).to_bytes(4, "big"))
            digest.update(raw)
        return digest.hexdigest()

    def scope(self, account_id, bot_id, work_chat_id, baseline_ms=None):
        if account_id is None or bot_id is None or work_chat_id is None:
            raise ValueError("Invalid account or bot identity")
        scope = self.fingerprint(
            "max-production-scope-v1", account_id, bot_id, work_chat_id
        )
        if baseline_ms is None:
            baseline_ms = int(time.time() * 1000)
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO scopes(scope,baseline_ms) VALUES (?,?)",
                (scope, int(baseline_ms)),
            )
        return scope

    def baseline(self, scope):
        row = self.conn.execute(
            "SELECT baseline_ms FROM scopes WHERE scope=?", (scope,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown account scope")
        return row[0]

    def enqueue(self, scope, chat_id, message_id, body):
        if not body or chat_id is None or message_id is None:
            return False
        self.baseline(scope)
        digest = self.fingerprint(
            "max-production-message-v1", scope, chat_id, message_id
        )
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO outbox "
                "(fingerprint,scope,body,state,created_ms) "
                "VALUES (?,?,?,'queued',?)",
                (digest, scope, body, int(time.time() * 1000)),
            )
        return cur.rowcount == 1

    def reserve(self, scope):
        with self.conn:
            row = self.conn.execute(
                "SELECT fingerprint,body FROM outbox "
                "WHERE scope=? AND state='queued' ORDER BY created_ms, rowid LIMIT 1",
                (scope,),
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE outbox SET state='sending' WHERE fingerprint=? "
                "AND state='queued'", (row[0],),
            )
            return row

    def finish(self, fingerprint, status, sent_mid=None):
        if status not in ("queued", "sent", "uncertain", "failed"):
            raise ValueError("Invalid delivery state")
        with self.conn:
            changed = self.conn.execute(
                "UPDATE outbox SET state=?, sent_mid=? "
                "WHERE fingerprint=? AND state='sending'",
                (status, sent_mid, fingerprint),
            ).rowcount
        return changed == 1

    def counts(self, scope):
        rows = self.conn.execute(
            "SELECT state,COUNT(*) FROM outbox WHERE scope=? GROUP BY state",
            (scope,),
        ).fetchall()
        counts = {
            "queued": 0, "sending": 0, "sent": 0,
            "uncertain": 0, "failed": 0,
        }
        for state, count in rows:
            counts[state] = count
        return counts

    def scan_position(self, scope, chat_id):
        chat_mac = self.fingerprint("max-production-chat-v1", scope, chat_id)
        row = self.conn.execute(
            "SELECT complete_ms,snapshot_ms,cutoff_ms,before_ms "
            "FROM chat_scans WHERE scope=? AND chat_mac=?",
            (scope, chat_mac),
        ).fetchone()
        return row or (self.baseline(scope), None, None, None)

    def begin_scan(self, scope, chat_id, snapshot_ms, overlap_ms=5000):
        cursor, running, cutoff, before = self.scan_position(scope, chat_id)
        if running is not None:
            return cursor, running, cutoff, before
        snapshot_ms = int(snapshot_ms)
        if snapshot_ms < cursor:
            raise ValueError("Clock moved backwards")
        if snapshot_ms == cursor:
            return cursor, None, None, None
        cutoff = max(0, cursor - overlap_ms)
        before = snapshot_ms + 1
        chat_mac = self.fingerprint("max-production-chat-v1", scope, chat_id)
        with self.conn:
            self.conn.execute(
                "INSERT INTO chat_scans "
                "(scope,chat_mac,complete_ms,snapshot_ms,cutoff_ms,before_ms) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(scope,chat_mac) DO UPDATE SET "
                "snapshot_ms=excluded.snapshot_ms,"
                "cutoff_ms=excluded.cutoff_ms,before_ms=excluded.before_ms",
                (scope, chat_mac, cursor, snapshot_ms, cutoff, before),
            )
        return cursor, snapshot_ms, cutoff, before

    def progress_scan(self, scope, chat_id, snapshot_ms, before_ms):
        chat_mac = self.fingerprint("max-production-chat-v1", scope, chat_id)
        cursor, snap, cutoff, before = self.scan_position(scope, chat_id)
        pos = int(before_ms)
        if (snap != snapshot_ms or before is None
                or not cutoff < pos < before):
            raise ValueError("Unsafe history page checkpoint")
        with self.conn:
            self.conn.execute(
                "UPDATE chat_scans SET before_ms=? "
                "WHERE scope=? AND chat_mac=?",
                (pos, scope, chat_mac),
            )

    def complete_scan(self, scope, chat_id, snapshot_ms):
        chat_mac = self.fingerprint("max-production-chat-v1", scope, chat_id)
        cursor, snap, _, _ = self.scan_position(scope, chat_id)
        if snap != snapshot_ms or snapshot_ms < cursor:
            raise ValueError("Invalid scan completion")
        with self.conn:
            self.conn.execute(
                "UPDATE chat_scans SET complete_ms=?,snapshot_ms=NULL,"
                "cutoff_ms=NULL,before_ms=NULL WHERE scope=? AND chat_mac=?",
                (snapshot_ms, scope, chat_mac),
            )
