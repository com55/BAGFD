"""Tests for Japan YoStar launcher → resources.assets config resolution."""
import struct
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from bagfd.crypto import create_key, encrypt_string, xor_inplace
from bagfd.database import get_stored_version, init_database
from bagfd.fetchers import fetch_japan_servers
from bagfd.yostar import (
    extract_game_main_from_resources,
    japan_build_version,
    resolve_japan_server_info_url,
)


def _ok(json_data=None, content=None, text=None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json.return_value = json_data
    if content is not None:
        resp.iter_content.return_value = [content]
    if text is not None:
        resp.text = text
    return resp


def _build_resources_asset(plaintext_config: dict) -> bytes:
    """Build a resources.assets-like blob containing GameMainConfig."""
    import json

    key = create_key("GameMainConfig")
    utf16 = bytearray()
    text = json.dumps(plaintext_config, separators=(",", ":"))
    for ch in text:
        utf16.extend(struct.pack("<H", ord(ch)))
    xor_inplace(utf16, key)
    encrypted = bytes(utf16)

    marker = b"GameMainConfig\x00\x00"
    return b"PAD" + marker + struct.pack("<i", len(encrypted)) + encrypted + b"TAIL"


def _server_info_config(url: str) -> dict:
    server_key = create_key("ServerInfoDataUrl")
    return {
        encrypt_string("ServerInfoDataUrl", server_key): encrypt_string(url, server_key),
    }


class TestExtractGameMainFromResources:
    def test_extracts_and_decrypts_server_info_url(self):
        blob = _build_resources_asset(
            _server_info_config("https://example.yostar/server-info.json")
        )
        assert extract_game_main_from_resources(blob) == "https://example.yostar/server-info.json"

    def test_full_pattern_branch_reads_size_from_blob(self):
        """When GAME_CONFIG_PATTERN matches, size must be unpacked from the blob."""
        import json

        import bagfd.yostar as yostar

        url = "https://size-from-blob.example/api"
        config = _server_info_config(url)
        # Grow JSON until the XOR'd UTF-16 payload is exactly 0x392 bytes so the
        # 20-byte GAME_CONFIG_PATTERN (marker + that size) matches in the blob.
        pad = 0
        while True:
            padded = {**config, "_": "x" * pad}
            if len(json.dumps(padded, separators=(",", ":"))) * 2 == 0x392:
                config = padded
                break
            pad += 1
            assert pad < 500

        blob = _build_resources_asset(config)
        assert blob.find(yostar.GAME_CONFIG_PATTERN) >= 0

        real_unpack = struct.unpack_from

        def spy(fmt, buffer, offset=0):
            if buffer == yostar.GAME_CONFIG_PATTERN:
                raise AssertionError("size must be read from blob, not GAME_CONFIG_PATTERN")
            return real_unpack(fmt, buffer, offset)

        with patch.object(yostar.struct, "unpack_from", spy):
            assert extract_game_main_from_resources(blob) == url

    def test_missing_pattern_raises(self):
        with pytest.raises(ValueError, match="GameMainConfig"):
            extract_game_main_from_resources(b"nope")


class TestResolveJapanServerInfoUrl:
    def test_launcher_path_skips_xapk(self, tmp_path):
        resources_blob = _build_resources_asset(
            _server_info_config("https://from-launcher/api")
        )
        # md5 of blob for hash check
        import hashlib
        asset_hash = hashlib.md5(resources_blob).hexdigest()

        base = _ok({
            "code": 200,
            "data": {
                "game_latest_version": "1.71.1",
                "game_latest_file_path": "BlueArchive/1.71.1",
            },
        })
        domain = _ok({"code": 200, "data": {"primary_cdn": "https://cdn.example/", "back_up_cdn": ""}})
        json_cfg = _ok({"code": 200, "data": {"url": "https://cdn.example/list.json"}})
        json_data = _ok({
            "source": "pkg/",
            "file": [
                {"path": "foo/bar", "hash": "aaa", "size": "1"},
                {"path": "x/resources.assets", "hash": asset_hash, "size": str(len(resources_blob))},
            ],
        })
        asset = _ok(content=resources_blob)

        def get_side(url, *args, **kwargs):
            if url.endswith("/api/launcher/game/config"):
                return base
            if "download/cdn" in url:
                return domain
            if "/api/launcher/game/config/json" in url:
                return json_cfg
            if url == "https://cdn.example/list.json":
                return json_data
            if url == "https://cdn.example/pkg/x/resources.assets":
                return asset
            raise AssertionError(f"unexpected GET {url}")

        session = MagicMock()
        session.get.side_effect = get_side

        with patch("bagfd.yostar._japan_api_url_from_xapk") as xapk:
            version, api_url = resolve_japan_server_info_url(session, temp_dir=tmp_path)
            xapk.assert_not_called()

        assert version == "1.71.1"
        assert api_url == "https://from-launcher/api"

    def test_falls_back_to_xapk_when_launcher_fails(self, tmp_path):
        session = MagicMock()
        session.get.side_effect = Exception("launcher down")

        with patch(
            "bagfd.yostar._japan_api_url_from_xapk",
            return_value=("1.70.0", "https://from-xapk/api"),
        ) as xapk:
            version, api_url = resolve_japan_server_info_url(session, temp_dir=tmp_path)
            xapk.assert_called_once()

        assert version == "1.70.0"
        assert api_url == "https://from-xapk/api"


_BASE_CONFIG = {
    "game_latest_version": "1.75.0",
    "game_latest_file_path": "prod/ZIP_TEMP/Game_TEMP/Game-1.75.479435-game.zip",
}


class TestJapanBuildVersion:
    def test_reads_full_version_from_package_filename(self):
        assert japan_build_version(_BASE_CONFIG) == "1.75.479435"

    @pytest.mark.parametrize("path", [
        None,
        123,
        "",
        "prod/Game-game.zip",
        "prod/1.75.479435/Game-game.zip",  # only the filename counts
        "prod/Game-1.74.471552-game.zip",  # different minor
        "prod/Game-2.75.479435-game.zip",  # different major
    ])
    def test_falls_back_to_short_version(self, path):
        base = {**_BASE_CONFIG, "game_latest_file_path": path}
        assert japan_build_version(base) == "1.75.0"

    def test_missing_path_key(self):
        assert japan_build_version({"game_latest_version": "1.75.0"}) == "1.75.0"

    def test_launcher_reports_full_version_but_requests_short_one(self, tmp_path):
        urls = []

        def get(url, *args, **kwargs):
            urls.append(url)
            if url.endswith("/api/launcher/game/config"):
                return _ok({"code": 200, "data": _BASE_CONFIG})
            if "download/cdn" in url:
                return _ok({"code": 200, "data": {"primary_cdn": "https://cdn.example/"}})
            if "/api/launcher/game/config/json" in url:
                return _ok({"code": 200, "data": {"url": "https://cdn.example/list.json"}})
            if url.endswith("list.json"):
                return _ok({"source": "", "file": [{"path": "x/resources.assets"}]})
            return _ok(content=_build_resources_asset(
                _server_info_config("https://from-launcher/api")
            ))

        session = MagicMock()
        session.get.side_effect = get

        version, _url = resolve_japan_server_info_url(session, temp_dir=tmp_path)

        assert version == "1.75.479435"
        [json_config_url] = [u for u in urls if "/game/config/json" in u]
        assert "version=1.75.0&" in json_config_url


def _japan_catalog_session():
    addressables = {"ConnectionGroups": [{"OverrideConnectionGroups": [
        {}, {"AddressablesCatalogUrlRoot": "https://cdn.example/root"},
    ]}]}
    bundles = {"UpdatePacks": [], "FullPatchPacks": [{
        "PackName": "a.zip", "Crc": 1, "PackSize": 1, "BundleFiles": [{"Name": "x"}],
    }]}
    session = MagicMock()
    session.get.side_effect = lambda url, **kwargs: _ok(
        addressables if url == "https://api.example" else bundles
    )
    return session


class TestApiUrlCacheAcrossSources:
    """The launcher and XAPK paths must agree on the cached version."""

    @pytest.mark.parametrize("launcher_failures", [
        [ValueError("down once")],
        [ValueError("always down")] * 3,
    ])
    def test_xapk_result_is_reused(self, tmp_path, launcher_failures):
        db = tmp_path / "catalog.db"
        init_database(db)

        with patch("bagfd.yostar.get_yostar_base_config", return_value=_BASE_CONFIG), \
                patch(
                    "bagfd.yostar._japan_api_url_from_launcher",
                    side_effect=launcher_failures,
                ) as launcher, \
                patch(
                    "bagfd.yostar._japan_api_url_from_xapk",
                    return_value=("1.75.479435", "https://api.example"),
                ) as xapk:
            changes = [
                fetch_japan_servers(
                    _japan_catalog_session(), db,
                    check_interval=timedelta(seconds=-1),
                    requested_targets=["japan-android"], temp_dir=tmp_path,
                )["japan-android"]
                for _ in range(3)
            ]

        assert launcher.call_count == 1
        assert xapk.call_count == 1
        assert changes == [True, False, False]
        assert get_stored_version(db, "japan-android") == "1.75.479435"
