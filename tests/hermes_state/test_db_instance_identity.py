"""H1 contract tests for SessionDB's copy-stable lineage identity."""

import shutil
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hermes_state
from hermes_state import SessionDB


StateDbIdentityUnavailable = getattr(
    hermes_state,
    "StateDbIdentityUnavailable",
    type("StateDbIdentityUnavailable", (RuntimeError,), {}),
)


def _assert_valid_uuid(value: str) -> None:
    assert str(uuid.UUID(value)) == value


def _read_identity(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT value FROM state_meta WHERE key = 'db_instance_id'"
        ).fetchall()


def test_two_writable_openers_share_one_valid_identity(tmp_path):
    db_path = tmp_path / "state.db"
    first = SessionDB(db_path=db_path)
    second = SessionDB(db_path=db_path)
    try:
        first_id = first.db_instance_id()
        second_id = second.db_instance_id()
    finally:
        second.close()
        first.close()

    _assert_valid_uuid(first_id)
    assert second_id == first_id
    assert _read_identity(db_path) == [(first_id,)]


def test_close_reopen_and_wal_checkpoint_preserve_identity(tmp_path):
    db_path = tmp_path / "state.db"
    initial = SessionDB(db_path=db_path)
    identity = initial.db_instance_id()
    initial.create_session("checkpoint", source="cli")
    initial.close()

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    reopened = SessionDB(db_path=db_path)
    try:
        assert reopened.db_instance_id() == identity
    finally:
        reopened.close()


def test_read_only_missing_identity_refuses_without_writing(tmp_path):
    db_path = tmp_path / "state.db"
    writable = SessionDB(db_path=db_path)
    writable.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM state_meta WHERE key = 'db_instance_id'")
        conn.commit()
    before = db_path.read_bytes()

    with pytest.raises(StateDbIdentityUnavailable):
        SessionDB(db_path=db_path, read_only=True)

    assert db_path.read_bytes() == before
    assert _read_identity(db_path) == []

    minted = SessionDB(db_path=db_path)
    try:
        _assert_valid_uuid(minted.db_instance_id())
        assert len(_read_identity(db_path)) == 1
    finally:
        minted.close()


def test_concurrent_writable_openers_converge_to_one_row(tmp_path):
    db_path = tmp_path / "state.db"
    start = threading.Barrier(2)

    def open_and_read():
        start.wait()
        db = SessionDB(db_path=db_path)
        try:
            return db.db_instance_id()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        identities = list(pool.map(lambda _: open_and_read(), range(2)))

    assert identities[0] == identities[1]
    _assert_valid_uuid(identities[0])
    assert _read_identity(db_path) == [(identities[0],)]


def test_copy_preserves_lineage_but_fresh_database_does_not(tmp_path):
    source_path = tmp_path / "source.db"
    source = SessionDB(db_path=source_path)
    source_id = source.db_instance_id()
    source.close()

    copy_path = tmp_path / "copy.db"
    shutil.copy2(source_path, copy_path)
    copied = SessionDB(db_path=copy_path)
    try:
        assert copied.db_instance_id() == source_id
    finally:
        copied.close()

    fresh_path = tmp_path / "fresh.db"
    fresh = SessionDB(db_path=fresh_path)
    try:
        assert fresh.db_instance_id() != source_id
    finally:
        fresh.close()


def test_malformed_identity_is_typed_refusal_and_never_overwritten(tmp_path):
    for malformed in ("", "not-a-uuid"):
        db_path = tmp_path / f"{uuid.uuid4().hex}.db"
        writable = SessionDB(db_path=db_path)
        writable.close()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE state_meta SET value = ? WHERE key = 'db_instance_id'",
                (malformed,),
            )
            conn.commit()

        with pytest.raises(StateDbIdentityUnavailable):
            SessionDB(db_path=db_path, read_only=True)
        with pytest.raises(StateDbIdentityUnavailable):
            SessionDB(db_path=db_path)

        assert _read_identity(db_path) == [(malformed,)]


def test_default_home_profile_a_b_a_reads_each_database(monkeypatch, tmp_path):
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()
    # The hermetic suite pins DEFAULT_DB_PATH at fixture setup. Restore its
    # normal call-time path resolution so this exercises profile A → B → A.
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    first_a = SessionDB()
    try:
        identity_a = first_a.db_instance_id()
    finally:
        first_a.close()

    monkeypatch.setenv("HERMES_HOME", str(home_b))
    only_b = SessionDB()
    try:
        identity_b = only_b.db_instance_id()
    finally:
        only_b.close()

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    second_a = SessionDB()
    try:
        assert second_a.db_instance_id() == identity_a
    finally:
        second_a.close()

    assert identity_a != identity_b
    _assert_valid_uuid(identity_a)
    _assert_valid_uuid(identity_b)
