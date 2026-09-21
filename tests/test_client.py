"""
Unit tests for bagfd.client (BlueArchiveGameFilesDownloader) — no network required.
"""
import hashlib
import json
import sqlite3
import threading
import zipfile
import zlib
from dataclasses import MISSING, asdict, fields
from unittest.mock import patch

import pytest

from bagfd import (
    BlueArchiveGameFilesDownloader,
    FileInfo,
    ResourceUnavailableError,
    TooManyFilesError,
)
from bagfd.database import (
    get_game_files,
    get_stored_version,
    get_table_name,
    save_game_files,
    update_version,
)
from bagfd.cli import _fi_dict
from bagfd.enums import FilterMethod, Platform, Server, VerifyMethod


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

    def test_db_path_overrides_data_dir(self, tmp_path):
        db_dir = tmp_path / "db_elsewhere"
        db_path = db_dir / "catalog.db"
        d = BlueArchiveGameFilesDownloader(data_dir=tmp_path, db_path=db_path)
        assert d.db_path == db_path
        assert db_path.exists()
        assert not (tmp_path / "catalog.db").exists()
        assert d.zip_cache == tmp_path / "zip_cache"

    def test_db_path_env_var(self, tmp_path, monkeypatch):
        db_path = tmp_path / "db_elsewhere" / "catalog.db"
        monkeypatch.setenv("BAGFD_DB_PATH", str(db_path))
        d = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        assert d.db_path == db_path
        assert db_path.exists()

    def test_db_path_arg_overrides_env_var(self, tmp_path, monkeypatch):
        env_path = tmp_path / "env" / "catalog.db"
        arg_path = tmp_path / "arg" / "catalog.db"
        monkeypatch.setenv("BAGFD_DB_PATH", str(env_path))
        d = BlueArchiveGameFilesDownloader(data_dir=tmp_path, db_path=arg_path)
        assert d.db_path == arg_path
        assert not env_path.exists()

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
        assert client._resolve_platforms('all') == [
            'global-android', 'global-ios', 'japan-android', 'japan-ios', 'japan-windows',
        ]

    def test_single_string(self, client):
        assert client._resolve_platforms('global-android') == ['global-android']

    def test_list(self, client):
        assert client._resolve_platforms(['global-android', 'japan-android']) == ['global-android', 'japan-android']

    def test_split_selection(self, client):
        assert client._resolve_platforms('ios', server='global') == ['global-ios']


# ---------------------------------------------------------------------------
# Helpers: seed test data into DB
# ---------------------------------------------------------------------------

def _seed_versions(db_path) -> None:
    """Mark all platforms as recently checked so _ensure_fresh() skips network."""
    for platform in [
        'global-android', 'global-ios', 'japan-android', 'japan-ios', 'japan-windows',
    ]:
        update_version(db_path, platform, '1.0.0')


def _seed_global(db_path) -> None:
    table = get_table_name("global-android")
    files = [
        ("Android/ch0230_foo.bundle", "https://cdn/ch0230_foo.bundle", "md5", "abc", 1024, None),
        ("Android/ch0231_bar.bundle", "https://cdn/ch0231_bar.bundle", "md5", "def", 2048, None),
        ("Android/Image_CueSheet_001.bundle", "https://cdn/Image_CueSheet_001.bundle", "md5", "ghi", 512, None),
    ]
    save_game_files(db_path, table, files)


def _seed_japan(db_path, platform: str = "japan-android") -> None:
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


def _seed_global_ios(db_path, name: str = 'ch0230_ios.bundle') -> None:
    save_game_files(db_path, get_table_name('global-ios'), [
        (f'iOS/{name}', f'https://ios/{name}', 'md5', 'ios-hash', 4, None),
    ])


def _seed_japan_ios(db_path, pack_name: str = 'iOS_Pack.zip') -> None:
    save_game_files(db_path, get_table_name('japan-ios'), [
        (pack_name, f'https://jp/{pack_name}', 'crc32', '123', 12,
         json.dumps(['iOS/ch0230_ios.bundle', 'iOS/other.bundle'])),
    ])


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


class TestSplitClientApi:
    @pytest.fixture
    def client(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_global(c.db_path)
        _seed_versions(c.db_path)
        return c

    def test_old_positional_query_and_split_query_are_equivalent(self, client):
        legacy = client.query('ch0230', Platform.GLOBAL_ANDROID, FilterMethod.CONTAINS, False)
        split = client.query(
            'ch0230', platform=Platform.ANDROID, filter_method=FilterMethod.CONTAINS,
            update_background=False, server=Server.GLOBAL,
        )
        assert split == legacy
        assert split[0].platform == 'global-android'

    def test_old_positional_download_apis_remain_equivalent(self, client, tmp_path):
        legacy_cache = tmp_path / 'legacy-cache'
        split_cache = tmp_path / 'split-cache'
        legacy_out = tmp_path / 'legacy-out'
        split_out = tmp_path / 'split-out'
        with patch('bagfd.client.download_files', side_effect=_fake_download):
            legacy_paths = client.get_latest_files(
                'ch0230', Platform.GLOBAL_ANDROID, legacy_cache, VerifyMethod.NONE,
                FilterMethod.CONTAINS, 1, False, None,
            )
            split_paths = client.get_latest_files(
                'ch0230', Platform.ANDROID, split_cache, VerifyMethod.NONE,
                FilterMethod.CONTAINS, 1, False, None, server=Server.GLOBAL,
            )
            legacy_result = client.download(
                'ch0230', Platform.GLOBAL_ANDROID, legacy_out, True, VerifyMethod.NONE,
                FilterMethod.CONTAINS, 1, False, None,
            )
            split_result = client.download(
                'ch0230', Platform.ANDROID, split_out, True, VerifyMethod.NONE,
                FilterMethod.CONTAINS, 1, False, None, server=Server.GLOBAL,
            )

        assert [path.name for path in legacy_paths] == [path.name for path in split_paths]
        assert [path.read_bytes() for path in legacy_paths] == [path.read_bytes() for path in split_paths]
        assert legacy_result.count == split_result.count == 1
        assert legacy_result.total_bytes == split_result.total_bytes == 4
        assert [path.relative_to(legacy_out) for path in legacy_result.files] == [
            path.relative_to(split_out) for path in split_result.files
        ]

    def test_global_ios_uses_direct_files_and_its_own_cache_namespace(self, client, tmp_path):
        _seed_global_ios(client.db_path)
        update_version(client.db_path, 'global-ios', '1.0.0')
        cache_root = tmp_path / 'cache'
        output_dir = tmp_path / 'out'

        queried = client.query('ch0230_ios.bundle', platform='ios', server='global')[0]
        with patch('bagfd.client.download_files', side_effect=_fake_download):
            cached = client.get_latest_files(
                'ch0230_ios.bundle', platform='ios', server='global', cache_dir=cache_root,
            )
            result = client.download(
                'ch0230_ios.bundle', platform='ios', server='global', output_dir=output_dir,
                with_path=True,
            )

        assert queried.platform == 'global-ios'
        assert queried.path == 'iOS/ch0230_ios.bundle'
        assert queried.url == 'https://ios/ch0230_ios.bundle'
        assert queried.server is Server.GLOBAL
        assert queried.device_platform is Platform.IOS
        assert cached == [cache_root / 'global-ios' / 'ch0230_ios.bundle']
        assert cached[0].read_bytes() == b'XXXX'
        assert result.files == [output_dir / 'iOS' / 'ch0230_ios.bundle']
        assert result.files[0].read_bytes() == b'XXXX'
        assert not (client.zip_cache / 'global-ios').exists()

    def test_japan_ios_extracts_only_matching_members_to_cache_and_output(self, client, tmp_path):
        _seed_japan_ios(client.db_path)
        update_version(client.db_path, 'japan-ios', '1.0.0')
        zip_path = client.zip_cache / 'japan-ios' / 'iOS_Pack.zip'
        zip_path.parent.mkdir(parents=True)
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('iOS/ch0230_ios.bundle', b'IOS')
            zf.writestr('iOS/other.bundle', b'OTHER')

        cache_root = tmp_path / 'cache'
        output_dir = tmp_path / 'out'
        with patch('bagfd.client.download_files', return_value=[]):
            cached = client.get_latest_files(
                'ch0230_ios.bundle', platform=Platform.IOS, server=Server.JAPAN,
                cache_dir=cache_root,
            )
            result = client.download(
                'ch0230_ios.bundle', platform='ios', server='japan',
                output_dir=output_dir, with_path=True,
            )

        assert cached == [cache_root / 'japan-ios' / 'ch0230_ios.bundle']
        assert cached[0].read_bytes() == b'IOS'
        assert result.files == [output_dir / 'iOS' / 'ch0230_ios.bundle']
        assert result.files[0].read_bytes() == b'IOS'
        assert (client.zip_cache / 'japan-ios' / 'iOS_Pack.zip').exists()

    def test_same_filename_is_cached_separately_for_global_targets(self, client, tmp_path):
        save_game_files(client.db_path, get_table_name('global-android'), [
            ('Android/shared.bundle', 'https://android/shared.bundle', 'md5', 'a', 7, None),
        ])
        save_game_files(client.db_path, get_table_name('global-ios'), [
            ('iOS/shared.bundle', 'https://ios/shared.bundle', 'md5', 'i', 2, None),
        ])
        update_version(client.db_path, 'global-ios', '1.0.0')

        def write_by_url(items, session, workers=10, show_progress=False, verify=VerifyMethod.HASH,
                         force=False, locks=None):
            delivered = []
            for item in items:
                item.dest.parent.mkdir(parents=True, exist_ok=True)
                item.dest.write_bytes(item.url.removeprefix('https://').encode())
                delivered.append(item.dest)
            return delivered

        cache_root = tmp_path / 'shared-cache'
        with patch('bagfd.client.download_files', side_effect=write_by_url):
            android = client.get_latest_files('shared.bundle', platform='global-android', cache_dir=cache_root)
            ios = client.get_latest_files('shared.bundle', platform='ios', server='global', cache_dir=cache_root)

        assert android == [cache_root / 'global-android' / 'shared.bundle']
        assert ios == [cache_root / 'global-ios' / 'shared.bundle']
        assert android[0].read_bytes() == b'android/shared.bundle'
        assert ios[0].read_bytes() == b'ios/shared.bundle'

    def test_japan_ios_query_uses_singleton_and_preserves_other_catalogs(self, client):
        _seed_japan(client.db_path, 'japan-android')
        _seed_japan_ios(client.db_path)
        _seed_japan(client.db_path, 'japan-windows')
        update_version(client.db_path, 'japan-ios', '1.0.0')
        update_version(client.db_path, 'japan-windows', '1.0.0')
        android_before = get_game_files(client.db_path, get_table_name('japan-android'))
        android_version = get_stored_version(client.db_path, 'japan-android')
        windows_before = get_game_files(client.db_path, get_table_name('japan-windows'))
        windows_version = get_stored_version(client.db_path, 'japan-windows')

        with patch('bagfd.client.fetch_japan_servers', return_value={'japan-ios': False}) as fetch:
            results = client.query(
                'ch0230_ios.bundle', platform='ios', server='japan',
                filter_method=FilterMethod.CONTAINS,
            )

        fetch.assert_called_once_with(
            client.session, client.db_path, False, requested_targets=['japan-ios'],
        )
        assert [result.name for result in results] == ['iOS/ch0230_ios.bundle']
        assert get_game_files(client.db_path, get_table_name('japan-android')) == android_before
        assert get_stored_version(client.db_path, 'japan-android') == android_version
        assert get_game_files(client.db_path, get_table_name('japan-windows')) == windows_before
        assert get_stored_version(client.db_path, 'japan-windows') == windows_version

    def test_update_prunes_only_the_selected_target(self, client):
        keep = b'KEEP'
        stale = b'STALE'
        save_game_files(client.db_path, get_table_name('global-ios'), [
            ('iOS/keep.bundle', 'https://ios/keep.bundle', 'md5', hashlib.md5(keep).hexdigest(), len(keep), None),
            ('iOS/stale.bundle', 'https://ios/stale.bundle', 'md5', hashlib.md5(stale).hexdigest(), len(stale), None),
        ])
        ios_cache = client.data_dir / 'download_cache' / 'global-ios'
        ios_cache.mkdir(parents=True)
        (ios_cache / 'keep.bundle').write_bytes(keep)
        (ios_cache / 'stale.bundle').write_bytes(stale)
        android_cache = client.data_dir / 'download_cache' / 'global-android'
        android_cache.mkdir(parents=True)
        (android_cache / 'keep.bundle').write_bytes(b'android')
        android_rows = get_game_files(client.db_path, get_table_name('global-android'))
        android_version = get_stored_version(client.db_path, 'global-android')

        def changed(session, db_path, force=False, check_interval=None, *, target):
            assert target == 'global-ios'
            save_game_files(db_path, get_table_name(target), [
                ('iOS/keep.bundle', 'https://ios/keep.bundle', 'md5', hashlib.md5(keep).hexdigest(), len(keep), None),
            ])
            return True

        with patch('bagfd.client.fetch_global', side_effect=changed):
            client.update(server='global', platform='ios')

        assert (ios_cache / 'keep.bundle').exists()
        assert not (ios_cache / 'stale.bundle').exists()
        assert get_game_files(client.db_path, get_table_name('global-android')) == android_rows
        assert get_stored_version(client.db_path, 'global-android') == android_version
        assert (android_cache / 'keep.bundle').read_bytes() == b'android'

    def test_clean_only_clears_the_selected_target(self, client):
        _seed_global_ios(client.db_path)
        update_version(client.db_path, 'global-ios', '1.0.0')
        ios_file = client.data_dir / 'download_cache' / 'global-ios' / 'ch0230_ios.bundle'
        ios_file.parent.mkdir(parents=True)
        ios_file.write_bytes(b'ios')
        ios_zip = client.zip_cache / 'global-ios' / 'keep.zip'
        ios_zip.parent.mkdir(parents=True)
        ios_zip.write_bytes(b'zip')
        android_file = client.data_dir / 'download_cache' / 'global-android' / 'keep.bundle'
        android_file.parent.mkdir(parents=True)
        android_file.write_bytes(b'android')
        android_rows = get_game_files(client.db_path, get_table_name('global-android'))
        android_version = get_stored_version(client.db_path, 'global-android')

        client.clean(server='global', platform='ios')

        assert get_game_files(client.db_path, get_table_name('global-ios')) == []
        assert get_stored_version(client.db_path, 'global-ios') is None
        assert not ios_file.exists() and not ios_zip.exists()
        assert get_game_files(client.db_path, get_table_name('global-android')) == android_rows
        assert get_stored_version(client.db_path, 'global-android') == android_version
        assert android_file.read_bytes() == b'android'

    def test_bulk_all_expands_in_supported_target_order(self, client):
        expected = ['global-android', 'global-ios', 'japan-android', 'japan-ios', 'japan-windows']
        with patch.object(client, '_fetch_platform', return_value=False) as fetch:
            client.update()
            assert [call.args[0] for call in fetch.call_args_list] == expected
            fetch.reset_mock()
            client.update(server='all', platform='all')
        assert [call.args[0] for call in fetch.call_args_list] == expected

    def test_invalid_bulk_list_fails_before_any_action(self, client):
        rows_before = get_game_files(client.db_path, get_table_name('global-android'))
        version_before = get_stored_version(client.db_path, 'global-android')
        with patch.object(client, '_fetch_platform') as fetch, patch.object(client, '_invalidate') as invalidate:
            with pytest.raises(ValueError):
                client.update(platform=['global-android', 'invalid-target'])
            with pytest.raises(ValueError):
                client.clean(platform=['global-android', 'invalid-target'])
        fetch.assert_not_called()
        invalidate.assert_not_called()
        assert get_game_files(client.db_path, get_table_name('global-android')) == rows_before
        assert get_stored_version(client.db_path, 'global-android') == version_before

    def test_invalid_split_inputs_fail_before_background_or_output_side_effects(self, client, tmp_path):
        with patch.object(client, '_ensure_fresh_background') as background:
            with pytest.raises(ValueError):
                client.query('ch0230', platform='ios', update_background=True)
            with pytest.raises(ValueError):
                client.query('ch0230', platform='windows', server='global', update_background=True)
        background.assert_not_called()

        output = tmp_path / 'invalid-output'
        cache = tmp_path / 'invalid-cache'
        with pytest.raises(ValueError):
            client.download('ch0230', platform='windows', server='global', output_dir=output)
        with pytest.raises(ValueError):
            client.get_latest_files('ch0230', platform='ios', cache_dir=cache)
        assert not output.exists()
        assert not cache.exists()


class TestFileInfoCompatibility:
    def test_computed_properties_do_not_change_dataclass_or_json_shape(self):
        field_names = [
            'name', 'platform', 'path', 'url', 'hash_type', 'hash_value', 'size', 'pack',
        ]
        dataclass_fields = fields(FileInfo)
        assert [item.name for item in dataclass_fields] == field_names
        assert all(item.default is MISSING for item in dataclass_fields)

        info = FileInfo(
            name='ch0230.bundle', platform='global-ios', path='iOS/ch0230.bundle',
            url='https://ios/ch0230.bundle', hash_type='md5', hash_value='abc',
            size=3, pack=None,
        )
        assert list(asdict(info)) == field_names
        assert asdict(info)['platform'] == 'global-ios'
        assert info == FileInfo(**asdict(info))
        assert _fi_dict(info) == {
            'name': 'ch0230.bundle', 'platform': 'global-ios',
            'path': 'iOS/ch0230.bundle', 'url': 'https://ios/ch0230.bundle',
            'hash_type': 'md5', 'hash_value': 'abc', 'size': 3, 'pack': None,
        }

    @pytest.mark.parametrize(
        ('target', 'server', 'device'),
        [
            ('global-android', Server.GLOBAL, Platform.ANDROID),
            ('global-ios', Server.GLOBAL, Platform.IOS),
            ('japan-android', Server.JAPAN, Platform.ANDROID),
            ('japan-ios', Server.JAPAN, Platform.IOS),
            ('japan-windows', Server.JAPAN, Platform.WINDOWS),
        ],
    )
    def test_target_properties_cover_supported_composites(self, target, server, device):
        info = FileInfo('a.bundle', target, None, None, None, None, None, None)
        assert info.server is server
        assert info.device_platform is device


# ---------------------------------------------------------------------------
# query() — update_background
# ---------------------------------------------------------------------------

class TestQueryUpdateBackground:
    def test_returns_immediately_without_blocking(self, tmp_path):
        client = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_global(client.db_path)
        started = threading.Event()
        release = threading.Event()

        def slow_fetch(session, db_path, force=False, check_interval=None, *, target):
            started.set()
            release.wait(timeout=2)
            return False

        with patch('bagfd.client.fetch_global', side_effect=slow_fetch):
            results = client.query('ch0230', platform='global-android', update_background=True)
            assert started.wait(timeout=1), "background fetch never started"
        assert len(results) == 1
        release.set()

    def test_dedupes_concurrent_background_calls(self, tmp_path):
        client = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_global(client.db_path)
        release = threading.Event()
        calls = []

        def slow_fetch(session, db_path, force=False, check_interval=None, *, target):
            calls.append(1)
            release.wait(timeout=2)
            return False

        with patch('bagfd.client.fetch_global', side_effect=slow_fetch):
            client.query('ch0230', platform='global-android', update_background=True)
            client.query('ch0230', platform='global-android', update_background=True)
            release.set()
            lock = client._update_locks['global-android']
            assert lock.acquire(timeout=2)
            lock.release()

        assert len(calls) == 1

    def test_background_thread_populates_empty_catalog(self, tmp_path):
        client = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        release = threading.Event()

        def fake_fetch(session, db_path, force=False, check_interval=None, *, target):
            # Stay blocked until the caller has asserted the first (empty) query.
            assert release.wait(timeout=2)
            _seed_global(db_path)
            update_version(db_path, 'global-android', '1.0.0', is_new_version=True)
            return True

        with patch('bagfd.client.fetch_global', side_effect=fake_fetch):
            results = client.query('ch0230', platform='global-android', update_background=True)
            assert results == []
            release.set()

            lock = client._update_locks['global-android']
            assert lock.acquire(timeout=2)
            lock.release()

            results = client.query('ch0230', platform='global-android')
        assert len(results) == 1

    def test_first_fetch_error_logs_releases_lock_and_allows_retry(self, tmp_path, caplog):
        client = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        started = threading.Event()
        release = threading.Event()
        failed = threading.Event()
        retried = threading.Event()
        calls = []

        def fail_then_retry(session, db_path, force=False, check_interval=None, *, target):
            calls.append(target)
            if len(calls) == 1:
                started.set()
                try:
                    assert release.wait(timeout=2)
                    raise ResourceUnavailableError('first catalog unavailable')
                finally:
                    failed.set()
            retried.set()
            return False

        lock = client._update_locks['global-ios']
        try:
            with patch('bagfd.client.fetch_global', side_effect=fail_then_retry):
                assert client.query(
                    'missing.bundle', platform='ios', server='global', update_background=True,
                ) == []
                assert started.wait(timeout=1)
                release.set()
                assert failed.wait(timeout=1)
                assert lock.acquire(timeout=1)
                lock.release()

                assert client.query(
                    'missing.bundle', platform='ios', server='global', update_background=True,
                ) == []
                assert retried.wait(timeout=1)
            assert calls == ['global-ios', 'global-ios']
            assert 'Background catalog update failed for global-ios' in caplog.text
        finally:
            release.set()
            if lock.acquire(timeout=2):
                lock.release()

    def test_different_targets_refresh_concurrently_with_distinct_locks(self, tmp_path):
        client = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        global_started = threading.Event()
        release_global = threading.Event()
        japan_finished = threading.Event()

        def slow_global(session, db_path, force=False, check_interval=None, *, target):
            global_started.set()
            assert release_global.wait(timeout=2)
            return False

        def quick_japan(session, db_path, force=False, check_interval=None, *, requested_targets):
            assert requested_targets == ['japan-ios']
            japan_finished.set()
            return {'japan-ios': False}

        global_lock = client._update_locks['global-ios']
        japan_lock = client._update_locks['japan-ios']
        try:
            with patch('bagfd.client.fetch_global', side_effect=slow_global), \
                    patch('bagfd.client.fetch_japan_servers', side_effect=quick_japan):
                client.query('missing.bundle', platform='ios', server='global', update_background=True)
                assert global_started.wait(timeout=1)
                client.query('missing.bundle', platform='ios', server='japan', update_background=True)
                assert japan_finished.wait(timeout=1)
                assert japan_lock.acquire(timeout=1)
                japan_lock.release()
                assert not release_global.is_set()
                assert global_lock.locked()
        finally:
            release_global.set()
            if global_lock.acquire(timeout=2):
                global_lock.release()
            if japan_lock.acquire(timeout=2):
                japan_lock.release()


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
        # Return a full delivery (51 paths) so the completeness guard is
        # satisfied — this test only asserts max_files=None bypasses the limit.
        delivered = [tmp_path / 'c' / f'f{i:03d}.bundle' for i in range(51)]
        with patch('bagfd.client.download_files', return_value=delivered):
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
        _seed_global_ios(client.db_path)
        _seed_japan_ios(client.db_path)
        _seed_versions(client.db_path)
        client.clean(platform='all')
        for platform in [
            'global-android', 'global-ios', 'japan-android', 'japan-ios', 'japan-windows',
        ]:
            assert get_game_files(client.db_path, get_table_name(platform)) == []
            assert get_stored_version(client.db_path, platform) is None

    def test_clean_japan_leaves_global(self, client):
        client.clean(platform='japan-android')
        conn = sqlite3.connect(client.db_path)
        rows = conn.execute("SELECT COUNT(*) FROM global_android").fetchone()[0]
        conn.close()
        assert rows == 3


# ---------------------------------------------------------------------------
# update() — cache pruning on a catalog change
# ---------------------------------------------------------------------------

class TestUpdatePrunesCache:
    """update() must keep cached files still valid after a catalog refresh,
    evicting only what changed or dropped out — not a blanket wipe."""

    def test_global_keeps_valid_evicts_stale(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_versions(c.db_path)

        cache_dir = c.data_dir / "download_cache" / "global-android"
        cache_dir.mkdir(parents=True)
        (cache_dir / "keep.bundle").write_bytes(b"KEEP")
        (cache_dir / "stale.bundle").write_bytes(b"STALE")

        def fake_fetch(session, db_path, force=False, check_interval=None, *, target):
            assert target == 'global-android'
            save_game_files(db_path, get_table_name("global-android"), [
                ("Android/keep.bundle", "https://cdn/keep.bundle", "md5", hashlib.md5(b"KEEP").hexdigest(), 4, None),
                # stale.bundle's catalog row is gone entirely (dropped from the catalog)
            ])
            return True

        with patch('bagfd.client.fetch_global', side_effect=fake_fetch):
            c.update(platform='global-android')

        assert (cache_dir / "keep.bundle").exists()
        assert not (cache_dir / "stale.bundle").exists()

    def test_japan_hash_sweeps_zip_cache_but_clears_extracted_files(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_versions(c.db_path)

        zip_dir = c.zip_cache / "japan-android"
        zip_dir.mkdir(parents=True)
        (zip_dir / "Pack_keep.zip").write_bytes(b"KEEP")
        (zip_dir / "Pack_stale.zip").write_bytes(b"STALE")

        extracted_dir = c.data_dir / "download_cache" / "japan-android"
        extracted_dir.mkdir(parents=True)
        (extracted_dir / "member.bundle").write_bytes(b"whatever")

        def fake_fetch(session, db_path, force=False, check_interval=None, *, requested_targets):
            assert requested_targets == ['japan-android']
            save_game_files(db_path, get_table_name("japan-android"), [
                ("Pack_keep.zip", "https://jp/Pack_keep.zip", "crc32",
                 str(zlib.crc32(b"KEEP") & 0xFFFFFFFF), 4, "[]"),
                # Pack_stale.zip's catalog row is gone entirely
            ])
            return {"japan-android": True}

        with patch('bagfd.client.fetch_japan_servers', side_effect=fake_fetch):
            c.update(platform='japan-android')

        assert (zip_dir / "Pack_keep.zip").exists()          # hash-verified, still valid
        assert not (zip_dir / "Pack_stale.zip").exists()     # gone from catalog, evicted
        # Extracted individual files aren't catalog-tracked on their own;
        # cheaper to just clear them and let extraction from the (now-valid)
        # zip cache repopulate them.
        assert not (extracted_dir / "member.bundle").exists()

    def test_no_catalog_change_leaves_cache_untouched(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path)
        _seed_versions(c.db_path)

        cache_dir = c.data_dir / "download_cache" / "global-android"
        cache_dir.mkdir(parents=True)
        (cache_dir / "untouched.bundle").write_bytes(b"DATA")

        with patch('bagfd.client.fetch_global', return_value=False):
            c.update(platform='global-android')

        assert (cache_dir / "untouched.bundle").exists()


# ---------------------------------------------------------------------------
# download() / get_latest_files() — delivery (network mocked)
# ---------------------------------------------------------------------------

def _fake_download(items, session, workers=10, show_progress=False, verify=VerifyMethod.HASH, force=False, locks=None):
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
# get_latest_files() surfaces an unavailable server as retryable
# ---------------------------------------------------------------------------

class TestResourceUnavailable:
    @pytest.fixture
    def client(self, tmp_path):
        c = BlueArchiveGameFilesDownloader(data_dir=tmp_path / "data")
        _seed_global(c.db_path)
        _seed_japan(c.db_path, "japan-android")
        _seed_versions(c.db_path)
        return c

    def test_global_short_delivery_raises(self, client, tmp_path):
        # download_files swallows failures and returns fewer paths than asked;
        # get_latest_files should surface that as ResourceUnavailableError.
        with patch('bagfd.client.download_files', return_value=[]):
            with pytest.raises(ResourceUnavailableError):
                client.get_latest_files('ch0230', platform='global-android',
                                        cache_dir=tmp_path / 'c')

    def test_japan_missing_pack_raises(self, client, tmp_path):
        # patched downloader places no zip -> the pack is missing -> retryable.
        with patch('bagfd.client.download_files', return_value=[]):
            with pytest.raises(ResourceUnavailableError):
                client.get_latest_files('ch0230_a.bundle', platform='japan-android',
                                        cache_dir=tmp_path / 'c', filter_method='contains')

    def test_japan_full_delivery_no_raise(self, client, tmp_path):
        # the pack is present -> extracts normally, no error.
        zpath = client.zip_cache / 'japan-android' / 'Pack_ch0230.zip'
        zpath.parent.mkdir(parents=True)
        with zipfile.ZipFile(zpath, 'w') as zf:
            zf.writestr('ch0230_a.bundle', b'AAA')
            zf.writestr('ch0230_b.bundle', b'BBB')
        with patch('bagfd.client.download_files', return_value=[]):
            paths = client.get_latest_files('ch0230_a.bundle', platform='japan-android',
                                            cache_dir=tmp_path / 'c', filter_method='contains')
        assert len(paths) == 1
