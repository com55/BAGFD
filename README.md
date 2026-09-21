# BAGFD

Blue Archive Game Files Downloader — inspired by / based on [BA-AD](https://github.com/Deathemonic/BA-AD).

This checkout supports five server/platform targets:

| Server | Device platform | Composite storage target |
|---|---|---|
| Global | Android | `global-android` |
| Global | iOS | `global-ios` |
| Japan | Android | `japan-android` |
| Japan | iOS | `japan-ios` |
| Japan | Windows | `japan-windows` |

Global Windows is unsupported. These iOS and split-selector examples describe this checkout and may not yet be available from the published `main` branch.

## Install

```bash
uv add git+https://github.com/com55/BAGFD@main
```

## CLI

New commands use separate server and device-platform selectors:

```text
bagfd query SERVER PLATFORM PATTERN [flags]
bagfd download SERVER PLATFORM PATTERN [flags]
bagfd update SERVER PLATFORM [flags]
bagfd clean SERVER PLATFORM [flags]
```

`SERVER` is `global`, `japan`, or `all` for bulk commands. `PLATFORM` is `android`, `ios`, `windows`, or `all` for bulk commands. Query and download require a concrete supported pair. `global windows` is unsupported; `all windows` selects Japan Windows.

```bash
# Refresh and search either iOS catalog
bagfd update global ios --force
bagfd query global ios 'azusa_swimsuit_spr'
bagfd query japan ios 'azusa_swimsuit_spr' --format json

# Download matching files; default output directory is ./download
bagfd download global ios 'azusa_swimsuit_spr' -o ./out
bagfd download japan ios 'azusa_swimsuit_spr' --with-path

# Bulk selectors expand over supported targets
bagfd update all all       # all five supported targets
bagfd update all ios       # both iOS targets
bagfd update japan all     # Japan Android, iOS, and Windows
bagfd update all windows   # Japan Windows only
bagfd clean global all     # Global Android and iOS
bagfd clean all all        # all five supported targets
```

Compatibility note: `bagfd update all` and `bagfd clean all` still work and now cover all five supported targets.

The old one-token composite form remains supported for the three original targets. A composite target is accepted only when no server is supplied:

```bash
bagfd update global-android --force
bagfd query global-android 'ch0230'
bagfd query japan-android '^ch\d+' --filter-method regex
bagfd download japan-windows '*.bundle' -o ./out
bagfd clean japan-android
bagfd update all
bagfd clean all
```

For query and download, patterns such as `ios`, `japan`, and `all` remain literal when they follow a legacy target. Prefix a pattern beginning with `-` with `--`, for example `bagfd query global ios -- '-azusa*'`. Options can appear before or after selectors and patterns. Old composite targets passed with an explicit server, bare platforms without a server, and unsupported server/platform pairs are rejected before the client is created. No deprecation warning is emitted for legacy calls.

For downloads, Global bundle files are fetched fresh and directly; Japan ZIP packs are cached and matching members are extracted. `--with-path` recreates each file's original directory structure under the output directory. If more than 50 files match, the CLI asks for confirmation; pass `-y`/`--yes` to skip the prompt. `--verify` controls cached-file reuse: `hash` (default; md5/crc32), `size`, or `none` (reuse if present).

**`query --format`**: `table` (default, human-readable with a count summary; Global shows `<size>  <name>`, Japan shows `<pack-size>  <pack>  <name>` grouped by pack), `name` (filenames), `url` (direct URLs for Global, deduplicated pack URLs for Japan), `path` (game-tree path for Global; bundle name for Japan), or `json` (structured records). Non-table formats emit only data.

**`query --color`** highlights matched filename text in `table` and `name` formats: `auto` (default; only on a terminal), `always`, or `never`. It also honours `NO_COLOR`. For glob patterns only literal parts are highlighted (`*ch0171*` highlights `ch0171`).

**Piping:** command results go to stdout; progress logs go to stderr. Highlight colour auto-disables when stdout is not a terminal. Use `-q`/`--quiet` (any command) to silence progress logs.

**`--filter-method`**: `auto` (default), `glob`, `regex`, `contains`, `starts_with`, or `ends_with`. Auto-detection treats `*`, `?`, and `[` as glob syntax; `^`, `$`, `\`, `+`, `|`, and `(` as regex syntax; other patterns as substring matches.

**`--data-dir`** priority: flag > `BAGFD_DATA_DIR` environment variable > `platformdirs.user_data_dir("BAGFD")` (`XDG_DATA_HOME/BAGFD` on Linux, `%LOCALAPPDATA%\BAGFD` on Windows).

## Python API

```python
from bagfd import BlueArchiveGameFilesDownloader, FileInfo, PackInfo, DownloadResult
from bagfd.enums import FilterMethod, Platform, Server, VerifyMethod

client = BlueArchiveGameFilesDownloader(data_dir=None, proxy=None)

# Split form: use keyword arguments for the pair
files = client.query(
    'azusa_swimsuit_spr', server=Server.GLOBAL, platform=Platform.IOS
)
japan_files = client.query(
    'azusa_swimsuit_spr', server='japan', platform='ios'
)

# Legacy composite form remains valid for the original three targets
old_files = client.query('ch0230', platform=Platform.GLOBAL_ANDROID)
```

Enums live in `bagfd.enums`; they are not re-exported from `bagfd`. `Server` values are `GLOBAL` and `JAPAN`. `Platform` includes the bare device values `ANDROID`, `IOS`, and `WINDOWS`, plus the retained legacy composites `GLOBAL_ANDROID`, `JAPAN_ANDROID`, and `JAPAN_WINDOWS`.

Single-target methods (`query`, `get_latest_files`, and `download`) require a concrete target. A bare device platform requires a server. When `server` is supplied, `platform` must be a bare device value; do not pass a composite target alongside it.

### `query` — metadata only

```python
files = client.query(
    'azusa_swimsuit_spr', server=Server.GLOBAL, platform=Platform.IOS
)
files = client.query('ch0230', platform=Platform.GLOBAL_ANDROID)

# Non-blocking refresh: query the catalog while a due update runs in the background
files = client.query(
    'azusa_swimsuit_spr', server=Server.JAPAN, platform=Platform.IOS,
    update_background=True,
)
```

`query` returns `list[FileInfo]`. The `platform` field retains the composite storage target (`global-ios`, for example) for both legacy and split calls. The computed `server` and `device_platform` properties return `Server` and bare `Platform` enum values. They are properties, not dataclass fields, so they do not appear in `dataclasses.asdict()` or CLI query JSON. The existing query JSON schema continues to use the composite `platform` field.

| Field | Global Android/iOS | Japan Android/iOS/Windows |
|---|---|---|
| `name` | bundle filename | bundle filename |
| `platform` | composite target | composite target |
| `server` | computed `Server.GLOBAL` | computed `Server.JAPAN` |
| `device_platform` | computed `Platform.ANDROID` or `Platform.IOS` | computed `Platform.ANDROID`, `Platform.IOS`, or `Platform.WINDOWS` |
| `path` | bundle path in the game tree | `None` |
| `url`, `hash_type`, `hash_value`, `size` | direct bundle URL and metadata | `None` (see `pack`) |
| `pack` | `None` | owning `PackInfo` |

Global files, including iOS, are downloaded directly and carry per-file metadata. Japan files, including iOS, are extracted from ZIP packs; the pack URL, CRC32, and size are held by `PackInfo`.

### `get_latest_files` — cache and return paths

Ensures that matching files exist in a cache directory, downloading missing or stale files and returning their paths. Global targets use direct bundle downloads. Japan targets cache ZIP packs and extract the matching members.

```python
paths = client.get_latest_files(
    'azusa_swimsuit_spr', server=Server.JAPAN, platform=Platform.IOS,
    cache_dir=None,             # default: data_dir/download_cache
    verify=VerifyMethod.HASH,   # hash (default), size, or none
    filter_method=FilterMethod.AUTO,
    workers=10,
    show_progress=False,
    max_files=50,               # None disables the count guard
)
```

### `download` — deliver files into a directory

Downloads matching files into `output_dir` and returns a `DownloadResult`. A match count above `max_files` (default 50) raises `TooManyFilesError`; pass `None` to remove that guard.

```python
result = client.download(
    'azusa_swimsuit_spr', server=Server.GLOBAL, platform=Platform.IOS,
    output_dir='./download', with_path=False,
    verify=VerifyMethod.HASH, filter_method=FilterMethod.AUTO,
    workers=10, show_progress=False, max_files=50,
)
print(result.count, result.total_bytes, result.output_dir)
for path in result:
    print(path)
```

`DownloadResult` contains `files`, `output_dir`, and `total_bytes`; it supports `.count`, `len()`, and iteration.

### `update` and `clean` — bulk selectors and compatibility

`update()` and `clean()` with no arguments operate on all five supported targets. This is broader than older releases, where `all` covered three targets. The same five-target expansion applies to `platform='all'` without a server.

```python
client.update(server=Server.GLOBAL, platform=Platform.IOS)
client.update(server='all', platform='ios')
client.update(server='all', platform='windows')  # Japan Windows only
client.clean(server=Server.JAPAN, platform=Platform.IOS)
client.update()  # all five targets
client.clean()   # all five targets

# Legacy composites and lists/tuples remain supported without a server
client.update(platform=Platform.GLOBAL_ANDROID)
client.update(platform=['global-android', 'japan-android'])
client.clean(platform=('japan-android', 'japan-windows'))
```

Without `server`, bulk methods accept `all`, one legacy composite, or a list/tuple of legacy composites. Lists and tuples preserve first-seen order and remove duplicates; an empty list or tuple is a no-op. They cannot be combined with `server`, and `all` cannot appear inside a list or tuple. With `server`, use a single bare platform value. `server='all'` or `platform='all'` expands the supported matrix in its documented order. An unsupported selection such as Global Windows raises `ValueError` before catalog or cache actions.

### Option enums

All enums are `StrEnum`s, so each member compares equal to its string value. Import them from `bagfd.enums`.

| Enum | Values |
|---|---|
| `Server` | `GLOBAL` / `JAPAN` (`"global"`, `"japan"`) |
| `Platform` | `ANDROID` / `IOS` / `WINDOWS` and the three legacy composites |
| `VerifyMethod` | `HASH` / `SIZE` / `NONE` (`"hash"`, `"size"`, `"none"`) |
| `FilterMethod` | `AUTO` / `GLOB` / `REGEX` / `CONTAINS` / `STARTS_WITH` / `ENDS_WITH` |

## Storage layout

The data directory defaults to `platformdirs.user_data_dir("BAGFD")`, or can be set through `BAGFD_DATA_DIR` or the constructor's `data_dir` argument.

| Location | Holds |
|---|---|
| `data_dir/catalog.db` | file catalogs and version records, keyed by composite target |
| `data_dir/zip_cache/<target>/` | cached Japan ZIP packs, including Japan iOS |
| `data_dir/download_cache/<target>/` | files returned by `get_latest_files`, partitioned by composite target |
| `./download` (or `output_dir`) | files delivered by `download` |

The existing composite identities and directories remain in place. iOS uses `global-ios` and `japan-ios`; no storage rename or copy is needed.

Catalog table names use the composite target with hyphens replaced by underscores (`global_ios`, `japan_ios`). The three existing table names remain unchanged.

## Acknowledgement

- [Deathemonic/BA-AD](https://github.com/Deathemonic/BA-AD) — the project this one is based on.

## Copyright

Blue Archive is a registered trademark of NAT GAMES Co., Ltd., NEXON Korea Corp., and Yostar, Inc.
This project is not affiliated with, endorsed by, or connected to NAT GAMES Co., Ltd., NEXON Korea
Corp., NEXON GAMES Co., Ltd., IODivision, Yostar, Inc., or any of their subsidiaries or affiliates.
All game assets, content, and materials are copyrighted by their respective owners and are used for
informational and educational purposes only.
