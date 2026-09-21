"""
Unit tests for bagfd.fetchers — the maintenance-defer fallback.

These exercise the path where the game server is mid version-update: a new
version is detected but the catalog can't be fetched. The fetcher must keep the
cached catalog, park a defer window, and return "no new version" instead of
raising.
"""
import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import requests
import pytest

from bagfd.database import (
    get_cached_japan_api_url,
    get_game_files,
    get_stored_version,
    init_database,
    save_game_files,
    set_cached_japan_api_url,
    set_defer,
    update_version,
)
from bagfd.fetchers import fetch_global, fetch_global_android, fetch_japan_servers
from bagfd.models import ResourceUnavailableError


def _read_defer(db, platform):
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT defer_until FROM versions WHERE platform = ?", (platform,)).fetchone()
    conn.close()
    return row[0] if row else None


def _read_last_check(db, platform):
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT last_check FROM versions WHERE platform = ?", (platform,)).fetchone()
    conn.close()
    return row[0] if row else None


def _read_last_update(db, platform):
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT last_update FROM versions WHERE platform = ?", (platform,)).fetchone()
    conn.close()
    return row[0] if row else None


def _set_last_check(db, platform, when: datetime):
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE versions SET last_check = ? WHERE platform = ?",
        (when.isoformat(), platform),
    )
    conn.commit()
    conn.close()


def _set_last_update(db, platform, when: datetime):
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE versions SET last_update = ? WHERE platform = ?",
        (when.isoformat(), platform),
    )
    conn.commit()
    conn.close()


def _ok(text=None, content=None, json_exc=None):
    """A fake response whose raise_for_status passes."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if text is not None:
        resp.text = text
    if content is not None:
        resp.content = content
    if json_exc is not None:
        resp.json.side_effect = json_exc
    return resp


def _json_response(payload):
    response = _ok()
    response.json.return_value = payload
    return response


class TestGlobalDefer:
    def test_catalog_failure_defers_and_keeps_stale(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        update_version(db, "global-android", "1.0.0")  # stale catalog to fall back to

        version_resp = _ok(text="Blue Archive 1.2.3")
        # The catalog POST returns an empty body -> .json() blows up.
        bad_resp = _ok(json_exc=json.JSONDecodeError("Expecting value", "", 0))

        session = MagicMock()
        session.get.return_value = version_resp
        session.post.return_value = bad_resp

        # force=True to bypass the check interval and enter the catalog fetch.
        result = fetch_global_android(session, db, force=True)

        assert result is False                              # reported as no-new-version, no raise
        assert get_stored_version(db, "global-android") == "1.0.0"  # stale kept, not bumped to 1.2.3
        assert _read_defer(db, "global-android") is not None        # defer window parked


class TestJapanDefer:
    def test_catalog_failure_defers_both_platforms(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        update_version(db, "japan-android", "1.69.0")
        update_version(db, "japan-windows", "1.69.0")

        bad_api = _ok(json_exc=json.JSONDecodeError("Expecting value", "", 0))
        session = MagicMock()
        session.get.return_value = bad_api

        with patch(
            "bagfd.yostar.resolve_japan_server_info_url",
            return_value=("1.70.436321", "https://fake/api"),
        ):
            results = fetch_japan_servers(session, db, force=True)

        assert results == {"japan-android": False, "japan-windows": False}
        assert get_stored_version(db, "japan-android") == "1.69.0"
        assert _read_defer(db, "japan-android") is not None
        assert _read_defer(db, "japan-windows") is not None


class TestJapanApiUrlCacheAndHotfix:
    """Cached API URL skips resources.assets/XAPK, but catalog lookup still runs."""

    def _mock_session(self, version_resp, addressable_resp, android_bundle_resp, windows_bundle_resp, api_url):
        requested = []

        def get_side(url, *args, **kwargs):
            requested.append(url)
            if (
                "api-launcher-jp.yo-star.com/api/launcher/game/config" in url
                and "json" not in url
                and "cdn" not in url
            ):
                return version_resp
            if url == api_url:
                return addressable_resp
            if "Android_PatchPack" in url:
                return android_bundle_resp
            if "Windows_PatchPack" in url:
                return windows_bundle_resp
            raise AssertionError(f"unexpected GET {url}")

        session = MagicMock()
        session.get.side_effect = get_side
        return session, requested

    def test_same_version_reuses_api_url_but_still_detects_hotfix(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        update_version(db, "japan-android", "1.70.436321", is_new_version=True)
        update_version(db, "japan-windows", "1.70.436321", is_new_version=True)
        set_cached_japan_api_url(db, "1.70.436321", "https://fake/api")

        save_game_files(db, "japan_android", [
            ("Android_Pack_0.zip", "http://cdn/Android_PatchPack/Android_Pack_0.zip", "crc32", "111", 1000, "[]"),
        ])
        save_game_files(db, "japan_windows", [
            ("Windows_Pack_0.zip", "http://cdn/Windows_PatchPack/Windows_Pack_0.zip", "crc32", "222", 1000, "[]"),
        ])

        version_resp = _ok()
        version_resp.json.return_value = {
            "code": 200,
            "data": {"game_latest_version": "1.70.436321", "game_latest_file_path": "x"},
        }
        addressable = _ok()
        addressable.json.return_value = {
            "ConnectionGroups": [{"OverrideConnectionGroups": [{}, {"AddressablesCatalogUrlRoot": "http://cdn"}]}]
        }
        android_bundle = _ok()
        android_bundle.json.return_value = {"FullPatchPacks": [
            {"PackName": "Android_Pack_0.zip", "Crc": 999, "PackSize": 1000, "BundleFiles": []}
        ], "UpdatePacks": []}
        windows_bundle = _ok()
        windows_bundle.json.return_value = {"FullPatchPacks": [
            {"PackName": "Windows_Pack_0.zip", "Crc": 222, "PackSize": 1000, "BundleFiles": []}
        ], "UpdatePacks": []}

        session, requested = self._mock_session(
            version_resp, addressable, android_bundle, windows_bundle, "https://fake/api"
        )
        results = fetch_japan_servers(session, db, force=False, check_interval=timedelta(seconds=-1))

        assert results == {"japan-android": True, "japan-windows": False}
        assert not any("resources.assets" in u or u.endswith(".xapk") for u in requested)
        rows = {path: hv for path, _u, _ht, hv, _s, _bf in get_game_files(db, "japan_android")}
        assert rows["Android_Pack_0.zip"] == "999"

    def test_identical_catalog_returns_false(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        update_version(db, "japan-android", "1.70.436321", is_new_version=True)
        update_version(db, "japan-windows", "1.70.436321", is_new_version=True)
        set_cached_japan_api_url(db, "1.70.436321", "https://fake/api")

        bundle_files_json = json.dumps(["a.bundle", "b.bundle"])
        save_game_files(db, "japan_android", [
            ("Android_Pack_0.zip", "http://cdn/Android_PatchPack/Android_Pack_0.zip", "crc32", "111", 1000, bundle_files_json),
        ])
        save_game_files(db, "japan_windows", [
            ("Windows_Pack_0.zip", "http://cdn/Windows_PatchPack/Windows_Pack_0.zip", "crc32", "222", 2000, "[]"),
        ])

        version_resp = _ok()
        version_resp.json.return_value = {
            "code": 200,
            "data": {"game_latest_version": "1.70.436321", "game_latest_file_path": "x"},
        }
        addressable = _ok()
        addressable.json.return_value = {
            "ConnectionGroups": [{"OverrideConnectionGroups": [{}, {"AddressablesCatalogUrlRoot": "http://cdn"}]}]
        }
        android_bundle = _ok()
        android_bundle.json.return_value = {"FullPatchPacks": [
            {"PackName": "Android_Pack_0.zip", "Crc": 111, "PackSize": 1000,
             "BundleFiles": [{"Name": "a.bundle"}, {"Name": "b.bundle"}]}
        ], "UpdatePacks": []}
        windows_bundle = _ok()
        windows_bundle.json.return_value = {"FullPatchPacks": [
            {"PackName": "Windows_Pack_0.zip", "Crc": 222, "PackSize": 2000, "BundleFiles": []}
        ], "UpdatePacks": []}

        session, _requested = self._mock_session(
            version_resp, addressable, android_bundle, windows_bundle, "https://fake/api"
        )
        results = fetch_japan_servers(session, db, force=False, check_interval=timedelta(seconds=-1))
        assert results == {"japan-android": False, "japan-windows": False}


class TestJapanForceBypassesApiUrlCache:
    def test_force_resolves_fresh_server_info(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        old_version = "1.70.436320"
        old_timestamp = datetime(2000, 1, 1)
        deferred_until = datetime(2099, 1, 1)
        targets = ("japan-android", "japan-windows")
        for target in targets:
            update_version(db, target, old_version, is_new_version=True)
            _set_last_check(db, target, old_timestamp)
            _set_last_update(db, target, old_timestamp)
            set_defer(db, target, deferred_until)
        set_cached_japan_api_url(db, old_version, "https://stale-cached/api")

        addressable = _ok()
        addressable.json.return_value = {
            "ConnectionGroups": [{"OverrideConnectionGroups": [{}, {"AddressablesCatalogUrlRoot": "http://cdn"}]}]
        }
        android_bundle = _json_response({"FullPatchPacks": [{
            "PackName": "Android_Pack_0.zip", "Crc": 111,
            "PackSize": 1000, "BundleFiles": [{"Name": "android.bundle"}],
        }], "UpdatePacks": []})
        windows_bundle = _json_response({"FullPatchPacks": [{
            "PackName": "Windows_Pack_0.zip", "Crc": 222,
            "PackSize": 2000, "BundleFiles": [{"Name": "windows.bundle"}],
        }], "UpdatePacks": []})

        def get_side(url, *args, **kwargs):
            if url == "https://fresh/api":
                return addressable
            if "Android_PatchPack" in url:
                return android_bundle
            if "Windows_PatchPack" in url:
                return windows_bundle
            raise AssertionError(f"unexpected GET {url}")

        session = MagicMock()
        session.get.side_effect = get_side

        with patch(
            "bagfd.yostar.resolve_japan_server_info_url",
            return_value=("1.70.436321", "https://fresh/api"),
        ):
            results = fetch_japan_servers(session, db, force=True)

        assert results == {"japan-android": True, "japan-windows": True}
        assert get_cached_japan_api_url(db) == ("1.70.436321", "https://fresh/api")
        assert get_game_files(db, "japan_android")[0][0] == "Android_Pack_0.zip"
        assert get_game_files(db, "japan_windows")[0][0] == "Windows_Pack_0.zip"
        for target in targets:
            assert get_stored_version(db, target) == "1.70.436321"
            assert datetime.fromisoformat(_read_last_check(db, target)) > old_timestamp
            assert datetime.fromisoformat(_read_last_update(db, target)) > old_timestamp
            assert _read_defer(db, target) is None


class TestJapanUpdateVersionOnlyDuePlatforms:
    """Non-due platforms must keep their last_check so the interval is not reset."""

    def test_skips_update_version_for_platforms_not_due(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        update_version(db, "japan-android", "1.70.436321", is_new_version=True)
        update_version(db, "japan-windows", "1.70.436321", is_new_version=True)
        set_cached_japan_api_url(db, "1.70.436321", "https://fake/api")

        # Only android is due; windows was checked recently.
        _set_last_check(db, "japan-android", datetime.now() - timedelta(hours=5))
        recent = datetime.now() - timedelta(minutes=10)
        _set_last_check(db, "japan-windows", recent)
        windows_last_check_before = _read_last_check(db, "japan-windows")

        save_game_files(db, "japan_android", [
            ("Android_Pack_0.zip", "http://cdn/Android_PatchPack/Android_Pack_0.zip", "crc32", "111", 1000, "[]"),
        ])
        save_game_files(db, "japan_windows", [
            ("Windows_Pack_0.zip", "http://cdn/Windows_PatchPack/Windows_Pack_0.zip", "crc32", "222", 1000, "[]"),
        ])

        version_resp = _ok()
        version_resp.json.return_value = {
            "code": 200,
            "data": {"game_latest_version": "1.70.436321", "game_latest_file_path": "x"},
        }
        addressable = _ok()
        addressable.json.return_value = {
            "ConnectionGroups": [{"OverrideConnectionGroups": [{}, {"AddressablesCatalogUrlRoot": "http://cdn"}]}]
        }
        android_bundle = _ok()
        android_bundle.json.return_value = {"FullPatchPacks": [
            {"PackName": "Android_Pack_0.zip", "Crc": 111, "PackSize": 1000, "BundleFiles": []}
        ], "UpdatePacks": []}
        windows_bundle = _ok()
        windows_bundle.json.return_value = {"FullPatchPacks": [
            {"PackName": "Windows_Pack_0.zip", "Crc": 222, "PackSize": 1000, "BundleFiles": []}
        ], "UpdatePacks": []}

        session = MagicMock()

        def get_side(url, *args, **kwargs):
            if (
                "api-launcher-jp.yo-star.com/api/launcher/game/config" in url
                and "json" not in url
                and "cdn" not in url
            ):
                return version_resp
            if url == "https://fake/api":
                return addressable
            if "Android_PatchPack" in url:
                return android_bundle
            if "Windows_PatchPack" in url:
                return windows_bundle
            raise AssertionError(f"unexpected GET {url}")

        session.get.side_effect = get_side

        fetch_japan_servers(session, db, force=False, check_interval=timedelta(hours=4))

        assert _read_last_check(db, "japan-windows") == windows_last_check_before
        # Due platform should have been refreshed.
        assert (
            datetime.fromisoformat(_read_last_check(db, "japan-android"))
            > datetime.fromisoformat(windows_last_check_before)
        )


class TestGlobalIOSFetcher:
    def test_ios_uses_apple_identity_payload_and_manifest_directory(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        apple_url = "https://itunes.apple.com/lookup?id=1571873795&country=us"
        manifest_url = "https://cdn.example/catalogs/rev-4/not-resource-data.json"
        apple = _json_response({"results": [{
            "trackId": 1571873795,
            "bundleId": "com.nexon.bluearchive",
            "version": "1.93.452024",
        }]})
        manifest = _json_response({"resources": [
            {"resource_path": "GameData/iOS/ios.bundle", "resource_size": 17,
             "resource_hash": "ios-md5"},
            {"resource_path": "GameData/Android/android.bundle", "resource_size": 18,
             "resource_hash": "android-md5"},
            {"resource_path": "GameData/NotiOS/other.bundle", "resource_size": 19,
             "resource_hash": "other-md5"},
        ]})
        session = MagicMock()

        def get_side(url, *args, **kwargs):
            if url == apple_url:
                return apple
            if url == manifest_url:
                return manifest
            raise AssertionError(f"unexpected GET {url}")

        session.get.side_effect = get_side
        session.post.return_value = _json_response({"patch": {"resource_path": manifest_url}})

        changed = fetch_global(session, db, force=True, target="global-ios")

        assert changed is True
        assert session.post.call_args.kwargs["json"] == {
            "market_game_id": "1571873795",
            "market_code": "appstore",
            "curr_build_version": "1.93.452024",
            "curr_build_number": "452024",
        }
        assert session.get.call_args_list[0].args[0] == apple_url
        assert get_game_files(db, "global_ios") == [(
            "GameData/iOS/ios.bundle",
            "https://cdn.example/catalogs/rev-4/GameData/iOS/ios.bundle",
            "md5", "ios-md5", 17, None,
        )]
        assert get_game_files(db, "global_android") == []
        assert get_stored_version(db, "global-ios") == "1.93.452024"
        assert get_stored_version(db, "global-android") is None

    @pytest.mark.parametrize("apple_result", [
        {"trackId": 42, "version": "1.2.3"},
        {"trackId": 1571873795, "bundleId": "other.bundle", "version": "1.2.3"},
        {"trackId": 1571873795, "version": "1.2"},
    ])
    def test_invalid_apple_lookup_is_unavailable_on_first_fetch(self, tmp_path, apple_result):
        db = tmp_path / "catalog.db"
        init_database(db)
        session = MagicMock()
        session.get.return_value = _json_response({"results": [apple_result]})

        with pytest.raises(ResourceUnavailableError):
            fetch_global(session, db, force=True, target="global-ios")

        assert get_stored_version(db, "global-ios") is None
        session.post.assert_not_called()

    def test_cached_apple_discovery_failure_defers_without_version_change(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        update_version(db, "global-ios", "1.2.3")
        save_game_files(db, "global_ios", [
            ("old.bundle", "https://cdn/old.bundle", "md5", "old-md5", 12, None),
        ])
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError("temporary outage")

        changed = fetch_global(session, db, force=True, target="global-ios")

        assert changed is False
        assert get_stored_version(db, "global-ios") == "1.2.3"
        assert get_game_files(db, "global_ios")[0][0] == "old.bundle"
        assert _read_defer(db, "global-ios") is not None
        session.post.assert_not_called()

    def test_same_version_manifest_change_then_identical_catalog(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        version = "1.93.452024"
        update_version(db, "global-ios", version, is_new_version=True)
        save_game_files(db, "global_ios", [
            ("GameData/iOS/old.bundle", "https://cdn/old.bundle", "md5", "old", 5, None),
        ])
        apple_url = "https://itunes.apple.com/lookup?id=1571873795&country=us"
        manifest_url = "https://cdn.example/catalog/revision.json"
        apple = _json_response({"results": [{
            "trackId": 1571873795, "version": version,
        }]})
        manifest = _json_response({"resources": [
            {"resource_path": "GameData/iOS/new.bundle", "resource_size": 10,
             "resource_hash": "new-md5"},
        ]})
        session = MagicMock()
        session.get.side_effect = lambda url, *args, **kwargs: {
            apple_url: apple, manifest_url: manifest,
        }[url]
        session.post.return_value = _json_response({"patch": {"resource_path": manifest_url}})

        first = fetch_global(session, db, force=True, target="global-ios")
        second = fetch_global(session, db, force=True, target="global-ios")

        assert (first, second) == (True, False)
        assert get_stored_version(db, "global-ios") == version
        assert get_game_files(db, "global_ios")[0][0] == "GameData/iOS/new.bundle"
        assert _read_defer(db, "global-ios") is None

    @pytest.mark.parametrize("manifest_data", [
        {"resources": []},
        {"resources": {}},
        {"resources": [{"resource_path": "GameData/iOS/missing-size.bundle",
                        "resource_hash": "hash"}]},
    ])
    def test_empty_or_malformed_manifest_preserves_cached_catalog(self, tmp_path, manifest_data):
        db = tmp_path / "catalog.db"
        init_database(db)
        version = "1.93.452024"
        update_version(db, "global-ios", version, is_new_version=True)
        old_rows = [
            ("GameData/iOS/old.bundle", "https://cdn/old.bundle", "md5", "old", 5, None),
        ]
        save_game_files(db, "global_ios", old_rows)
        apple_url = "https://itunes.apple.com/lookup?id=1571873795&country=us"
        manifest_url = "https://cdn.example/catalog/revision.json"
        session = MagicMock()
        session.get.side_effect = lambda url, *args, **kwargs: {
            apple_url: _json_response({"results": [{
                "trackId": 1571873795, "version": version,
            }]}),
            manifest_url: _json_response(manifest_data),
        }[url]
        session.post.return_value = _json_response({"patch": {"resource_path": manifest_url}})

        changed = fetch_global(session, db, force=True, target="global-ios")

        assert changed is False
        assert get_game_files(db, "global_ios") == old_rows
        assert get_stored_version(db, "global-ios") == version
        assert _read_defer(db, "global-ios") is not None

    def test_android_compatibility_wrapper_keeps_playstore_route(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        manifest_url = "https://cdn.example/android/manifest.json"
        session = MagicMock()
        session.get.side_effect = [
            _ok(text="Blue Archive 1.90.123456"),
            _json_response({"resources": [
                {"resource_path": "GameData/Android/aa.bundle", "resource_size": 7,
                 "resource_hash": "android-md5"},
                {"resource_path": "GameData/iOS/ios.bundle", "resource_size": 8,
                 "resource_hash": "ios-md5"},
            ]}),
        ]
        session.post.return_value = _json_response({"patch": {"resource_path": manifest_url}})

        changed = fetch_global_android(session, db, True)

        assert changed is True
        assert session.post.call_args.kwargs["json"] == {
            "market_game_id": "com.nexon.bluearchive",
            "market_code": "playstore",
            "curr_build_version": "1.90.123456",
            "curr_build_number": "123456",
        }
        assert get_game_files(db, "global_android")[0][0] == "GameData/Android/aa.bundle"
        assert get_game_files(db, "global_ios") == []

    @pytest.mark.parametrize("target", ["japan-ios", "global-all", "ios"])
    def test_invalid_global_target_has_no_database_or_network_side_effects(self, tmp_path, target):
        db = tmp_path / "not-created.db"
        session = MagicMock()

        with pytest.raises(ValueError):
            fetch_global(session, db, target=target)

        assert not db.exists()
        assert session.method_calls == []


class TestJapanIOSFetcher:
    def test_ios_catalog_uses_requested_patch_pack_and_validates_members(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        version = "1.72.436321"
        set_cached_japan_api_url(db, version, "https://fake/api")
        update_version(db, "japan-ios", version, is_new_version=True)

        addressable = _json_response({"ConnectionGroups": [{
            "OverrideConnectionGroups": [{}, {
                "AddressablesCatalogUrlRoot": "https://cdn.example/catalog",
            }],
        }]})
        ios_packs = _json_response({
            "FullPatchPacks": [{
                "PackName": "IOS_Pack_0.zip",
                "Crc": 3_000_000_001,
                "PackSize": 1000,
                "BundleFiles": [
                    {"Name": "z.bundle", "Size": 9},
                    {"Name": "a.bundle", "Size": 8},
                ],
            }],
            "UpdatePacks": [{
                "PackName": "IOS_Update.zip", "Crc": "-42", "PackSize": 40,
                "BundleFiles": [{"Name": "update.bundle", "Crc": 9}],
            }],
        })
        session = MagicMock()
        session.get.side_effect = lambda url, *args, **kwargs: {
            "https://fake/api": addressable,
            "https://cdn.example/catalog/iOS_PatchPack/BundlePackingInfo.json": ios_packs,
        }[url]

        with patch(
            "bagfd.yostar.get_yostar_base_config",
            return_value={"game_latest_version": version},
        ):
            results = fetch_japan_servers(
                session, db, check_interval=timedelta(seconds=-1),
                requested_targets=["japan-ios"],
            )

        assert results == {"japan-ios": True}
        assert session.get.call_args_list[1].args[0] == (
            "https://cdn.example/catalog/iOS_PatchPack/BundlePackingInfo.json"
        )
        assert get_game_files(db, "japan_ios") == [
            (
                "IOS_Pack_0.zip",
                "https://cdn.example/catalog/iOS_PatchPack/IOS_Pack_0.zip",
                "crc32", "3000000001", 1000, '["a.bundle", "z.bundle"]',
            ),
            (
                "IOS_Update.zip",
                "https://cdn.example/catalog/iOS_PatchPack/IOS_Update.zip",
                "crc32", "-42", 40, '["update.bundle"]',
            ),
        ]
        assert get_game_files(db, "japan_android") == []
        assert get_game_files(db, "japan_windows") == []
        assert get_stored_version(db, "japan-ios") == version

    @pytest.mark.parametrize("selection", [
        "japan-ios",
        ("global-ios",),
        ("japan-ios", "japan-unknown"),
    ])
    def test_invalid_requested_targets_have_no_side_effects(self, tmp_path, selection):
        db = tmp_path / "not-created.db"
        session = MagicMock()

        with pytest.raises(ValueError):
            fetch_japan_servers(session, db, requested_targets=selection)

        assert not db.exists()
        assert session.method_calls == []

    def test_empty_requested_targets_return_without_side_effects(self, tmp_path):
        db = tmp_path / "not-created.db"
        session = MagicMock()

        assert fetch_japan_servers(session, db, requested_targets=[]) == {}

        assert not db.exists()
        assert session.method_calls == []

    def test_failed_requested_target_defers_only_it_and_continues(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        version = "1.72.436321"
        set_cached_japan_api_url(db, version, "https://fake/api")
        for target in ("japan-android", "japan-ios", "japan-windows"):
            update_version(db, target, version, is_new_version=True)
        recent_windows_check = datetime.now() - timedelta(minutes=1)
        _set_last_check(db, "japan-windows", recent_windows_check)
        windows_check_before = _read_last_check(db, "japan-windows")
        save_game_files(db, "japan_android", [
            ("Android_old.zip", "https://cdn/Android_old.zip", "crc32", "10", 100, "[]"),
        ])
        save_game_files(db, "japan_ios", [
            ("IOS_old.zip", "https://cdn/IOS_old.zip", "crc32", "20", 200, "[]"),
        ])

        addressable = _json_response({"ConnectionGroups": [{
            "OverrideConnectionGroups": [{}, {
                "AddressablesCatalogUrlRoot": "https://cdn.example/catalog",
            }],
        }]})
        empty_android = _json_response({"FullPatchPacks": [], "UpdatePacks": []})
        valid_ios = _json_response({"FullPatchPacks": [{
            "PackName": "IOS_new.zip", "Crc": 30, "PackSize": 300,
            "BundleFiles": [{"Name": "new.bundle"}],
        }], "UpdatePacks": []})
        session = MagicMock()

        def get_side(url, *args, **kwargs):
            if url == "https://fake/api":
                return addressable
            if url.endswith("Android_PatchPack/BundlePackingInfo.json"):
                return empty_android
            if url.endswith("iOS_PatchPack/BundlePackingInfo.json"):
                return valid_ios
            raise AssertionError(f"unexpected GET {url}")

        session.get.side_effect = get_side
        with patch(
            "bagfd.yostar.get_yostar_base_config",
            return_value={"game_latest_version": version},
        ):
            results = fetch_japan_servers(
                session, db, check_interval=timedelta(seconds=-1),
                requested_targets=["japan-android", "japan-ios"],
            )

        assert results == {"japan-android": False, "japan-ios": True}
        assert get_game_files(db, "japan_android")[0][0] == "Android_old.zip"
        assert _read_defer(db, "japan-android") is not None
        assert get_game_files(db, "japan_ios")[0][0] == "IOS_new.zip"
        assert _read_defer(db, "japan-ios") is None
        assert _read_last_check(db, "japan-windows") == windows_check_before
        assert _read_defer(db, "japan-windows") is None

    @pytest.mark.parametrize("bad_catalog", [
        {"FullPatchPacks": {}, "UpdatePacks": []},
        {"FullPatchPacks": [{
            "PackName": "IOS_missing_members.zip", "Crc": 4, "PackSize": 5,
        }], "UpdatePacks": []},
        {"FullPatchPacks": [{
            "PackName": "IOS_bad.zip", "Crc": "not-decimal", "PackSize": 4,
            "BundleFiles": [],
        }], "UpdatePacks": []},
        {"FullPatchPacks": [{
            "PackName": "IOS_bad.zip", "Crc": 5, "PackSize": 4,
            "BundleFiles": [{"Size": 8}],
        }], "UpdatePacks": []},
    ])
    def test_malformed_ios_pack_catalog_preserves_stale_rows(self, tmp_path, bad_catalog):
        db = tmp_path / "catalog.db"
        init_database(db)
        version = "1.72.436321"
        set_cached_japan_api_url(db, version, "https://fake/api")
        update_version(db, "japan-ios", version, is_new_version=True)
        old_rows = [
            ("IOS_old.zip", "https://cdn/IOS_old.zip", "crc32", "-7", 7, "[]"),
        ]
        save_game_files(db, "japan_ios", old_rows)
        last_check_before = _read_last_check(db, "japan-ios")
        addressable = _json_response({"ConnectionGroups": [{
            "OverrideConnectionGroups": [{}, {
                "AddressablesCatalogUrlRoot": "https://cdn.example/catalog",
            }],
        }]})
        session = MagicMock()
        session.get.side_effect = lambda url, *args, **kwargs: {
            "https://fake/api": addressable,
            "https://cdn.example/catalog/iOS_PatchPack/BundlePackingInfo.json": _json_response(bad_catalog),
        }[url]

        with patch(
            "bagfd.yostar.get_yostar_base_config",
            return_value={"game_latest_version": version},
        ):
            changed = fetch_japan_servers(
                session, db, check_interval=timedelta(seconds=-1),
                requested_targets=["japan-ios"],
            )

        assert changed == {"japan-ios": False}
        assert get_game_files(db, "japan_ios") == old_rows
        assert get_stored_version(db, "japan-ios") == version
        assert _read_last_check(db, "japan-ios") == last_check_before
        assert _read_defer(db, "japan-ios") is not None

    def test_first_fetch_missing_bundle_files_raises_without_success_state(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        version = "1.72.436321"
        set_cached_japan_api_url(db, version, "https://fake/api")
        addressable = _json_response({"ConnectionGroups": [{
            "OverrideConnectionGroups": [{}, {
                "AddressablesCatalogUrlRoot": "https://cdn.example/catalog",
            }],
        }]})
        missing_members = _json_response({"FullPatchPacks": [{
            "PackName": "IOS_missing_members.zip", "Crc": 4, "PackSize": 5,
        }], "UpdatePacks": []})
        session = MagicMock()
        session.get.side_effect = lambda url, *args, **kwargs: {
            "https://fake/api": addressable,
            "https://cdn.example/catalog/iOS_PatchPack/BundlePackingInfo.json": missing_members,
        }[url]

        with patch(
            "bagfd.yostar.get_yostar_base_config",
            return_value={"game_latest_version": version},
        ), pytest.raises(ResourceUnavailableError):
            fetch_japan_servers(
                session, db, check_interval=timedelta(seconds=-1),
                requested_targets=["japan-ios"],
            )

        assert get_stored_version(db, "japan-ios") is None
        assert _read_last_check(db, "japan-ios") is None
        assert _read_defer(db, "japan-ios") is None
        assert get_game_files(db, "japan_ios") == []

    def test_first_fetch_failure_raises_after_other_requested_target_succeeds(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        version = "1.72.436321"
        set_cached_japan_api_url(db, version, "https://fake/api")
        update_version(db, "japan-android", version, is_new_version=True)

        addressable = _json_response({"ConnectionGroups": [{
            "OverrideConnectionGroups": [{}, {
                "AddressablesCatalogUrlRoot": "https://cdn.example/catalog",
            }],
        }]})
        android_packs = _json_response({"FullPatchPacks": [{
            "PackName": "Android_new.zip", "Crc": 40, "PackSize": 400,
            "BundleFiles": [{"Name": "android.bundle"}],
        }], "UpdatePacks": []})
        empty_ios = _json_response({"FullPatchPacks": [], "UpdatePacks": []})
        session = MagicMock()

        def get_side(url, *args, **kwargs):
            if url == "https://fake/api":
                return addressable
            if url.endswith("Android_PatchPack/BundlePackingInfo.json"):
                return android_packs
            if url.endswith("iOS_PatchPack/BundlePackingInfo.json"):
                return empty_ios
            raise AssertionError(f"unexpected GET {url}")

        session.get.side_effect = get_side
        with patch(
            "bagfd.yostar.get_yostar_base_config",
            return_value={"game_latest_version": version},
        ), pytest.raises(ResourceUnavailableError):
            fetch_japan_servers(
                session, db, check_interval=timedelta(seconds=-1),
                requested_targets=["japan-android", "japan-ios"],
            )

        assert get_game_files(db, "japan_android")[0][0] == "Android_new.zip"
        assert get_stored_version(db, "japan-android") == version
        assert get_stored_version(db, "japan-ios") is None

    def test_forced_bootstrap_failure_touches_only_requested_target(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        for target in ("japan-android", "japan-ios", "japan-windows"):
            update_version(db, target, "1.70.0", is_new_version=True)
        set_defer(db, "japan-android", datetime.now() + timedelta(minutes=2))
        set_defer(db, "japan-windows", datetime.now() + timedelta(minutes=3))
        before = {
            target: (_read_last_check(db, target), _read_defer(db, target))
            for target in ("japan-android", "japan-ios", "japan-windows")
        }
        session = MagicMock()

        with patch(
            "bagfd.yostar.resolve_japan_server_info_url",
            side_effect=requests.ConnectionError("offline"),
        ):
            result = fetch_japan_servers(
                session, db, force=True, requested_targets=["japan-ios"],
            )

        assert result == {"japan-ios": False}
        assert (_read_last_check(db, "japan-android"), _read_defer(db, "japan-android")) == before["japan-android"]
        assert (_read_last_check(db, "japan-windows"), _read_defer(db, "japan-windows")) == before["japan-windows"]
        assert _read_defer(db, "japan-ios") is not None
        assert session.method_calls == []

    def test_bootstrap_failure_leaves_requested_nondue_and_nonrequested_targets_untouched(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        for target in ("japan-android", "japan-ios", "japan-windows"):
            update_version(db, target, "1.70.0", is_new_version=True)
        _set_last_check(db, "japan-android", datetime.now() - timedelta(hours=5))
        _set_last_check(db, "japan-ios", datetime.now() - timedelta(minutes=1))
        set_defer(db, "japan-ios", datetime.now() + timedelta(minutes=2))
        set_defer(db, "japan-windows", datetime.now() + timedelta(minutes=3))
        before = {
            target: (_read_last_check(db, target), _read_defer(db, target))
            for target in ("japan-ios", "japan-windows")
        }
        session = MagicMock()

        with patch(
            "bagfd.yostar.resolve_japan_server_info_url",
            side_effect=requests.ConnectionError("offline"),
        ):
            results = fetch_japan_servers(
                session, db, check_interval=timedelta(hours=4),
                requested_targets=["japan-android", "japan-ios"],
            )

        assert results == {"japan-android": False, "japan-ios": False}
        assert _read_defer(db, "japan-android") is not None
        assert (_read_last_check(db, "japan-ios"), _read_defer(db, "japan-ios")) == before["japan-ios"]
        assert (_read_last_check(db, "japan-windows"), _read_defer(db, "japan-windows")) == before["japan-windows"]
