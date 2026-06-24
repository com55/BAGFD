"""
Unit tests for bagfd.fetchers — the maintenance-defer fallback.

These exercise the path where the game server is mid version-update: a new
version is detected but the catalog can't be fetched. The fetcher must keep the
cached catalog, park a defer window, and return "no new version" instead of
raising.
"""
import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import requests

from bagfd.database import (
    get_stored_version,
    init_database,
    update_version,
)
from bagfd.fetchers import fetch_global_android, fetch_japan_servers


def _read_defer(db, platform):
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT defer_until FROM versions WHERE platform = ?", (platform,)).fetchone()
    conn.close()
    return row[0] if row else None


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

        # PureAPK page text must satisfy both the version regex and the APK-url regex.
        pureapk = _ok(text="XAPKJ: https://example.com/app.xapk build 1.70.436321")
        xapk = _ok(content=b"fake-xapk-bytes")
        bad_api = _ok(json_exc=json.JSONDecodeError("Expecting value", "", 0))

        def get_side(url, *args, **kwargs):
            if "pureapk" in url:
                return pureapk
            if url.endswith(".xapk"):
                return xapk
            return bad_api  # the addressable api_url

        session = MagicMock()
        session.get.side_effect = get_side

        with patch("bagfd.fetchers._extract_japan_api_url", return_value="https://fake/api"):
            results = fetch_japan_servers(session, db, force=True)

        assert results == {"japan-android": False, "japan-windows": False}  # no new version, no raise
        assert get_stored_version(db, "japan-android") == "1.69.0"          # stale kept
        assert _read_defer(db, "japan-android") is not None
        assert _read_defer(db, "japan-windows") is not None
