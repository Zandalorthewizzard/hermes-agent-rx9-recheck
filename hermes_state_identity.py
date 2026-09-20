"""Copy-stable lineage identity for ``SessionDB`` state databases."""

import sqlite3
import uuid
from typing import Any


_DB_INSTANCE_ID_KEY = "db_instance_id"


class SessionIdentityMixin:
    """Provide the generic durable StateDB lineage identity contract."""

    @staticmethod
    def _validated_db_instance_id(value: Any, db_path) -> str:
        if isinstance(value, bytes):
            try:
                value = value.decode("ascii")
            except UnicodeDecodeError:
                value = None
        if not isinstance(value, str):
            raise StateDbIdentityUnavailable(
                db_path,
                "state_meta.db_instance_id is absent or not a string",
            )
        try:
            parsed = uuid.UUID(value)
        except (AttributeError, ValueError, TypeError):
            parsed = None
        if parsed is None or str(parsed) != value:
            raise StateDbIdentityUnavailable(
                db_path,
                "state_meta.db_instance_id is not a canonical lowercase UUID",
            )
        return value

    def _db_instance_id_from_conn(self, conn: sqlite3.Connection) -> str:
        try:
            row = conn.execute(
                "SELECT value FROM state_meta WHERE key = ? LIMIT 1",
                (_DB_INSTANCE_ID_KEY,),
            ).fetchone()
        except UnicodeDecodeError:
            # A corrupt unrelated sqlite_master text value can make pysqlite
            # fail while decoding an otherwise independent SELECT. Retry this
            # read with bytes so identity validation remains typed and does not
            # make read-only callers fail on an FTS/schema diagnostic.
            text_factory = conn.text_factory
            conn.text_factory = bytes
            try:
                row = conn.execute(
                    "SELECT value FROM state_meta WHERE key = ? LIMIT 1",
                    (_DB_INSTANCE_ID_KEY,),
                ).fetchone()
            finally:
                conn.text_factory = text_factory
        if row is None:
            raise StateDbIdentityUnavailable(
                self.db_path,
                "state_meta.db_instance_id is missing",
            )
        return self._validated_db_instance_id(row[0], self.db_path)

    def _ensure_db_instance_id(self, cursor: sqlite3.Cursor) -> str:
        """Ensure a writable schema init has exactly one valid lineage identity row."""
        row = cursor.execute(
            "SELECT value FROM state_meta WHERE key = ? LIMIT 1",
            (_DB_INSTANCE_ID_KEY,),
        ).fetchone()
        if row is None:
            cursor.execute(
                "INSERT OR IGNORE INTO state_meta (key, value) VALUES (?, ?)",
                (_DB_INSTANCE_ID_KEY, str(uuid.uuid4())),
            )
            row = cursor.execute(
                "SELECT value FROM state_meta WHERE key = ? LIMIT 1",
                (_DB_INSTANCE_ID_KEY,),
            ).fetchone()
        return self._validated_db_instance_id(row[0], self.db_path)

    def _load_db_instance_id(self) -> str:
        """Validate identity during open without changing a read-only database."""
        try:
            return self._db_instance_id_from_conn(self._conn)
        except UnicodeDecodeError:
            if not self.read_only:
                raise
            # sqlite may decode an unrelated malformed sqlite_master row while
            # preparing this SELECT. Keep the read-only handle available for
            # non-identity reads; db_instance_id() reports a typed refusal.
            return ""

    def db_instance_id(self) -> str:
        """Return this StateDB's durable, copy-stable lineage identity.

        This is an opaque lineage value, not an immutable file identity, security
        credential, host/owner identity, or replacement/rollback detector. Copies
        and backup restores intentionally retain it; filesystem path, inode, device,
        and ``PRAGMA application_id`` are not used as fallbacks.
        """
        with self._lock:
            if self._conn is None:
                self._reopen_after_close_locked(context="identity")
            try:
                return self._db_instance_id_from_conn(self._conn)
            except UnicodeDecodeError as exc:
                raise StateDbIdentityUnavailable(
                    self.db_path,
                    "state_meta.db_instance_id could not be read because the database schema contains invalid UTF-8",
                ) from exc


class StateDbIdentityUnavailable(RuntimeError):
    """The durable StateDB lineage identity is absent or malformed."""

    def __init__(self, db_path, reason: str):
        self.db_path = db_path
        self.reason = reason
        super().__init__(f"StateDB lineage identity unavailable for {db_path}: {reason}")
