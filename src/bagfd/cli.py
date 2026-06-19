"""Command-line interface for the Blue Archive Game Files Downloader.

Commands:
    update PLATFORM            refresh the file catalog
    clean  PLATFORM            clear cached files and catalog rows
    query  PLATFORM PATTERN    search the catalog (single platform)
    download PLATFORM PATTERN  download matching files into a directory

``update``/``clean`` accept ``all``; ``query``/``download`` take one platform.
"""
import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stderr,
)

from .client import BlueArchiveGameFilesDownloader
from .enums import FilterMethod, Platform, VerifyMethod
from .filter import FileFilter
from .models import TooManyFilesError

_PLATFORMS = [p.value for p in Platform]
_PLATFORM_CHOICES_ALL = ['all'] + _PLATFORMS
_PLATFORM_HELP = "platform: all, global-android, japan-android, japan-windows"
_PLATFORM_HELP_ONE = "platform: global-android, japan-android, japan-windows"
_DATA_DIR_HELP = "data directory for catalog DB + zip cache (overrides BAGFD_DATA_DIR)"
_PROXY_HELP = "HTTP/HTTPS proxy URL, e.g. http://proxy:8080"
_FILTER_METHOD_HELP = "pattern matching: auto (default), glob, regex, contains, starts_with, ends_with"
_VERIFY_HELP = "cache reuse check: hash (default), size, none"
_QUERY_FORMATS = ['table', 'name', 'url', 'path', 'json']
_FORMAT_HELP = "output format: table (default), name, url, path, json"
_COLOR_CHOICES = ['auto', 'always', 'never']
_COLOR_HELP = "highlight matched text: auto (default, only on a terminal), always, never"

_HL = "\033[1;33m"   # bold yellow
_RST = "\033[0m"


def _want_color(mode: str) -> bool:
    """Whether to emit ANSI highlight, honouring ``--color`` and ``NO_COLOR``."""
    if mode == 'always':
        return True
    if mode == 'never':
        return False
    return sys.stdout.isatty() and not os.environ.get('NO_COLOR')


def _glob_literals(pattern: str) -> list[str]:
    """Return the literal substrings of a glob pattern (the non-wildcard runs).

    e.g. ``*ch0171*`` -> ``["ch0171"]``; ``ch0171*.bundle`` -> ``["ch0171", ".bundle"]``.
    ``*``, ``?`` and ``[...]`` classes act as separators.
    """
    lits, cur, i = [], [], 0
    while i < len(pattern):
        c = pattern[i]
        if c in '*?':
            if cur:
                lits.append(''.join(cur)); cur = []
            i += 1
        elif c == '[':
            if cur:
                lits.append(''.join(cur)); cur = []
            j = pattern.find(']', i + 1)
            i = j + 1 if j != -1 else i + 1
        else:
            cur.append(c); i += 1
    if cur:
        lits.append(''.join(cur))
    return [s for s in lits if s]


def _match_spans(name: str, filt: FileFilter) -> list[tuple[int, int]]:
    """Return (start, end) spans of ``name`` that ``filt`` matched on."""
    p, method = filt.pattern, filt.filter_method
    if method == FilterMethod.CONTAINS:
        i = name.find(p)
        return [(i, i + len(p))] if p and i >= 0 else []
    if method == FilterMethod.STARTS_WITH:
        return [(0, len(p))] if p and name.startswith(p) else []
    if method == FilterMethod.ENDS_WITH:
        return [(len(name) - len(p), len(name))] if p and name.endswith(p) else []
    if method == FilterMethod.REGEX:
        mo = re.search(p, name)
        return [mo.span()] if mo and mo.end() > mo.start() else []
    if method == FilterMethod.GLOB:
        spans, cur = [], 0
        for lit in _glob_literals(p):
            i = name.find(lit, cur)
            if i < 0:
                continue
            spans.append((i, i + len(lit)))
            cur = i + len(lit)
        return spans
    return []


def _highlight(name: str, filt: FileFilter) -> str:
    """Wrap the matched span(s) of ``name`` in ANSI highlight codes."""
    spans = sorted(s for s in _match_spans(name, filt) if s[0] < s[1])
    if not spans:
        return name
    merged = [spans[0]]
    for s, e in spans[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    out, prev = [], 0
    for s, e in merged:
        out += [name[prev:s], _HL, name[s:e], _RST]
        prev = e
    out.append(name[prev:])
    return ''.join(out)


def _fi_dict(fi) -> dict:
    """Serialise a FileInfo (and its pack, if any) to a plain dict for JSON."""
    return {
        'name': fi.name,
        'platform': fi.platform,
        'path': fi.path,
        'url': fi.url,
        'hash_type': fi.hash_type,
        'hash_value': fi.hash_value,
        'size': fi.size,
        'pack': None if fi.pack is None else {
            'name': fi.pack.name,
            'url': fi.pack.url,
            'hash_type': fi.pack.hash_type,
            'hash_value': fi.pack.hash_value,
            'size': fi.pack.size,
            'files': fi.pack.files,
        },
    }


def _render_query(results, fmt: str, highlight: FileFilter | None = None) -> str:
    """Render query results in the requested format.

    ``table`` is human-readable (and ends with a count summary). The other
    formats emit only data — one value per line, or a JSON array — so they pipe
    cleanly. Global files carry their own url/size/hash; for Japan that info
    lives on the pack, so ``url`` falls back to the pack URL (de-duplicated) and
    ``path`` falls back to the bundle name.

    If ``highlight`` (the active filter) is given, the matched span of each
    filename is wrapped in ANSI codes — only for the ``table`` and ``name``
    formats, never for the machine-readable ones.
    """
    def hl(name: str) -> str:
        return _highlight(name, highlight) if highlight else name

    if fmt == 'name':
        return "\n".join(hl(fi.name) for fi in results)
    if fmt == 'path':
        return "\n".join((fi.path if fi.path is not None else fi.name) for fi in results)
    if fmt == 'url':
        lines, seen = [], set()
        for fi in results:
            u = fi.url if fi.pack is None else fi.pack.url
            if u and u not in seen:
                seen.add(u)
                lines.append(u)
        return "\n".join(lines)
    if fmt == 'json':
        return json.dumps([_fi_dict(fi) for fi in results], ensure_ascii=False, indent=2)
    # table — size first (right-aligned, fixed width) so the column lines up;
    # the long, variable-length filename goes last.
    rows = results
    if rows and rows[0].pack is not None:
        # Japan: the leading size is the pack's, so show the pack right after it.
        # Group by pack (sort by pack name, then filename).
        rows = sorted(rows, key=lambda fi: (fi.pack.name, fi.name))
    lines = []
    for fi in rows:
        if fi.pack is None:
            lines.append(f"{_human_size(fi.size):>9}  {hl(fi.name)}")
        else:
            lines.append(f"{_human_size(fi.pack.size):>9}  {fi.pack.name}  {hl(fi.name)}")
    lines.append(f"\n{len(results)} file(s) found.")
    return "\n".join(lines)


def _human_size(n: int | None) -> str:
    """Format a byte count as a short human-readable string."""
    if n is None:
        return "?"
    size = float(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024:
            return f"{int(size)}{unit}" if unit == 'B' else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def main():
    parser = argparse.ArgumentParser(description="Blue Archive Game Files Downloader.")
    subparsers = parser.add_subparsers(dest='command', required=True)

    # Shared options every subcommand accepts (e.g. `bagfd query ... -q`).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('-q', '--quiet', action='store_true',
                        help="suppress progress logs on stderr (errors still shown)")

    p_update = subparsers.add_parser(
        'update', parents=[common],
        help="Fetch latest game file catalog.",
        usage="bagfd update PLATFORM [--force] [--proxy URL] [--data-dir DIR] [-q]",
    )
    p_update.add_argument('platform', choices=_PLATFORM_CHOICES_ALL, metavar='PLATFORM', help=_PLATFORM_HELP)
    p_update.add_argument('--force', action='store_true', help="fetch even if catalog is already up to date")
    p_update.add_argument('--proxy', default=None, metavar='URL', help=_PROXY_HELP)
    p_update.add_argument('--data-dir', type=Path, default=None, metavar='DIR', help=_DATA_DIR_HELP)

    p_clean = subparsers.add_parser(
        'clean', parents=[common],
        help="Clear cached files and DB entries.",
        usage="bagfd clean PLATFORM [--data-dir DIR] [-q]",
    )
    p_clean.add_argument('platform', choices=_PLATFORM_CHOICES_ALL, metavar='PLATFORM', help=_PLATFORM_HELP)
    p_clean.add_argument('--data-dir', type=Path, default=None, metavar='DIR', help=_DATA_DIR_HELP)

    p_query = subparsers.add_parser(
        'query', parents=[common],
        help="Search filenames in catalog.",
        usage="bagfd query PLATFORM PATTERN [--format FORMAT] [--filter-method METHOD] [--data-dir DIR] [-q]",
    )
    p_query.add_argument('platform', choices=_PLATFORMS, metavar='PLATFORM', help=_PLATFORM_HELP_ONE)
    p_query.add_argument('pattern', help="filename pattern to match")
    p_query.add_argument('--format', choices=_QUERY_FORMATS, default='table', metavar='FORMAT', help=_FORMAT_HELP)
    p_query.add_argument('--color', choices=_COLOR_CHOICES, default='auto', metavar='WHEN', help=_COLOR_HELP)
    p_query.add_argument('--filter-method', choices=[m.value for m in FilterMethod],
                         default='auto', metavar='METHOD', help=_FILTER_METHOD_HELP)
    p_query.add_argument('--data-dir', type=Path, default=None, metavar='DIR', help=_DATA_DIR_HELP)

    p_download = subparsers.add_parser(
        'download', parents=[common],
        help="Download matching files into a directory.",
        usage="bagfd download PLATFORM PATTERN [-o DIR] [--with-path] [--verify MODE] "
              "[--filter-method METHOD] [--workers N] [--proxy URL] [--data-dir DIR] [-y] [-q]",
    )
    p_download.add_argument('platform', choices=_PLATFORMS, metavar='PLATFORM', help=_PLATFORM_HELP_ONE)
    p_download.add_argument('pattern', help="filename pattern to match")
    p_download.add_argument('-o', '--output', type=Path, default=Path('./download'), metavar='DIR',
                            help="output directory (default: ./download)")
    p_download.add_argument('--with-path', action='store_true',
                            help="recreate each file's original directory structure under the output dir")
    p_download.add_argument('--verify', choices=[v.value for v in VerifyMethod],
                            default='hash', metavar='MODE', help=_VERIFY_HELP)
    p_download.add_argument('--filter-method', choices=[m.value for m in FilterMethod],
                            default='auto', metavar='METHOD', help=_FILTER_METHOD_HELP)
    p_download.add_argument('--workers', type=int, default=10, metavar='N', help="parallel download workers (default: 10)")
    p_download.add_argument('--proxy', default=None, metavar='URL', help=_PROXY_HELP)
    p_download.add_argument('--data-dir', type=Path, default=None, metavar='DIR', help=_DATA_DIR_HELP)
    p_download.add_argument('--yes', '-y', action='store_true', help="skip confirmation when downloading more than 50 files")

    args = parser.parse_args()

    if getattr(args, 'quiet', False):
        logging.getLogger().setLevel(logging.ERROR)

    try:
        client = BlueArchiveGameFilesDownloader(
            data_dir=args.data_dir,
            proxy=getattr(args, 'proxy', None),
        )
        platform = args.platform

        if args.command == 'update':
            client.update(force=args.force, platform=platform)

        elif args.command == 'clean':
            client.clean(platform=platform)
            print(f"Cleaned: {platform}")

        elif args.command == 'query':
            results = client.query(args.pattern, platform=platform, filter_method=args.filter_method)
            highlight = FileFilter(args.pattern, args.filter_method) if _want_color(args.color) else None
            print(_render_query(results, args.format, highlight))

        elif args.command == 'download':
            def _run(max_files=50):
                return client.download(
                    args.pattern,
                    platform=platform,
                    output_dir=args.output,
                    with_path=args.with_path,
                    verify=args.verify,
                    filter_method=args.filter_method,
                    workers=args.workers,
                    show_progress=True,
                    max_files=max_files,
                )
            try:
                result = _run()
            except TooManyFilesError as e:
                if args.yes:
                    result = _run(max_files=None)
                else:
                    print(f"Warning: {e.count} files match '{args.pattern}'. Download all? [y/N] ", end='', file=sys.stderr)
                    sys.stderr.flush()
                    if input().strip().lower() in ('y', 'yes'):
                        result = _run(max_files=None)
                    else:
                        print("Aborted.", file=sys.stderr)
                        raise SystemExit(0)
            for p in result.files:
                print(p)
            print(f"\n{result.count} file(s) downloaded to {result.output_dir} "
                  f"({_human_size(result.total_bytes)}).")

    except Exception as e:
        logging.error("%s", e)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
