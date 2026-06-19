"""
Unit tests for bagfd.cli rendering/highlighting — no network required.
"""
import json

from bagfd import FileInfo, PackInfo
from bagfd.cli import _glob_literals, _highlight, _render_query, _want_color
from bagfd.filter import FileFilter as _FF

HL, RST = "\033[1;33m", "\033[0m"


def _global_fi(name="a.bundle"):
    return FileInfo(name=name, platform="global-android", path=f"Android/{name}",
                    url=f"https://cdn/{name}", hash_type="md5", hash_value="h1",
                    size=1024, pack=None)


def _japan_fi(name="x.bundle"):
    pack = PackInfo(name="Pack.zip", url="https://jp/Pack.zip", hash_type="crc32",
                    hash_value="c1", size=8192, files=["x.bundle", "y.bundle"])
    return FileInfo(name=name, platform="japan-android", path=None, url=None,
                    hash_type=None, hash_value=None, size=None, pack=pack)


# ---------------------------------------------------------------------------
# CLI query --format rendering
# ---------------------------------------------------------------------------

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
