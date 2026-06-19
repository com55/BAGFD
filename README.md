# BAGFD

Blue Archive Game Files Downloader — inspired by / based on [BA-AD](https://github.com/Deathemonic/BA-AD).

Downloads game bundle files from Blue Archive servers across 3 platforms:
- Global Android
- Japan Android
- Japan Windows

## Install

```bash
uv add git+https://github.com/com55/BAGFD@main
```

## CLI

```
bagfd <command> <platform> [pattern] [flags]
```

**Platform**: `update`/`clean` accept `all`; `query`/`download` take a single
platform (`global-android`, `japan-android`, `japan-windows`).

```bash
# Fetch latest catalog
bagfd update all
bagfd update global-android --force
bagfd update all --proxy http://proxy:8080

# Search filenames (single platform)
bagfd query global-android 'ch0230'
bagfd query global-android '*.bundle'
bagfd query japan-android '^ch\d+' --filter-method regex
bagfd query global-android 'ch0230' --format name     # filenames only, one per line
bagfd query global-android 'ch0230' --format url      # download URLs
bagfd query japan-android 'ch0230'  --format json     # full structured output

# Download files into a directory (default ./download)
bagfd download global-android 'ch0230'
bagfd download global-android '*.bundle' -o ./out --workers 20
bagfd download japan-android 'ch0230' --with-path        # keep original folder structure
bagfd download global-android 'ch0230' --verify size     # cheaper cache check
bagfd download global-android '*.bundle' -y              # skip the >50-file prompt

# Clear cache and DB entries
bagfd clean all
bagfd clean japan-android
```

**`query --format`**: `table` (default, human-readable with a count summary;
Global shows `<size>  <name>`, Japan shows `<pack-size>  <pack>  <name>` grouped
by pack), `name` (filenames), `url` (download URLs — direct for Global,
deduplicated pack URLs for Japan), `path` (game-tree path; bundle name for
Japan), `json` (full structured records). The non-`table` formats emit only data.

**`query --color`** highlights the matched part of each filename in the `table`
and `name` formats: `auto` (default — only on a terminal), `always`, `never`
(also honours `NO_COLOR`). For glob patterns only the literal parts are
highlighted (`*ch0171*` → highlights `ch0171`).

**Piping:** all command output goes to **stdout**; progress logs go to
**stderr**, and highlight colour auto-disables when output isn't a terminal. So
`bagfd query … --format json | jq …` and `… > out.txt` capture clean data even
when the catalog auto-updates. Add **`-q`/`--quiet`** (any command) to silence
the stderr logs entirely.

**`--filter-method`**: `auto` (default), `glob`, `regex`, `contains`, `starts_with`, `ends_with`

Auto-detection: patterns with `*`, `?`, `[` → glob; patterns with `^`, `$`, `\`, `+`, `|`, `(` → regex; otherwise → contains.

**`--verify`** (download): how a cached file is reused — `hash` (default, md5/crc32),
`size`, or `none` (reuse if present).

**`--data-dir`** priority: flag > `BAGFD_DATA_DIR` env var > `platformdirs.user_data_dir("BAGFD")`
(`XDG_DATA_HOME/BAGFD` on Linux, `%LOCALAPPDATA%\BAGFD` on Windows).

## Python API

```python
from bagfd import (
    BlueArchiveGameFilesDownloader,
    FileInfo, PackInfo, DownloadResult, TooManyFilesError,
)
# Option enums are not re-exported — import them from bagfd.enums:
from bagfd.enums import Platform, VerifyMethod, FilterMethod

client = BlueArchiveGameFilesDownloader(
    data_dir=None,   # Path | None — catalog DB + zip cache (default: user data dir)
    proxy=None,      # str | None  — e.g. "http://proxy:8080"
)

# Fetch/update catalog (accepts 'all')
client.update(force=False, platform='all')
client.update(platform=Platform.GLOBAL_ANDROID)
```

### `query` — metadata only (no download)

```python
files = client.query('ch0230', platform=Platform.GLOBAL_ANDROID)
files = client.query('*.bundle', platform='global-android', filter_method='glob')

# Non-blocking: query whatever is in the catalog now, refresh in the background
files = client.query('ch0230', platform=Platform.GLOBAL_ANDROID, update_background=True)
```

`update_background=True` kicks a due catalog refresh onto a daemon thread instead
of blocking the call — the query runs against the current (possibly stale, or
empty) catalog. At most one background refresh runs per platform at a time.

`query` returns `list[FileInfo]`. The fields differ by platform:

| Field | Global Android | Japan (Android/Windows) |
|---|---|---|
| `name` | bundle filename | bundle filename |
| `platform` | platform value | platform value |
| `path` | bundle path in the game tree | `None` |
| `url` | direct bundle URL | `None` (see `pack.url`) |
| `hash_type` | usually `"md5"` | `None` (see `pack.hash_type`) |
| `hash_value` | bundle hash | `None` |
| `size` | bundle size (bytes) | `None` (see `pack.size`) |
| `pack` | `None` | the owning `PackInfo` |

`PackInfo` (Japan only): `name` (zip filename), `url`, `hash_type` (usually
`"crc32"`), `hash_value`, `size` (zip bytes), `files` (`list[str]` — every bundle
name inside the zip).

### `get_latest_files` — cache + return paths

Ensures the latest matching files exist in a cache directory and returns their
paths there — downloading what's missing/stale, reusing what's valid. Best for
code that just needs to read the files in place.

```python
paths = client.get_latest_files(
    'ch0230',
    platform=Platform.GLOBAL_ANDROID,
    cache_dir=None,          # Path | None — default: data_dir/download_cache
    verify=VerifyMethod.HASH,      # 'hash' (default) | 'size' | 'none'
    filter_method='auto',
    workers=10,
    show_progress=False,
    max_files=50,            # raise TooManyFilesError above this many (None = unlimited)
)  # -> list[Path]
```

### `download` — deliver files into a directory

Downloads the latest matching files into `output_dir` (Global bundles are
fetched fresh; Japan zips are cached, then matching members are extracted).

```python
result = client.download(
    'ch0230',
    platform=Platform.GLOBAL_ANDROID,
    output_dir='./download',   # default
    with_path=False,           # True = recreate original folder structure
    verify=VerifyMethod.HASH,
    filter_method='auto',
    workers=10,
    show_progress=False,
    max_files=50,
)
print(result.count, result.total_bytes, result.output_dir)
for path in result:           # DownloadResult is iterable
    print(path)
```

`DownloadResult`: `files` (`list[Path]`), `output_dir` (`Path`), `total_bytes`
(`int`), `.count`; also supports `len()` and iteration.

### `clean` — remove caches + catalog rows

Clears the catalog rows and the cached files for a platform. Only the contents
of each `<cache>/<platform>/` folder are removed — the parent cache directory
itself is left untouched, so any unrelated files or other platforms' folders
inside it survive a clean.

```python
client.clean(platform='all')
client.clean(platform=Platform.JAPAN_ANDROID)
```

### Option enums

Defined in `bagfd.enums` (imported explicitly, not from the top-level package).
All are `StrEnum`s, so the enum member and its string value are interchangeable
(`Platform.GLOBAL_ANDROID == "global-android"`) — pass either:

| Enum | Values |
|---|---|
| `Platform` | `GLOBAL_ANDROID` / `JAPAN_ANDROID` / `JAPAN_WINDOWS` (`"global-android"`, …) |
| `VerifyMethod` | `HASH` / `SIZE` / `NONE` (`"hash"`, `"size"`, `"none"`) |
| `FilterMethod` | `AUTO` / `GLOB` / `REGEX` / `CONTAINS` / `STARTS_WITH` / `ENDS_WITH` |

## Storage layout

`bagfd` keeps its state under a fixed **data directory** (`platformdirs.user_data_dir("BAGFD")`
by default, or `$BAGFD_DATA_DIR`, or the `data_dir=` constructor argument):

| Location | Holds |
|---|---|
| `data_dir/catalog.db` | the file catalog |
| `data_dir/zip_cache/<platform>/` | cached Japan zip packs (shared by `download` and `get_latest_files`) |
| `data_dir/download_cache/<platform>/` | default cache for `get_latest_files` |
| `./download` (or your `output_dir`) | files delivered by `download` |

## Acknowledgement

- [Deathemonic/BA-AD](https://github.com/Deathemonic/BA-AD) — the project this one is based on.

## Copyright

Blue Archive is a registered trademark of NAT GAMES Co., Ltd., NEXON Korea Corp., and Yostar, Inc.
This project is not affiliated with, endorsed by, or connected to NAT GAMES Co., Ltd., NEXON Korea
Corp., NEXON GAMES Co., Ltd., IODivision, Yostar, Inc., or any of their subsidiaries or affiliates.
All game assets, content, and materials are copyrighted by their respective owners and are used for
informational and educational purposes only.