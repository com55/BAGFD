"""
Unit tests for bagfd.database — no network required.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from bagfd.database import (
    get_game_files,
    get_table_name,
    get_stored_version,
    init_database,
    save_game_files,
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
