"""Concurrent file downloading with cache reuse.

`download_files` fetches a batch of `DownloadItem`s in parallel. Before fetching
each item it decides whether an existing local file can be reused, governed by
`VerifyMethod` (defined in `bagfd.enums`):

- ``VerifyMethod.HASH`` (default): reuse only if the file's hash matches the
  expected one (md5 or crc32, per ``hash_type``). Most correct, reads the file.
- ``VerifyMethod.SIZE``: reuse if the byte size matches. Cheap.
- ``VerifyMethod.NONE``: reuse if the file simply exists. Cheapest.

`VerifyMethod` is a `StrEnum`, so plain strings ("hash", "size", "none") work.
"""
import hashlib
import logging
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

import requests
from filelock import FileLock, Timeout
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .enums import VerifyMethod

logger = logging.getLogger(__name__)


class PathLockManager:
    """Serialize access to a cache file across both threads and processes.

    The same cache file can be touched concurrently by several download threads
    of one client *and* by separate client processes that share a cache
    directory. Opening it twice at once corrupts a partial download or, on
    Windows, fails outright (``WinError 32``). For each destination path this
    layers a process-local ``threading.Lock`` (mutual exclusion between threads)
    over a cross-process ``filelock.FileLock`` (mutual exclusion between
    processes). The ``threading.Lock`` is required because ``FileLock`` is
    thread-local by default and would not reliably exclude sibling threads on
    its own.

    Lock files live in a central ``lock_dir`` keyed by a hash of the resolved
    path, so they never litter the cache or the user's output directory.
    """

    def __init__(self, lock_dir: Path, timeout: float = 300):
        self.lock_dir = Path(lock_dir)
        self.timeout = timeout
        self._guard = threading.Lock()
        self._thread_locks: dict[str, threading.Lock] = {}

    def _key(self, path: Path) -> str:
        return hashlib.sha1(str(Path(path).resolve()).encode()).hexdigest()

    @contextmanager
    def lock(self, path: Path):
        """Hold the combined thread+process lock for ``path`` for the block."""
        key = self._key(path)
        with self._guard:
            thread_lock = self._thread_locks.setdefault(key, threading.Lock())
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        file_lock = FileLock(str(self.lock_dir / f"{key}.lock"), timeout=self.timeout)
        with thread_lock, file_lock:
            yield

    @contextmanager
    def try_lock(self, path: Path):
        """Non-blocking acquire; yield True if the lock is held, else False.

        Lets cache invalidation delete a file only when no downloader or
        extractor currently holds it — so a stale file that is still in use is
        skipped (and cleaned later) instead of being removed mid-use, which on
        Windows raises ``WinError 32`` and on Linux strands the open reader.
        """
        key = self._key(path)
        with self._guard:
            thread_lock = self._thread_locks.setdefault(key, threading.Lock())
        if not thread_lock.acquire(blocking=False):
            yield False
            return
        try:
            self.lock_dir.mkdir(parents=True, exist_ok=True)
            file_lock = FileLock(str(self.lock_dir / f"{key}.lock"), timeout=0)
            try:
                file_lock.acquire()
            except Timeout:
                yield False
                return
            try:
                yield True
            finally:
                file_lock.release()
        finally:
            thread_lock.release()


@dataclass
class DownloadItem:
    """A single file to download.

    Args:
        url: Source URL.
        dest: Local destination path.
        size: Expected size in bytes (used by ``VerifyMethod.SIZE`` / hash fallback).
        hash_type: ``"md5"`` or ``"crc32"`` (used by ``VerifyMethod.HASH``).
        hash_value: Expected hash — md5 hex string, or crc32 as a decimal string.
    """

    url: str
    dest: Path
    size: int | None = None
    hash_type: str | None = None
    hash_value: str | None = None


def _hash_matches(path: Path, hash_type: str, hash_value: str) -> bool:
    """Return True if ``path``'s hash equals ``hash_value`` for ``hash_type``.

    Supports md5 (compared as a hex string, case-insensitive) and crc32
    (compared as a decimal string; accepts both unsigned and signed-32 forms
    since catalogs vary). Unknown hash types return False.
    """
    ht = hash_type.lower()
    if ht == "md5":
        h = hashlib.md5()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest().lower() == str(hash_value).strip().lower()
    if ht == "crc32":
        crc = 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                crc = zlib.crc32(chunk, crc)
        crc &= 0xFFFFFFFF
        expected = str(hash_value).strip()
        return str(crc) == expected or str(crc - 2**32) == expected
    return False


def _is_valid_cache(item: DownloadItem, verify: VerifyMethod) -> bool:
    """Decide whether the existing file at ``item.dest`` can be reused."""
    if not item.dest.exists():
        return False
    if verify == VerifyMethod.NONE:
        return True
    if verify == VerifyMethod.SIZE:
        return item.size is not None and item.dest.stat().st_size == item.size
    # VerifyMethod.HASH — fall back to a size check if no hash is available.
    if item.hash_type and item.hash_value is not None:
        return _hash_matches(item.dest, item.hash_type, item.hash_value)
    return item.size is not None and item.dest.stat().st_size == item.size


def _download_one(
    item: DownloadItem,
    session: requests.Session,
    verify: VerifyMethod = VerifyMethod.HASH,
    force: bool = False,
    locks: PathLockManager | None = None,
) -> Path:
    """Download a single item, reusing a valid cached file unless ``force``.

    A cached file that fails verification is deleted and re-downloaded. When
    ``locks`` is given, the whole reuse-or-download decision runs under that
    path's lock so a concurrent thread or process can't read or overwrite the
    file mid-write; once the lock is held the cache is re-checked, so a waiter
    reuses the file the prior holder just finished instead of re-downloading.
    """
    cm = locks.lock(item.dest) if locks is not None else nullcontext()
    with cm:
        if not force and _is_valid_cache(item, verify):
            logger.debug("Cache hit: %s", item.dest.name)
            return item.dest
        if item.dest.exists():
            item.dest.unlink()
        item.dest.parent.mkdir(parents=True, exist_ok=True)
        response = session.get(item.url, stream=True)
        response.raise_for_status()
        with open(item.dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                f.write(chunk)
        logger.debug("Downloaded: %s", item.dest.name)
        return item.dest


def download_files(
    items: list[DownloadItem],
    session: requests.Session,
    workers: int = 10,
    show_progress: bool = False,
    verify: VerifyMethod = VerifyMethod.HASH,
    force: bool = False,
    locks: PathLockManager | None = None,
) -> list[Path]:
    """Download ``items`` concurrently and return the local paths that succeeded.

    Args:
        items: Files to download.
        session: Requests session (gets a retrying HTTP adapter mounted).
        workers: Number of parallel download threads.
        show_progress: Show a tqdm progress bar if tqdm is installed.
        verify: Cache-reuse strategy (see `VerifyMethod`).
        force: Always re-download, ignoring any cached file.
        locks: Optional `PathLockManager` to serialize concurrent access to each
            destination across threads and processes sharing the cache.

    Failed downloads are logged and omitted from the returned list.
    """
    if not items:
        return []

    retry = Retry(total=3, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry, pool_connections=workers, pool_maxsize=workers)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    try:
        from tqdm import tqdm
        has_tqdm = True
    except ImportError:
        has_tqdm = False

    results: list[Path] = []

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_dest = {
            executor.submit(_download_one, item, session, verify, force, locks): item.dest
            for item in items
        }
        futures_iter = (
            tqdm(as_completed(future_to_dest), total=len(items), desc="Downloading", unit="file")
            if show_progress and has_tqdm
            else as_completed(future_to_dest)
        )
        for future in futures_iter:
            try:
                results.append(future.result())
            except Exception as e:
                dest = future_to_dest[future]
                logger.warning("Failed %s: %s", dest.name, e)

    return results
