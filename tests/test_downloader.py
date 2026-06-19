"""
Unit tests for bagfd.downloader — no network required.
"""
import hashlib
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
