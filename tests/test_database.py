"""
Unit tests for bagfd.database — no network required.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from bagfd.database import (
    clear_defer,
    get_game_files,
    get_table_name,
    get_stored_version,
    init_database,
    save_game_files,
    set_defer,
    should_check_version,
    update_version,
)


# ---------------------------------------------------------------------------
# get_table_name
# ---------------------------------------------------------------------------

class TestGetTableName:
    def test_global_android(self):
        assert get_table_name("global-android") == "global_android"

    def test_japan_android(self):
        assert get_table_name("japan-android") == "japan_android"

    def test_japan_windows(self):
        assert get_table_name("japan-windows") == "japan_windows"

    def test_unknown_returns_empty(self):
        assert get_table_name("unknown-platform") == ""


# ---------------------------------------------------------------------------
# init_database
# ---------------------------------------------------------------------------

class TestInitDatabase:
    def test_creates_versions_table(self, tmp_path):
        db = tmp_path / "test.db"
        init_database(db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "versions" in tables

    def test_creates_platform_tables(self, tmp_path):
        db = tmp_path / "test.db"
        init_database(db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "global_android" in tables
        assert "japan_android" in tables
        assert "japan_windows" in tables

    def test_idempotent(self, tmp_path):
        db = tmp_path / "test.db"
        init_database(db)
        init_database(db)


class TestDatabasePragmasAndSchema:
    def test_wal_and_auto_vacuum_persisted(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        conn = sqlite3.connect(db)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 1  # FULL
        conn.close()

    def test_path_is_pk_no_autoincrement(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        conn = sqlite3.connect(db)
        cols = {c[1]: c for c in conn.execute("PRAGMA table_info(global_android)").fetchall()}
        assert "id" not in cols          # surrogate id dropped
        assert cols["path"][5] == 1      # path is the primary key
        seq = conn.execute("SELECT name FROM sqlite_master WHERE name='sqlite_sequence'").fetchall()
        conn.close()
        assert seq == []                 # no AUTOINCREMENT → no sqlite_sequence

    def test_save_then_read_roundtrip(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        table = get_table_name("global-android")
        save_game_files(db, table, [
            ("Android/a.bundle", "https://cdn/a", "md5", "h1", 10, None),
        ])
        rows = get_game_files(db, table)
        assert rows == [("Android/a.bundle", "https://cdn/a", "md5", "h1", 10, None)]


# ---------------------------------------------------------------------------
# should_check_version / update_version / get_stored_version
# ---------------------------------------------------------------------------

class TestVersionLogic:
    @pytest.fixture
    def db(self, tmp_path):
        path = tmp_path / "test.db"
        init_database(path)
        return path

    def test_no_record_returns_true(self, db):
        assert should_check_version(db, "global-android") is True

    def test_recent_check_returns_false(self, db):
        update_version(db, "global-android", "1.0.0")
        assert should_check_version(db, "global-android", check_interval=timedelta(hours=4)) is False

    def test_old_check_returns_true(self, db):
        old_time = (datetime.now() - timedelta(hours=5)).isoformat()
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO versions (platform, version, last_check, last_update) VALUES (?, ?, ?, ?)",
            ("global-android", "1.0.0", old_time, old_time),
        )
        conn.commit()
        conn.close()
        assert should_check_version(db, "global-android", check_interval=timedelta(hours=4)) is True

    def test_force_returns_true(self, db):
        update_version(db, "global-android", "1.0.0")
        assert should_check_version(db, "global-android", force=True) is True

    def test_update_and_get_stored_version(self, db):
        update_version(db, "global-android", "1.2.3")
        assert get_stored_version(db, "global-android") == "1.2.3"

    def test_get_stored_version_missing_returns_none(self, db):
        assert get_stored_version(db, "global-android") is None

    def test_update_version_overwrites(self, db):
        update_version(db, "global-android", "1.0.0")
        update_version(db, "global-android", "2.0.0", is_new_version=True)
        assert get_stored_version(db, "global-android") == "2.0.0"


# ---------------------------------------------------------------------------
# defer window: set_defer / clear_defer / should_check_version interaction
# ---------------------------------------------------------------------------

def _insert_due_row(db, platform="japan-android"):
    """Insert a version row whose last_check is old enough to be due."""
    old = (datetime.now() - timedelta(hours=5)).isoformat()
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO versions (platform, version, last_check, last_update) VALUES (?, ?, ?, ?)",
        (platform, "1.0.0", old, old),
    )
    conn.commit()
    conn.close()


def _read_defer(db, platform="japan-android"):
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT defer_until FROM versions WHERE platform = ?", (platform,)).fetchone()
    conn.close()
    return row[0] if row else None


class TestDeferLogic:
    @pytest.fixture
    def db(self, tmp_path):
        path = tmp_path / "test.db"
        init_database(path)
        return path

    def test_active_defer_blocks_otherwise_due_check(self, db):
        _insert_due_row(db)
        # due by interval on its own
        assert should_check_version(db, "japan-android", check_interval=timedelta(hours=4)) is True
        # an active defer window holds the check off
        set_defer(db, "japan-android", datetime.now() + timedelta(minutes=10))
        assert should_check_version(db, "japan-android", check_interval=timedelta(hours=4)) is False

    def test_expired_defer_falls_back_to_interval(self, db):
        _insert_due_row(db)
        set_defer(db, "japan-android", datetime.now() - timedelta(minutes=1))  # already past
        assert should_check_version(db, "japan-android", check_interval=timedelta(hours=4)) is True

    def test_force_overrides_active_defer(self, db):
        update_version(db, "japan-android", "1.0.0")
        set_defer(db, "japan-android", datetime.now() + timedelta(minutes=10))
        assert should_check_version(db, "japan-android", force=True) is True

    def test_successful_update_clears_defer(self, db):
        update_version(db, "japan-android", "1.0.0")
        set_defer(db, "japan-android", datetime.now() + timedelta(minutes=10))
        update_version(db, "japan-android", "1.1.0", is_new_version=True)
        assert _read_defer(db) is None

    def test_clear_defer(self, db):
        update_version(db, "japan-android", "1.0.0")
        set_defer(db, "japan-android", datetime.now() + timedelta(minutes=10))
        clear_defer(db, "japan-android")
        assert _read_defer(db) is None

    def test_set_defer_noop_without_row(self, db):
        # no catalog row yet -> nothing stale to serve -> defer is a no-op
        set_defer(db, "japan-android", datetime.now() + timedelta(minutes=10))
        assert should_check_version(db, "japan-android") is True


class TestDeferMigration:
    def test_adds_defer_until_to_legacy_db(self, tmp_path):
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """CREATE TABLE versions (
                platform TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                last_check TIMESTAMP NOT NULL,
                last_update TIMESTAMP NOT NULL
            )"""
        )
        conn.commit()
        conn.close()

        init_database(db)  # should ALTER the missing column in

        conn = sqlite3.connect(db)
        cols = {c[1] for c in conn.execute("PRAGMA table_info(versions)").fetchall()}
        conn.close()
        assert "defer_until" in cols
