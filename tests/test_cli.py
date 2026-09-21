"""
Unit tests for bagfd.cli rendering/highlighting — no network required.
"""
import json
import builtins
import io
import logging
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bagfd import FileInfo, PackInfo
from bagfd import cli as _cli
from bagfd.cli import _glob_literals, _highlight, _render_query, _want_color
from bagfd.filter import FileFilter as _FF
from bagfd.models import TooManyFilesError

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


def _run_cli(argv, *, client=None, input_text=None):
    client = client if client is not None else Mock()
    client.query.return_value = []
    client.download.return_value = SimpleNamespace(
        files=[], count=0, output_dir=Path("./download"), total_bytes=0,
    )
    constructor = Mock(return_value=client)
    stdout, stderr = io.StringIO(), io.StringIO()
    patches = [
        patch.object(sys, "argv", ["bagfd", *argv]),
        patch.object(_cli, "BlueArchiveGameFilesDownloader", constructor),
    ]
    if input_text is not None:
        patches.append(patch.object(builtins, "input", return_value=input_text))
    exit_code = 0
    root_logger = logging.getLogger()
    old_level = root_logger.level
    try:
        with patches[0], patches[1], redirect_stdout(stdout), redirect_stderr(stderr):
            if len(patches) == 3:
                with patches[2]:
                    try:
                        _cli.main()
                    except SystemExit as exc:
                        exit_code = exc.code
            else:
                try:
                    _cli.main()
                except SystemExit as exc:
                    exit_code = exc.code
    finally:
        root_logger.setLevel(old_level)
    return constructor, client, stdout.getvalue(), stderr.getvalue(), exit_code


class TestCliEntrypoint:
    def test_legacy_and_split_forms_route_all_four_commands(self):
        cases = [
            (["query", "global-android", "ch0230"], "query", "global-android", None),
            (["query", "japan", "ios", "ch0230"], "query", "ios", "japan"),
            (["download", "japan-windows", "ch0230", "-y"], "download", "japan-windows", None),
            (["download", "global", "ios", "ch0230", "-y"], "download", "ios", "global"),
            (["update", "global-android", "--force"], "update", "global-android", None),
            (["update", "global", "ios", "--force"], "update", "ios", "global"),
            (["clean", "japan-windows"], "clean", "japan-windows", None),
            (["clean", "japan", "ios"], "clean", "ios", "japan"),
        ]
        for argv, method, platform, server in cases:
            constructor, client, _out, _err, exit_code = _run_cli(argv)
            assert exit_code == 0
            assert constructor.call_count == 1
            call = getattr(client, method).call_args
            if method in ("query", "download"):
                assert call.args == ("ch0230",)
            assert call.kwargs["platform"] == platform
            if server is None:
                assert "server" not in call.kwargs
            else:
                assert call.kwargs["server"] == server

    def test_bulk_all_forms_route_to_shared_selector_api(self):
        cases = [
            (["update", "all"], "update", {"force": False, "platform": "all"}),
            (["update", "all", "ios"], "update", {"force": False, "platform": "ios", "server": "all"}),
            (["update", "all", "windows"], "update", {"force": False, "platform": "windows", "server": "all"}),
            (["update", "all", "all"], "update", {"force": False, "platform": "all", "server": "all"}),
            (["update", "japan", "all"], "update", {"force": False, "platform": "all", "server": "japan"}),
            (["clean", "all"], "clean", {"platform": "all"}),
            (["clean", "all", "windows"], "clean", {"platform": "windows", "server": "all"}),
            (["clean", "global", "all"], "clean", {"platform": "all", "server": "global"}),
        ]
        for argv, method, expected in cases:
            constructor, client, _out, _err, exit_code = _run_cli(argv)
            assert exit_code == 0
            assert constructor.call_count == 1
            assert getattr(client, method).call_args.kwargs == expected

    def test_selector_like_legacy_patterns_remain_literal(self):
        for pattern in ("ios", "japan", "all"):
            constructor, client, _out, _err, exit_code = _run_cli(
                ["query", "global-android", pattern]
            )
            assert exit_code == 0
            assert constructor.call_count == 1
            assert client.query.call_args.args == (pattern,)
            assert client.query.call_args.kwargs["platform"] == "global-android"

    def test_flags_before_and_after_selectors_and_negative_pattern(self):
        constructor, client, _out, _err, exit_code = _run_cli([
            "query", "--format", "json", "global", "ios",
            "--filter-method", "glob", "--color", "never", "--data-dir", "catalog",
            "-q",
            "--", "-negative",
        ])
        assert exit_code == 0, _err
        assert constructor.call_args.kwargs["data_dir"] == Path("catalog")
        assert client.query.call_args.args == ("-negative",)
        assert client.query.call_args.kwargs["platform"] == "ios"
        assert client.query.call_args.kwargs["server"] == "global"
        assert client.query.call_args.kwargs["filter_method"] == "glob"
        assert "[]" in _out

        _constructor, client, _out, _err, exit_code = _run_cli([
            "query", "global", "ios", "ch0230", "--format", "name", "--color", "never",
        ])
        assert exit_code == 0
        assert client.query.call_args.args == ("ch0230",)

        constructor, client, _out, _err, exit_code = _run_cli([
            "download", "global-android", "--workers", "3", "--", "-negative.bundle",
        ])
        assert exit_code == 0
        assert client.download.call_args.args == ("-negative.bundle",)
        assert client.download.call_args.kwargs["platform"] == "global-android"
        assert client.download.call_args.kwargs["workers"] == 3

        constructor, client, _out, _err, exit_code = _run_cli([
            "download", "--data-dir", "catalog", "--proxy", "http://proxy:8080",
            "global", "ios", "ch0230", "--output", "deliver", "--with-path",
            "--verify", "size", "--filter-method", "contains", "--workers", "4", "-y",
        ])
        assert exit_code == 0
        assert constructor.call_args.kwargs == {
            "data_dir": Path("catalog"), "proxy": "http://proxy:8080",
        }
        assert client.download.call_args.args == ("ch0230",)
        assert client.download.call_args.kwargs == {
            "platform": "ios", "server": "global", "output_dir": Path("deliver"),
            "with_path": True, "verify": "size", "filter_method": "contains",
            "workers": 4, "show_progress": True, "max_files": 50,
        }

    def test_invalid_selectors_and_argument_counts_fail_before_client_creation(self):
        invalid = [
            ["query", "global-android"],
            ["download", "global", "ios"],
            ["query", "global", "windows", "ch0230"],
            ["update", "global", "windows"],
            ["update", "global-android", "ios"],
            ["clean", "japan", "ios", "extra"],
            ["update", "windows"],
            ["query", "unknown", "ch0230"],
        ]
        for argv in invalid:
            constructor, _client, _out, err, exit_code = _run_cli(argv)
            assert exit_code == 2
            assert constructor.call_count == 0
            assert "usage:" in err

    def test_clean_split_output_identifies_selection_and_legacy_text_is_unchanged(self):
        _ctor, _client, out, _err, exit_code = _run_cli(["clean", "japan-android"])
        assert exit_code == 0
        assert out == "Cleaned: japan-android\n"
        _ctor, _client, out, _err, exit_code = _run_cli(["clean", "japan", "ios"])
        assert exit_code == 0
        assert out == "Cleaned: japan/ios\n"

    def test_download_confirmation_yes_no_and_yes_flag(self):
        accept = Mock()
        accept.download.side_effect = [TooManyFilesError(51, 50), _empty_download_result()]
        _ctor, client, _out, _err, exit_code = _run_cli(
            ["download", "global-android", "*.bundle"], client=accept, input_text="yes"
        )
        assert exit_code == 0
        assert [call.kwargs["max_files"] for call in client.download.call_args_list] == [50, None]

        split_ios = Mock()
        split_ios.download.side_effect = [TooManyFilesError(51, 50), _empty_download_result()]
        _ctor, client, _out, _err, exit_code = _run_cli(
            ["download", "japan", "ios", "ch0230"], client=split_ios, input_text="yes"
        )
        assert exit_code == 0
        assert _ctor.call_count == 1
        assert [call.args for call in client.download.call_args_list] == [
            ("ch0230",), ("ch0230",),
        ]
        assert [call.kwargs["server"] for call in client.download.call_args_list] == [
            "japan", "japan",
        ]
        assert [call.kwargs["platform"] for call in client.download.call_args_list] == [
            "ios", "ios",
        ]
        assert [call.kwargs["max_files"] for call in client.download.call_args_list] == [
            50, None,
        ]

        decline = Mock()
        decline.download.side_effect = TooManyFilesError(51, 50)
        _ctor, client, _out, err, exit_code = _run_cli(
            ["download", "global-android", "*.bundle"], client=decline, input_text="n"
        )
        assert exit_code == 0
        assert client.download.call_count == 1
        assert "Aborted." in err

        yes_flag = Mock()
        yes_flag.download.side_effect = [TooManyFilesError(51, 50), _empty_download_result()]
        _ctor, client, _out, _err, exit_code = _run_cli(
            ["download", "global-android", "*.bundle", "-y"], client=yes_flag
        )
        assert exit_code == 0
        assert [call.kwargs["max_files"] for call in client.download.call_args_list] == [50, None]

    def test_runtime_error_keeps_exit_code_one(self):
        client = Mock()
        client.update.side_effect = RuntimeError("update failed")
        _ctor, _client, _out, _err, exit_code = _run_cli(["update", "global-android"], client=client)
        assert exit_code == 1
        assert client.update.call_count == 1


def _empty_download_result():
    return SimpleNamespace(files=[], count=0, output_dir=Path("./download"), total_bytes=0)
