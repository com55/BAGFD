"""Public data models returned by the client.

- `FileInfo` / `PackInfo` — results of `query`.
- `DownloadResult` — outcome of `download`.
- `TooManyFilesError` — raised when a pattern matches too many files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .enums import Platform, Server
from .targets import split_target


class TooManyFilesError(Exception):
    """Raised when a pattern matches more files than the allowed limit."""

    def __init__(self, count: int, limit: int):
        self.count = count
        self.limit = limit
        super().__init__(
            f"{count} files match (limit {limit}). "
            f"Narrow the pattern or pass max_files=None to override."
        )


class ResourceUnavailableError(Exception):
    """Raised when matched files can't be downloaded right now.

    The catalog lists the files, but the game server couldn't be reached to
    fetch the bytes. This may be a maintenance window during a version update,
    or the game changing its endpoint / access method. Retry; if it keeps
    failing, the access path may need updating.
    """


@dataclass
class PackInfo:
    """A Japan zip pack — one catalog row for a Japan target.

    Attributes:
        name: The zip filename (the catalog row's ``path``).
        url: Direct download URL of the zip.
        hash_type: Hash algorithm of the zip, usually ``"crc32"``.
        hash_value: Expected zip hash (crc32 as a decimal string).
        size: Zip size in bytes.
        files: Names of every bundle file contained in the zip. Kept as plain
            strings (not `FileInfo`) so the dataclass has no reference cycle.
    """

    name: str
    url: str
    hash_type: str
    hash_value: str
    size: int
    files: list[str] = field(default_factory=list)


@dataclass
class FileInfo:
    """A single matched bundle file.

    For **Global** targets every field is populated from the bundle's own
    catalog row. For **Japan** targets a "file" lives inside a zip pack, so
    the per-file ``path``/``url``/``hash``/``size`` are ``None`` and the
    zip-level information is carried on ``pack`` instead.

    Attributes:
        name: Bundle filename.
        platform: Composite target identifier (e.g. ``global-ios``).
        path: Global bundle path within the game's file tree; Japan — ``None``.
        url: Global direct bundle URL; Japan — ``None`` (see ``pack.url``).
        hash_type: Global usually ``"md5"``; Japan — ``None`` (see ``pack``).
        hash_value: Global bundle hash; Japan — ``None``.
        size: Global bundle size in bytes; Japan — ``None`` (see ``pack.size``).
        pack: Global — ``None``; Japan — the `PackInfo` of the owning zip.
    """

    name: str
    platform: str
    path: str | None
    url: str | None
    hash_type: str | None
    hash_value: str | None
    size: int | None
    pack: PackInfo | None

    @property
    def server(self) -> Server:
        """Server enum derived from the composite ``platform`` target."""
        return split_target(self.platform)[0]

    @property
    def device_platform(self) -> Platform:
        """Device platform enum derived from the composite ``platform`` target."""
        return split_target(self.platform)[1]


@dataclass
class DownloadResult:
    """Outcome of a `download` call.

    Attributes:
        files: Paths of the delivered files under ``output_dir``.
        output_dir: Directory the files were written to.
        total_bytes: Combined size of the delivered files.
    """

    files: list[Path]
    output_dir: Path
    total_bytes: int

    @property
    def count(self) -> int:
        """Number of delivered files."""
        return len(self.files)

    def __len__(self) -> int:
        return len(self.files)

    def __iter__(self):
        return iter(self.files)
