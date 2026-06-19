"""
Unit tests for bagfd — no network required.
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from bagfd import BlueArchiveGameFilesDownloader, FileInfo, PackInfo, TooManyFilesError
from bagfd.database import (
    clear_platform_db,
    get_table_name,
    init_database,
    get_stored_version,
    save_game_files,
    should_check_version,
    update_version,
)
from bagfd.filter import FileFilter


# ---------------------------------------------------------------------------
# FileFilter
# ---------------------------------------------------------------------------

class TestFileFilterAutoDetect:
    def test_glob_star(self):
        assert FileFilter.auto_detect('*.bundle') == 'glob'

    def test_glob_question(self):
        assert FileFilter.auto_detect('file?.txt') == 'glob'

    def test_glob_bracket(self):
        assert FileFilter.auto_detect('file[0-9].txt') == 'glob'

    def test_regex_caret(self):
        assert FileFilter.auto_detect('^ch') == 'regex'

    def test_regex_dollar(self):
        assert FileFilter.auto_detect('bundle$') == 'regex'

    def test_regex_backslash(self):
        assert FileFilter.auto_detect('\\d+') == 'regex'

    def test_regex_plus(self):
        assert FileFilter.auto_detect('ch+') == 'regex'

    def test_regex_pipe(self):
        assert FileFilter.auto_detect('a|b') == 'regex'

    def test_regex_paren(self):
        assert FileFilter.auto_detect('(abc)') == 'regex'

    def test_contains_fallback(self):
        assert FileFilter.auto_detect('ch0230') == 'contains'


class TestFileFilterMatches:
    def test_glob(self):
        f = FileFilter('*.bundle', 'glob')
        assert f.matches('foo.bundle')
        assert not f.matches('foo.txt')

    def test_glob_auto(self):
        f = FileFilter('*.bundle')
        assert f.matches('foo.bundle')

    def test_regex(self):
        f = FileFilter('^ch\\d+', 'regex')
        assert f.matches('ch0230_something')
        assert not f.matches('Image_ch0230')

    def test_regex_auto(self):
        f = FileFilter('^ch\\d+')
        assert f.matches('ch0230_foo')

    def test_contains(self):
        f = FileFilter('ch0230', 'contains')
        assert f.matches('Image_ch0230_HD')
        assert not f.matches('ch0231')

    def test_contains_auto(self):
        f = FileFilter('ch0230')
        assert f.matches('ch0230_foo.bundle')

    def test_starts_with(self):
        f = FileFilter('Image_', 'starts_with')
        assert f.matches('Image_CueSheet_001')
        assert not f.matches('ch0230_Image_')

    def test_ends_with(self):
        f = FileFilter('.bundle', 'ends_with')
        assert f.matches('foo.bundle')
        assert not f.matches('foo.bundle.bak')


# ---------------------------------------------------------------------------
# Constructor / data_dir resolution
# ---------------------------------------------------------------------------

class TestDataDir:
    def test_explicit_arg(self, tmp_path):
        d = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        assert d.data_dir == tmp_path
        assert d.db_path == tmp_path / "catalog.db"
        assert d.zip_cache == tmp_path / "zip_cache"

    def test_explicit_arg_creates_directory(self, tmp_path):
        target = tmp_path / "nested" / "data"
        BlueArchiveGameFilesDownloader(data_dir=target)
        assert target.exists()

    def test_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BAGFD_DATA_DIR", str(tmp_path))
        d = BlueArchiveGameFilesDownloader()
        assert d.data_dir == tmp_path

    def test_arg_overrides_env_var(self, tmp_path, tmp_path_factory, monkeypatch):
        env_dir = tmp_path_factory.mktemp("env")
        monkeypatch.setenv("BAGFD_DATA_DIR", str(env_dir))
        d = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        assert d.data_dir == tmp_path

    def test_default_uses_platformdirs(self, tmp_path, monkeypatch):
        monkeypatch.delenv("BAGFD_DATA_DIR", raising=False)
        with patch("platformdirs.user_data_dir", return_value=str(tmp_path)) as mock_udd:
            d = BlueArchiveGameFilesDownloader()
        mock_udd.assert_called_once_with("BAGFD")
        assert d.data_dir == tmp_path

    def test_db_created_in_data_dir(self, tmp_path):
        BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        assert (tmp_path / "catalog.db").exists()

    def test_proxy_sets_session_proxies(self, tmp_path):
        d = BlueArchiveGameFilesDownloader(data_dir=tmp_path, proxy="http://proxy:8080")
        assert d.session.proxies == {'http': 'http://proxy:8080', 'https': 'http://proxy:8080'}

    def test_no_proxy_leaves_session_unset(self, tmp_path):
        d = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        assert not d.session.proxies


# ---------------------------------------------------------------------------
# _resolve_platforms
# ---------------------------------------------------------------------------

class TestResolvePlatforms:
    @pytest.fixture
    def client(self, tmp_path):
        return BlueArchiveGameFilesDownloader(data_dir=tmp_path)

    def test_all(self, client):
        assert client._resolve_platforms('all') == ['global-android', 'japan-android', 'japan-windows']

    def test_single_string(self, client):
        assert client._resolve_platforms('global-android') == ['global-android']

    def test_list(self, client):
        assert client._resolve_platforms(['global-android', 'japan-android']) == ['global-android', 'japan-android']


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
        from bagfd.database import get_game_files
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
# Helpers: seed test data into DB
# ---------------------------------------------------------------------------

def _seed_versions(db_path: Path) -> None:
    """Mark all platforms as recently checked so _ensure_fresh() skips network."""
    for platform in ['global-android', 'japan-android', 'japan-windows']:
        update_version(db_path, platform, '1.0.0')


def _seed_global(db_path: Path) -> None:
    table = get_table_name("global-android")
    files = [
        ("Android/ch0230_foo.bundle", "https://cdn/ch0230_foo.bundle", "md5", "abc", 1024, None),
        ("Android/ch0231_bar.bundle", "https://cdn/ch0231_bar.bundle", "md5", "def", 2048, None),
        ("Android/Image_CueSheet_001.bundle", "https://cdn/Image_CueSheet_001.bundle", "md5", "ghi", 512, None),
    ]
    save_game_files(db_path, table, files)


def _seed_japan(db_path: Path, platform: str = "japan-android") -> None:
    table = get_table_name(platform)
    files = [
        (
            "Pack_ch0230.zip",
            "https://jp/Pack_ch0230.zip",
            "crc32", "111", 8192,
            json.dumps(["ch0230_a.bundle", "ch0230_b.bundle"]),
        ),
        (
            "Pack_Image.zip",
            "https://jp/Pack_Image.zip",
            "crc32", "222", 4096,
            json.dumps(["Image_CueSheet_001.bundle", "Image_CueSheet_002.bundle"]),
        ),
    ]
    save_game_files(db_path, table, files)


# ---------------------------------------------------------------------------
# query()
# ---------------------------------------------------------------------------

class TestQuery:
    @pytest.fixture
    def client(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_global(c.db_path)
        _seed_japan(c.db_path, "japan-android")
        _seed_versions(c.db_path)
        return c

    def test_global_contains_match(self, client):
        results = client.query('ch0230', platform='global-android')
        assert len(results) == 1
        fi = results[0]
        assert fi.name == 'ch0230_foo.bundle'
        assert fi.platform == 'global-android'
        assert fi.size == 1024
        assert fi.pack is None

    def test_global_fileinfo_fields(self, client):
        fi = client.query('ch0230', platform='global-android')[0]
        assert fi.path == 'Android/ch0230_foo.bundle'
        assert fi.url == 'https://cdn/ch0230_foo.bundle'
        assert fi.hash_type == 'md5'
        assert fi.hash_value == 'abc'

    def test_global_glob_match(self, client):
        results = client.query('*.bundle', platform='global-android')
        assert len(results) == 3

    def test_global_no_match(self, client):
        assert client.query('nonexistent', platform='global-android') == []

    def test_japan_contains_match(self, client):
        results = client.query('ch0230', platform='japan-android')
        assert len(results) == 2
        names = {fi.name for fi in results}
        assert names == {'ch0230_a.bundle', 'ch0230_b.bundle'}

    def test_japan_fileinfo_has_pack(self, client):
        results = client.query('ch0230_a.bundle', platform='japan-android', filter_method='contains')
        assert len(results) == 1
        fi = results[0]
        # per-file fields are empty on Japan; the data lives on the pack
        assert fi.path is None
        assert fi.url is None
        assert fi.hash_type is None
        assert fi.size is None
        assert fi.pack is not None
        assert fi.pack.name == 'Pack_ch0230.zip'
        assert fi.pack.url == 'https://jp/Pack_ch0230.zip'
        assert fi.pack.hash_type == 'crc32'
        assert fi.pack.hash_value == '111'
        assert fi.pack.size == 8192

    def test_japan_pack_files_are_names(self, client):
        results = client.query('ch0230_a.bundle', platform='japan-android', filter_method='contains')
        pack = results[0].pack
        # files is the full member list of the zip, as plain strings
        assert pack.files == ['ch0230_a.bundle', 'ch0230_b.bundle']

    def test_rejects_all_platform(self, client):
        with pytest.raises(ValueError):
            client.query('ch0230', platform='all')

    def test_rejects_unknown_platform(self, client):
        with pytest.raises(ValueError):
            client.query('ch0230', platform='switch')


# ---------------------------------------------------------------------------
# download() — guard
# ---------------------------------------------------------------------------

class TestDownloadGuard:
    @pytest.fixture
    def client_51(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        table = get_table_name("global-android")
        files = [
            (f"Android/f{i:03d}.bundle", f"https://cdn/f{i:03d}.bundle", "md5", "x", 1024, None)
            for i in range(51)
        ]
        save_game_files(c.db_path, table, files)
        _seed_versions(c.db_path)
        return c

    def test_raises_over_default_limit(self, client_51, tmp_path):
        with pytest.raises(TooManyFilesError) as exc:
            client_51.download('*.bundle', platform='global-android', output_dir=tmp_path / 'out')
        assert exc.value.count == 51
        assert exc.value.limit == 50

    def test_max_files_none_bypasses(self, client_51, tmp_path):
        with patch('bagfd.client.download_files', return_value=[]):
            client_51.download('*.bundle', platform='global-android',
                               output_dir=tmp_path / 'out', max_files=None)

    def test_within_limit_passes(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_global(c.db_path)   # 3 files
        _seed_versions(c.db_path)
        with patch('bagfd.client.download_files', return_value=[]):
            c.download('*.bundle', platform='global-android', output_dir=tmp_path / 'out')

    def test_custom_limit(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_global(c.db_path)   # 3 files
        _seed_versions(c.db_path)
        with pytest.raises(TooManyFilesError) as exc:
            c.download('*.bundle', platform='global-android',
                       output_dir=tmp_path / 'out', max_files=2)
        assert exc.value.count == 3
        assert exc.value.limit == 2

    def test_get_latest_files_default_limit(self, client_51, tmp_path):
        with pytest.raises(TooManyFilesError) as exc:
            client_51.get_latest_files('*.bundle', platform='global-android',
                                       cache_dir=tmp_path / 'c')
        assert exc.value.count == 51
        assert exc.value.limit == 50

    def test_get_latest_files_none_bypasses(self, client_51, tmp_path):
        with patch('bagfd.client.download_files', return_value=[]):
            client_51.get_latest_files('*.bundle', platform='global-android',
                                       cache_dir=tmp_path / 'c', max_files=None)


# ---------------------------------------------------------------------------
# clean()
# ---------------------------------------------------------------------------

class TestClean:
    @pytest.fixture
    def client(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_global(c.db_path)
        _seed_japan(c.db_path, "japan-android")
        ga_dir = c.data_dir / 'download_cache' / 'global-android'
        ga_dir.mkdir(parents=True)
        (ga_dir / 'ch0230_foo.bundle').write_bytes(b'data')
        zip_dir = c.zip_cache / 'global-android'
        zip_dir.mkdir(parents=True)
        (zip_dir / 'Pack_x.zip').write_bytes(b'zip')
        return c

    def test_clean_removes_cached_files_but_keeps_dir(self, client):
        ga_dir = client.data_dir / 'download_cache' / 'global-android'
        client.clean(platform='global-android')
        assert ga_dir.exists()                              # folder kept
        assert not (ga_dir / 'ch0230_foo.bundle').exists()  # contents gone

    def test_clean_clears_zip_cache_contents(self, client):
        zip_dir = client.zip_cache / 'global-android'
        client.clean(platform='global-android')
        assert zip_dir.exists()
        assert not (zip_dir / 'Pack_x.zip').exists()

    def test_clean_leaves_cache_dir_and_siblings(self, client):
        cache_root = client.data_dir / 'download_cache'
        (cache_root / 'japan-android').mkdir(parents=True)
        (cache_root / 'japan-android' / 'keep.bundle').write_bytes(b'keep')
        client.clean(platform='global-android')
        assert cache_root.exists()                                   # parent untouched
        assert (cache_root / 'japan-android' / 'keep.bundle').exists()  # sibling untouched

    def test_clean_removes_db_entries(self, client):
        client.clean(platform='global-android')
        conn = sqlite3.connect(client.db_path)
        rows = conn.execute("SELECT COUNT(*) FROM global_android").fetchone()[0]
        conn.close()
        assert rows == 0

    def test_clean_removes_version(self, client):
        update_version(client.db_path, 'global-android', '1.0.0')
        client.clean(platform='global-android')
        assert get_stored_version(client.db_path, 'global-android') is None

    def test_clean_all_clears_all_platforms(self, client):
        client.clean(platform='all')
        for platform in ['global-android', 'japan-android', 'japan-windows']:
            conn = sqlite3.connect(client.db_path)
            rows = conn.execute(f"SELECT COUNT(*) FROM {get_table_name(platform)}").fetchone()[0]
            conn.close()
            assert rows == 0

    def test_clean_japan_leaves_global(self, client):
        client.clean(platform='japan-android')
        conn = sqlite3.connect(client.db_path)
        rows = conn.execute("SELECT COUNT(*) FROM global_android").fetchone()[0]
        conn.close()
        assert rows == 3


# ---------------------------------------------------------------------------
# Verify / cache reuse (downloader)
# ---------------------------------------------------------------------------

import hashlib
import zipfile
import zlib

from bagfd.downloader import DownloadItem, _hash_matches, _is_valid_cache
from bagfd.enums import VerifyMethod


class TestHashMatches:
    def test_md5_match(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        assert _hash_matches(f, "md5", hashlib.md5(b"hello").hexdigest())

    def test_md5_case_insensitive(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        assert _hash_matches(f, "MD5", hashlib.md5(b"hello").hexdigest().upper())

    def test_md5_mismatch(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        assert not _hash_matches(f, "md5", "deadbeef")

    def test_crc32_match_unsigned(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        assert _hash_matches(f, "crc32", str(zlib.crc32(b"hello") & 0xFFFFFFFF))

    def test_crc32_match_signed(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        signed = (zlib.crc32(b"hello") & 0xFFFFFFFF) - 2**32
        assert _hash_matches(f, "crc32", str(signed))

    def test_unknown_hash_type(self, tmp_path):
        f = tmp_path / "a.bin"
        f.write_bytes(b"hello")
        assert not _hash_matches(f, "sha256", "whatever")


class TestIsValidCache:
    def _item(self, tmp_path, data=b"hello"):
        f = tmp_path / "a.bin"
        f.write_bytes(data)
        return DownloadItem(
            url="http://x", dest=f, size=len(data),
            hash_type="md5", hash_value=hashlib.md5(data).hexdigest(),
        )

    def test_missing_file_never_valid(self, tmp_path):
        item = DownloadItem(url="http://x", dest=tmp_path / "missing", size=1)
        assert not _is_valid_cache(item, VerifyMethod.NONE)

    def test_none_reuses_if_exists(self, tmp_path):
        assert _is_valid_cache(self._item(tmp_path), VerifyMethod.NONE)

    def test_size_match(self, tmp_path):
        assert _is_valid_cache(self._item(tmp_path), VerifyMethod.SIZE)

    def test_size_mismatch(self, tmp_path):
        item = self._item(tmp_path)
        item.size = 999
        assert not _is_valid_cache(item, VerifyMethod.SIZE)

    def test_hash_match(self, tmp_path):
        assert _is_valid_cache(self._item(tmp_path), VerifyMethod.HASH)

    def test_hash_mismatch(self, tmp_path):
        item = self._item(tmp_path)
        item.hash_value = "deadbeef"
        assert not _is_valid_cache(item, VerifyMethod.HASH)

    def test_hash_falls_back_to_size_when_no_hash(self, tmp_path):
        item = self._item(tmp_path)
        item.hash_type = None
        item.hash_value = None
        assert _is_valid_cache(item, VerifyMethod.HASH)  # size still matches


# ---------------------------------------------------------------------------
# download() / get_latest_files() — delivery (network mocked)
# ---------------------------------------------------------------------------

def _fake_download(items, session, workers=10, show_progress=False, verify=VerifyMethod.HASH, force=False):
    """Stand-in for download_files: writes a stub file at each destination."""
    out = []
    for it in items:
        it.dest.parent.mkdir(parents=True, exist_ok=True)
        it.dest.write_bytes(b'XXXX')
        out.append(it.dest)
    return out


class TestDownloadDelivery:
    @pytest.fixture
    def client(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path / "data")
        _seed_global(c.db_path)
        _seed_japan(c.db_path, "japan-android")
        _seed_versions(c.db_path)
        return c

    def test_global_flat(self, client, tmp_path):
        out = tmp_path / "out"
        with patch('bagfd.client.download_files', side_effect=_fake_download):
            result = client.download('*.bundle', platform='global-android', output_dir=out, max_files=None)
        assert result.count == 3
        assert result.total_bytes == 12  # 3 files * 4 bytes
        assert (out / 'ch0230_foo.bundle').exists()
        assert result.output_dir == out

    def test_global_with_path(self, client, tmp_path):
        out = tmp_path / "out"
        with patch('bagfd.client.download_files', side_effect=_fake_download):
            client.download('ch0230', platform='global-android', output_dir=out, with_path=True)
        assert (out / 'Android' / 'ch0230_foo.bundle').exists()

    def test_japan_extracts_matching_member_to_output(self, client, tmp_path):
        # Pre-place the zip so the (patched, no-op) downloader doesn't hit network.
        zpath = client.zip_cache / 'japan-android' / 'Pack_ch0230.zip'
        zpath.parent.mkdir(parents=True)
        with zipfile.ZipFile(zpath, 'w') as zf:
            zf.writestr('ch0230_a.bundle', b'AAA')
            zf.writestr('ch0230_b.bundle', b'BBB')

        out = tmp_path / "out"
        with patch('bagfd.client.download_files', return_value=[]):
            result = client.download('ch0230_a.bundle', platform='japan-android',
                                     output_dir=out, filter_method='contains')
        assert result.count == 1
        assert (out / 'ch0230_a.bundle').read_bytes() == b'AAA'
        assert not (out / 'ch0230_b.bundle').exists()  # not matched

    def test_get_latest_files_global_returns_cache_paths(self, client, tmp_path):
        cache = tmp_path / "mycache"
        with patch('bagfd.client.download_files', side_effect=_fake_download):
            paths = client.get_latest_files('ch0230', platform='global-android', cache_dir=cache)
        assert paths == [cache / 'global-android' / 'ch0230_foo.bundle']
        assert paths[0].exists()

    def test_japan_dedups_to_smallest_pack(self, tmp_path):
        # same bundle present in two packs of different size -> use the smaller
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path / "data")
        table = get_table_name("japan-android")
        save_game_files(c.db_path, table, [
            ("Big.zip",   "https://jp/Big.zip",   "crc32", "1", 9999, json.dumps(["shared.bundle"])),
            ("Small.zip", "https://jp/Small.zip", "crc32", "2", 10,   json.dumps(["shared.bundle"])),
        ])
        _seed_versions(c.db_path)
        zdir = c.zip_cache / "japan-android"
        zdir.mkdir(parents=True)
        with zipfile.ZipFile(zdir / "Big.zip", "w") as zf:
            zf.writestr("shared.bundle", b"BIG")
        with zipfile.ZipFile(zdir / "Small.zip", "w") as zf:
            zf.writestr("shared.bundle", b"SMALL")

        out = tmp_path / "out"
        with patch('bagfd.client.download_files', return_value=[]):
            res = c.download("shared.bundle", platform="japan-android",
                             output_dir=out, filter_method="contains")
        assert res.count == 1                                    # no duplicate path
        assert (out / "shared.bundle").read_bytes() == b"SMALL"  # smaller pack won


# ---------------------------------------------------------------------------
# CLI query --format rendering
# ---------------------------------------------------------------------------

from bagfd.cli import _render_query


def _global_fi(name="a.bundle"):
    return FileInfo(name=name, platform="global-android", path=f"Android/{name}",
                    url=f"https://cdn/{name}", hash_type="md5", hash_value="h1",
                    size=1024, pack=None)


def _japan_fi(name="x.bundle"):
    pack = PackInfo(name="Pack.zip", url="https://jp/Pack.zip", hash_type="crc32",
                    hash_value="c1", size=8192, files=["x.bundle", "y.bundle"])
    return FileInfo(name=name, platform="japan-android", path=None, url=None,
                    hash_type=None, hash_value=None, size=None, pack=pack)


class TestQueryRender:
    def test_table_global_size_first_then_name(self):
        out = _render_query([_global_fi()], "table")
        line = out.splitlines()[0]
        assert line.endswith("a.bundle")          # name is last
        assert "1.0KB" in line                     # size column shown
        assert out.endswith("1 file(s) found.")

    def test_table_size_column_aligned(self):
        # different magnitudes should right-align to the same column width
        big = _global_fi("big.bundle")
        big.size = 5 * 1024 * 1024
        out = _render_query([_global_fi(), big], "table")
        l1, l2 = out.splitlines()[:2]
        assert l1.index("  ") == l2.index("  ")    # name starts at same column

    def test_table_japan_size_then_pack_then_name(self):
        out = _render_query([_japan_fi()], "table")
        line = out.splitlines()[0]
        assert "8.0KB" in line                       # pack size in the size column
        assert line.index("Pack.zip") < line.index("x.bundle")  # pack before filename
        assert line.endswith("x.bundle")             # filename last

    def test_table_japan_sorted_by_pack_then_name(self):
        pa = PackInfo(name="A.zip", url="u", hash_type="crc32", hash_value="1", size=100, files=[])
        pb = PackInfo(name="B.zip", url="u", hash_type="crc32", hash_value="2", size=200, files=[])
        def jfi(name, pack):
            return FileInfo(name=name, platform="japan-android", path=None, url=None,
                            hash_type=None, hash_value=None, size=None, pack=pack)
        results = [jfi("z.bundle", pb), jfi("b.bundle", pa), jfi("a.bundle", pa)]
        out = _render_query(results, "table")
        order = [ln.split()[-1] for ln in out.splitlines() if ln and "file(s)" not in ln]
        assert order == ["a.bundle", "b.bundle", "z.bundle"]  # A.zip(a,b) then B.zip(z)

    def test_name_one_per_line(self):
        out = _render_query([_global_fi(), _japan_fi()], "name")
        assert out.splitlines() == ["a.bundle", "x.bundle"]

    def test_url_global_direct_japan_pack_deduped(self):
        out = _render_query([_global_fi(), _japan_fi("x.bundle"), _japan_fi("y.bundle")], "url")
        assert out.splitlines() == ["https://cdn/a.bundle", "https://jp/Pack.zip"]

    def test_path_japan_falls_back_to_name(self):
        out = _render_query([_global_fi(), _japan_fi()], "path")
        assert out.splitlines() == ["Android/a.bundle", "x.bundle"]

    def test_json_structure(self):
        data = json.loads(_render_query([_global_fi(), _japan_fi()], "json"))
        assert data[0]["url"] == "https://cdn/a.bundle"
        assert data[0]["pack"] is None
        assert data[1]["size"] is None
        assert data[1]["pack"]["name"] == "Pack.zip"
        assert data[1]["pack"]["files"] == ["x.bundle", "y.bundle"]

    def test_json_empty_is_array(self):
        assert _render_query([], "json") == "[]"


# ---------------------------------------------------------------------------
# CLI match highlighting
# ---------------------------------------------------------------------------

from bagfd.cli import _glob_literals, _highlight, _want_color
from bagfd.filter import FileFilter as _FF

HL, RST = "\033[1;33m", "\033[0m"


def test_glob_literals_extraction():
    assert _glob_literals("*ch0171*") == ["ch0171"]
    assert _glob_literals("ch0171*.bundle") == ["ch0171", ".bundle"]
    assert _glob_literals("file?.txt") == ["file", ".txt"]
    assert _glob_literals("a[0-9]b") == ["a", "b"]
    assert _glob_literals("*") == []


class TestHighlight:
    def _hl(self, name, pattern, method):
        return _highlight(name, _FF(pattern, method))

    def test_contains(self):
        assert self._hl("x_ch0171_y", "ch0171", "contains") == f"x_{HL}ch0171{RST}_y"

    def test_starts_with(self):
        assert self._hl("Image_001", "Image_", "starts_with") == f"{HL}Image_{RST}001"

    def test_ends_with(self):
        assert self._hl("foo.bundle", ".bundle", "ends_with") == f"foo{HL}.bundle{RST}"

    def test_regex(self):
        assert self._hl("ch0171_foo", r"ch\d+", "regex") == f"{HL}ch0171{RST}_foo"

    def test_glob_single_literal(self):
        assert self._hl("x_ch0171.bundle", "*ch0171*", "glob") == f"x_{HL}ch0171{RST}.bundle"

    def test_glob_multi_literal(self):
        out = self._hl("ch0171_x.bundle", "ch0171*.bundle", "glob")
        assert out == f"{HL}ch0171{RST}_x{HL}.bundle{RST}"

    def test_no_match_returns_plain(self):
        assert self._hl("abc", "zzz", "contains") == "abc"


def test_want_color_modes():
    assert _want_color("always") is True
    assert _want_color("never") is False


class TestRenderQueryHighlight:
    def test_name_format_highlights(self):
        fi = _global_fi("x_ch0171.bundle")
        out = _render_query([fi], "name", _FF("ch0171", "contains"))
        assert out == f"x_{HL}ch0171{RST}.bundle"

    def test_table_highlights_name_only(self):
        fi = _global_fi("x_ch0171.bundle")
        out = _render_query([fi], "table", _FF("ch0171", "contains"))
        assert f"{HL}ch0171{RST}" in out

    def test_json_never_colored_even_with_highlight(self):
        fi = _global_fi("x_ch0171.bundle")
        out = _render_query([fi], "json", _FF("ch0171", "contains"))
        assert "\033[" not in out

    def test_no_highlight_when_none(self):
        fi = _global_fi("x_ch0171.bundle")
        out = _render_query([fi], "name", None)
        assert "\033[" not in out
