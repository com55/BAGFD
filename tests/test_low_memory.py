"""Bounded-memory regression tests for the Japan bootstrap and ZIP extraction."""
import hashlib
import io
import os
import random
import struct
import tempfile
import zipfile
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from bagfd import BlueArchiveGameFilesDownloader, FileInfo, PackInfo
from bagfd import client as client_mod
from bagfd import fetchers, yostar
from bagfd.database import init_database, update_version
from test_yostar import _build_resources_asset, _server_info_config

CHUNK = 64 * 1024
URL = "https://bootstrap.example/api"
# The 20-byte pattern used by the XAPK path.
PATTERN = yostar.GAME_CONFIG_PATTERN


def _filler(size: int, seed: int = 0) -> bytes:
    # Incompressible, so deflated members also span several chunks.
    return random.Random(seed).randbytes(size)


def _asset(prefix_len: int = 3 * CHUNK) -> bytes:
    core = _build_resources_asset(_server_info_config(URL))[len(b"PAD"):]
    return _filler(prefix_len) + core + _filler(2 * CHUNK, 1)


class _Response:
    """A streaming response that forbids reading the whole body at once."""

    def __init__(self, body=b"", *, error=None, status_error=None):
        self.body = body
        self.error = error
        self.status_error = status_error
        self.closed = False
        self.chunk_sizes = []

    @property
    def content(self):
        raise AssertionError(".content must not be used for large downloads")

    def raise_for_status(self):
        if self.status_error:
            raise self.status_error

    def iter_content(self, chunk_size=1):
        self.chunk_sizes.append(chunk_size)
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class _BoundedFile:
    """Wrap a temp file and reject reads that would return a large buffer."""

    # zipfile reads the end-of-central-directory tail with a bare read().
    LIMIT = CHUNK + 1024

    def __init__(self, raw):
        self.raw = raw

    def read(self, n=-1):
        if n is None or n < 0:
            position = self.raw.tell()
            requested = self.raw.seek(0, os.SEEK_END) - position
            self.raw.seek(position)
        else:
            requested = n
        assert requested <= self.LIMIT, f"unbounded read of {requested} bytes"
        return self.raw.read(n)

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.raw.close()


@pytest.fixture
def temp_files(monkeypatch):
    """Record every temporary file and enforce bounded reads on it."""
    created = []
    real = tempfile.TemporaryFile

    def factory(*args, **kwargs):
        wrapped = _BoundedFile(real(*args, **kwargs))
        created.append((wrapped, kwargs.get("dir")))
        return wrapped

    monkeypatch.setattr(tempfile, "TemporaryFile", factory)
    return created


@pytest.fixture
def bounded_zip(monkeypatch):
    """Forbid whole-member zip reads."""
    real = zipfile.ZipExtFile.read

    def read(self, n=-1):
        assert n is not None and 0 <= n <= CHUNK, f"unbounded member read: {n}"
        return real(self, n)

    def forbidden(self, *args, **kwargs):
        raise AssertionError("ZipFile.read loads a whole member")

    monkeypatch.setattr(zipfile.ZipExtFile, "read", read)
    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden)


def _assert_cleaned(temp_files, directory):
    assert temp_files, "expected temporary files to be used"
    assert all(f.raw.closed for f, _dir in temp_files)
    assert all(d == directory for _f, d in temp_files)
    assert list(directory.iterdir()) == []


def _run_launcher(response, temp_dir, expected_hash=""):
    session = MagicMock()
    session.get.return_value = response
    with patch.object(
        yostar, "get_yostar_base_config",
        return_value={"game_latest_version": "1.2.3"},
    ), patch.object(
        yostar, "get_resources_asset_url",
        return_value=("https://cdn.example/resources.assets", {"hash": expected_hash}),
    ):
        return yostar._japan_api_url_from_launcher(session, temp_dir)


class TestLauncherDownload:
    def test_streams_to_disk_with_bounded_reads(self, tmp_path, temp_files):
        blob = _asset()
        response = _Response(blob)
        result = _run_launcher(response, tmp_path / "tmp", hashlib.md5(blob).hexdigest())

        assert result == ("1.2.3", URL)
        assert response.chunk_sizes == [CHUNK]
        assert response.closed
        _assert_cleaned(temp_files, tmp_path / "tmp")

    def test_hash_mismatch_raises_even_with_valid_config(self, tmp_path, temp_files):
        response = _Response(_asset())
        with patch.object(yostar, "_extract_game_main_from_file") as extract:
            with pytest.raises(ValueError, match="hash mismatch"):
                _run_launcher(response, tmp_path, "0" * 32)
        extract.assert_not_called()
        assert response.closed
        _assert_cleaned(temp_files, tmp_path)

    def test_hash_covers_bytes_after_the_config(self, tmp_path):
        blob = _asset()
        expected = hashlib.md5(blob).hexdigest()
        corrupted = blob[:-1] + bytes([blob[-1] ^ 1])
        with pytest.raises(ValueError, match="hash mismatch"):
            _run_launcher(_Response(corrupted), tmp_path, expected)

    @pytest.mark.parametrize("kwargs", [
        {"error": requests.exceptions.ChunkedEncodingError("cut")},
        {"status_error": requests.HTTPError("404")},
    ])
    def test_network_failure_cleans_up(self, tmp_path, temp_files, kwargs):
        response = _Response(_asset(), **kwargs)
        with pytest.raises(requests.RequestException):
            _run_launcher(response, tmp_path)
        assert response.closed
        _assert_cleaned(temp_files, tmp_path)

    def test_write_failure_cleans_up(self, tmp_path, temp_files, monkeypatch):
        def fail(self, data):
            raise OSError("disk full")

        monkeypatch.setattr(_BoundedFile, "write", fail, raising=False)
        response = _Response(_asset())
        with pytest.raises(OSError, match="disk full"):
            _run_launcher(response, tmp_path)
        assert response.closed
        _assert_cleaned(temp_files, tmp_path)

    def test_read_failure_cleans_up(self, tmp_path, temp_files, monkeypatch):
        def fail(self, n=-1):
            raise OSError("read failed")

        monkeypatch.setattr(_BoundedFile, "read", fail)
        with pytest.raises(OSError, match="read failed"):
            _run_launcher(_Response(_asset()), tmp_path)
        _assert_cleaned(temp_files, tmp_path)


class TestFileExtraction:
    # Split points covering the marker, the size field and the payload start.
    @pytest.mark.parametrize("before_boundary", range(0, 24))
    def test_marker_straddles_chunk_boundary(self, before_boundary):
        blob = _asset(CHUNK - before_boundary)
        assert yostar._extract_game_main_from_file(io.BytesIO(blob)) == URL

    @pytest.mark.parametrize("before_boundary", range(0, 24))
    def test_full_pattern_straddles_chunk_boundary(self, before_boundary):
        blob = _filler(CHUNK - before_boundary) + PATTERN + b"x" * 0x392
        assert (
            yostar._find_in_file(io.BytesIO(blob), PATTERN)
            == CHUNK - before_boundary
        )

    def test_full_pattern_wins_over_earlier_bare_marker(self):
        decoy = b"GameMainConfig\x00\x00" + struct.pack("<i", 4) + b"junk"
        blob = decoy + _filler(CHUNK) + PATTERN + b"p" * 0x392
        with patch.object(yostar, "_decrypt_game_main_payload", side_effect=bytes) as dec:
            yostar._extract_game_main_from_file(io.BytesIO(blob))
        dec.assert_called_once_with(b"p" * 0x392)

    @pytest.mark.parametrize("tail", [
        b"",
        b"\x01\x00",
        struct.pack("<i", 0),
        struct.pack("<i", -5),
        struct.pack("<i", 100) + b"short",
    ])
    def test_corrupt_or_truncated_payload_matches_blob_behaviour(self, tail):
        blob = _filler(CHUNK - 7) + b"GameMainConfig\x00\x00" + tail
        with pytest.raises(ValueError, match="payload size out of range"):
            yostar.extract_game_main_from_resources(blob)
        with pytest.raises(ValueError, match="payload size out of range"):
            yostar._extract_game_main_from_file(io.BytesIO(blob))

    def test_missing_marker(self):
        with pytest.raises(ValueError, match="pattern not found"):
            yostar._extract_game_main_from_file(io.BytesIO(_filler(3 * CHUNK)))


def _zip_bytes(members, compression) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return buffer.getvalue()


def _xapk(members, compression=zipfile.ZIP_DEFLATED) -> bytes:
    apk = _zip_bytes(members, compression)
    return _zip_bytes([("UnityDataAssetPack.apk", apk)], compression)


def _run_xapk(xapk, temp_dir, response=None):
    page = MagicMock()
    page.text = "ver 1.2.3 XAPKJ..https://download.example.com/game.xapk"
    session = MagicMock()
    session.get.side_effect = [page, response or _Response(xapk)]
    return yostar._japan_api_url_from_xapk(session, temp_dir)


class TestXapkFallback:
    @pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
    @pytest.mark.parametrize("before_boundary", [0, 1, 19, 20, 21, 500])
    def test_nested_apk_with_bounded_reads(
        self, tmp_path, temp_files, bounded_zip, compression, before_boundary,
    ):
        payload = _filler(1024, 7)
        member = (
            _filler(2 * CHUNK - before_boundary) + PATTERN + payload + _filler(CHUNK, 2)
        )
        xapk = _xapk([
            ("assets/bin/Data/first", _filler(2 * CHUNK, 3)),
            ("assets/other", PATTERN + b"ignored"),
            ("assets/bin/Data/config", member),
            ("assets/bin/Data/later", PATTERN + b"never reached"),
        ], compression)

        with patch.object(fetchers, "_decrypt_japan_config", return_value=URL) as dec:
            assert _run_xapk(xapk, tmp_path) == ("1.2.3", URL)

        dec.assert_called_once_with(payload)
        assert len(temp_files) == 2
        _assert_cleaned(temp_files, tmp_path)

    def test_short_tail_after_pattern(self, tmp_path):
        xapk = _xapk([("assets/bin/Data/config", _filler(CHUNK - 5) + PATTERN + b"abc")])
        with patch.object(fetchers, "_decrypt_japan_config", return_value=URL) as dec:
            _run_xapk(xapk, tmp_path)
        dec.assert_called_once_with(b"abc")

    def test_missing_config(self, tmp_path, temp_files):
        with pytest.raises(ValueError, match="Could not find game config"):
            _run_xapk(_xapk([("assets/bin/Data/x", b"nothing")]), tmp_path)
        _assert_cleaned(temp_files, tmp_path)

    def test_corrupt_member_after_match_fails_crc(self, tmp_path, temp_files):
        trailer = b"TRAILER-TO-CORRUPT"
        member = PATTERN + _filler(2 * CHUNK) + trailer
        apk = _zip_bytes([("assets/bin/Data/config", member)], zipfile.ZIP_STORED)
        apk = apk.replace(trailer, trailer.lower())
        xapk = _zip_bytes([("UnityDataAssetPack.apk", apk)], zipfile.ZIP_STORED)

        with patch.object(fetchers, "_decrypt_japan_config") as dec:
            with pytest.raises(zipfile.BadZipFile):
                _run_xapk(xapk, tmp_path)
        dec.assert_not_called()
        _assert_cleaned(temp_files, tmp_path)

    @pytest.mark.parametrize("data", [b"not a zip", _zip_bytes([("a", b"b")], 0)])
    def test_bad_xapk_cleans_up(self, tmp_path, temp_files, data):
        with pytest.raises((zipfile.BadZipFile, KeyError)):
            _run_xapk(data, tmp_path)
        _assert_cleaned(temp_files, tmp_path)

    def test_network_failure_cleans_up(self, tmp_path, temp_files):
        response = _Response(b"partial", error=requests.ConnectionError("cut"))
        with pytest.raises(requests.ConnectionError):
            _run_xapk(b"", tmp_path, response)
        assert response.closed
        _assert_cleaned(temp_files, tmp_path)


class TestTempDirWiring:
    def test_default_is_under_data_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BAGFD_DATA_DIR", str(tmp_path))
        with patch.object(yostar, "_japan_api_url_from_launcher") as launcher:
            yostar.resolve_japan_server_info_url(MagicMock())
        assert launcher.call_args.args[1] == tmp_path / "tmp"

    def test_client_passes_its_data_dir(self, tmp_path):
        downloader = BlueArchiveGameFilesDownloader(
            data_dir=tmp_path / "data", db_path=tmp_path / "db" / "catalog.db",
        )
        with patch.object(client_mod, "fetch_japan_servers", return_value={}) as fetch:
            downloader.update(platform="japan-android")
        assert fetch.call_args.kwargs["temp_dir"] == tmp_path / "data" / "tmp"

    def test_disk_error_defers_and_keeps_cached_catalog(self, tmp_path):
        db = tmp_path / "catalog.db"
        init_database(db)
        update_version(db, "japan-android", "1.0.0")
        with patch.object(
            yostar, "resolve_japan_server_info_url", side_effect=OSError("disk full"),
        ) as resolve:
            result = fetchers.fetch_japan_servers(
                MagicMock(), db, force=True, check_interval=timedelta(hours=4),
                requested_targets=["japan-android"], temp_dir=tmp_path / "tmp",
            )
        assert result == {"japan-android": False}
        assert resolve.call_args.kwargs == {"temp_dir": tmp_path / "tmp"}


def _japan_pack(tmp_path, data: bytes):
    downloader = BlueArchiveGameFilesDownloader(data_dir=tmp_path / "data")
    zip_path = downloader.zip_cache / "japan-android" / "Pack.zip"
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(
        _zip_bytes([("dir/member.bundle", data)], zipfile.ZIP_STORED)
    )
    pack = PackInfo(
        name="Pack.zip", url="https://jp.example/Pack.zip", hash_type=None,
        hash_value=None, size=zip_path.stat().st_size, files=["dir/member.bundle"],
    )
    return downloader, zip_path, {"Pack.zip": (pack, {"dir/member.bundle"})}


class TestJapanExtraction:
    def _extract(self, downloader, zip_path, packs, out, overwrite=True):
        return downloader._extract_japan(
            packs, zip_path.parent, out, with_path=True, overwrite=overwrite,
        )

    def test_bounded_copy_produces_identical_file(self, tmp_path, bounded_zip):
        data = _filler(5 * CHUNK + 123)
        downloader, zip_path, packs = _japan_pack(tmp_path, data)
        out = tmp_path / "out"

        delivered = self._extract(downloader, zip_path, packs, out)

        assert delivered == [out / "dir/member.bundle"]
        assert delivered[0].read_bytes() == data
        assert list(delivered[0].parent.iterdir()) == delivered
        assert zip_path.exists()

    @pytest.mark.parametrize("failure", ["crc", "read", "write", "replace"])
    @pytest.mark.parametrize("existing", [True, False])
    def test_failure_preserves_destination(
        self, tmp_path, monkeypatch, failure, existing,
    ):
        data = _filler(3 * CHUNK) + b"TRAILER-TO-CORRUPT"
        downloader, zip_path, packs = _japan_pack(tmp_path, data)
        target = tmp_path / "out" / "dir" / "member.bundle"
        target.parent.mkdir(parents=True)
        if existing:
            target.write_bytes(b"original")

        def fail(*args, **kwargs):
            raise OSError(failure)

        def partial_write(src, dst, length=0):
            dst.write(src.read(length))
            fail()

        if failure == "crc":
            zip_path.write_bytes(zip_path.read_bytes().replace(
                b"TRAILER-TO-CORRUPT", b"trailer-to-corrupt",
            ))
        elif failure == "read":
            monkeypatch.setattr(zipfile.ZipExtFile, "read", fail)
        elif failure == "write":
            monkeypatch.setattr(client_mod.shutil, "copyfileobj", partial_write)
        else:
            monkeypatch.setattr(client_mod.os, "replace", fail)

        with pytest.raises((OSError, zipfile.BadZipFile)):
            self._extract(downloader, zip_path, packs, tmp_path / "out")

        if existing:
            assert target.read_bytes() == b"original"
            assert list(target.parent.iterdir()) == [target]
        else:
            assert list(target.parent.iterdir()) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
    def test_permissions_match_a_plain_write(self, tmp_path):
        downloader, zip_path, packs = _japan_pack(tmp_path, b"data")
        reference = tmp_path / "reference"
        reference.write_bytes(b"")

        [target] = self._extract(downloader, zip_path, packs, tmp_path / "out")
        assert target.stat().st_mode == reference.stat().st_mode

        target.chmod(0o640)
        self._extract(downloader, zip_path, packs, tmp_path / "out")
        assert target.stat().st_mode & 0o777 == 0o640

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges")
    def test_symlinked_destination_is_written_through(self, tmp_path):
        downloader, zip_path, packs = _japan_pack(tmp_path, b"new")
        out = tmp_path / "out"
        (out / "dir").mkdir(parents=True)
        real = out / "real.bundle"
        real.write_bytes(b"old")
        (out / "dir" / "member.bundle").symlink_to(real)

        [target] = self._extract(downloader, zip_path, packs, out)

        assert target.is_symlink()
        assert real.read_bytes() == b"new"

    def test_cached_zip_is_reused_without_network(self, tmp_path):
        downloader, zip_path, packs = _japan_pack(tmp_path, b"data")
        pack = packs["Pack.zip"][0]
        record = FileInfo(
            name="dir/member.bundle", platform="japan-android", path=None, url=None,
            hash_type=None, hash_value=None, size=None, pack=pack,
        )
        before = zip_path.read_bytes()
        with patch.object(downloader.session, "get") as get:
            for name in ("first", "second"):
                result = downloader.download_files(
                    [record], tmp_path / name, verify="size",
                )
                assert [p.read_bytes() for p in result.files] == [b"data"]
        get.assert_not_called()
        assert zip_path.read_bytes() == before
