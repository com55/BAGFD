"""
Database operations for Blue Archive game files.
"""

import sqlite3
import logging
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Dict

logger = logging.getLogger(__name__)

VALID_TABLE_NAMES = {"global_android", "japan_android", "japan_windows"}


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection tuned for frequent, concurrent-ish access.

    Enables WAL journaling (better read/write concurrency, fewer "database is
    locked" errors) and a busy timeout so writers wait briefly instead of
    failing immediately. WAL is persisted in the database file, so setting it
    on every connection is a cheap no-op after the first time.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _validate_table_name(table_name: str) -> None:
    """Validate table name to prevent SQL injection.
    
    Args:
        table_name: Table name to validate.
        
    Raises:
        ValueError: If table name is not in the whitelist.
    """
    if table_name not in VALID_TABLE_NAMES:
        raise ValueError(f"Invalid table name: {table_name}")


def init_database(db_path: Path) -> None:
    """Initialize database and tables.
    
    Creates the versions table for storing platform version information,
    and platform-specific tables for storing game files metadata.
    
    Args:
        db_path: Path to the SQLite database file.
    """
    # auto_vacuum is baked into the database header at creation, so it must be
    # set before anything else writes the header (incl. switching to WAL) and
    # before any table exists. Use a raw connection here and set it first.
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA auto_vacuum=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    cursor = conn.cursor()

    # Table: versions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS versions (
            platform TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            last_check TIMESTAMP NOT NULL,
            last_update TIMESTAMP NOT NULL
        )
    """)

    # Create separate tables for each platform. `path` is the primary key — no
    # surrogate id / AUTOINCREMENT, so nothing climbs across catalog refreshes.
    for table_name in ["global_android", "japan_android", "japan_windows"]:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                path TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                hash_type TEXT NOT NULL,
                hash_value TEXT NOT NULL,
                size INTEGER NOT NULL,
                bundle_files TEXT
            )
        """)

    conn.commit()
    conn.close()


def get_table_name(platform: str) -> str:
    """Get table name for platform.
    
    Args:
        platform: Platform identifier ('global_android', 'japan_android', or 'japan_windows').
        
    Returns:
        Database table name for the platform.
    """
    table_map = {
        "global-android": "global_android",
        "japan-android": "japan_android",
        "japan-windows": "japan_windows",
    }
    return table_map.get(platform, "")


def should_check_version(db_path: Path, platform: str, force: bool = False, 
                        check_interval: timedelta = timedelta(hours=4)) -> bool:
    """Check if version check is needed.
    
    Determines whether to fetch a new version based on the last check time
    and the specified check interval.
    
    Args:
        db_path: Path to the SQLite database.
        platform: Platform identifier.
        force: Force version check regardless of interval.
        check_interval: Minimum time between checks.
        
    Returns:
        True if version should be checked, False otherwise.
    """
    if force:
        return True
    
    conn = _connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT last_check FROM versions WHERE platform = ?", (platform,))
    result = cursor.fetchone()
    conn.close()
    
    if not result:
        return True
    
    last_check = datetime.fromisoformat(result[0])
    return datetime.now() - last_check > check_interval


def get_stored_version(db_path: Path, platform: str) -> Optional[str]:
    """Get stored version from database.
    
    Args:
        db_path: Path to the SQLite database.
        platform: Platform identifier.
        
    Returns:
        Stored version string, or None if not found.
    """
    conn = _connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT version FROM versions WHERE platform = ?", (platform,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None


def update_version(db_path: Path, platform: str, version: str, is_new_version: bool = False) -> None:
    """Update version in database.
    
    Args:
        db_path: Path to the SQLite database.
        platform: Platform identifier.
        version: New version string.
        is_new_version: Whether this is a new version (updates last_update timestamp).
    """
    now = datetime.now().isoformat()
    
    conn = _connect(db_path)
    cursor = conn.cursor()
    
    if is_new_version:
        cursor.execute("""
            INSERT OR REPLACE INTO versions (platform, version, last_check, last_update)
            VALUES (?, ?, ?, ?)
        """, (platform, version, now, now))
    else:
        cursor.execute("""
            INSERT OR REPLACE INTO versions (platform, version, last_check, last_update)
            VALUES (?, ?, ?, COALESCE((SELECT last_update FROM versions WHERE platform = ?), ?))
        """, (platform, version, now, platform, now))
    
    conn.commit()
    conn.close()


def clear_cache_for_platform(cache_dir: Path, platform: str) -> None:
    """Clear a platform's cached files, without touching ``cache_dir`` itself.

    Removes everything inside ``cache_dir/<platform>/`` only. ``cache_dir`` is
    left alone since it may hold unrelated files or other platforms' folders.

    Args:
        cache_dir: Base cache directory path.
        platform: Platform identifier.
    """
    platform_cache = cache_dir / platform
    if not platform_cache.exists():
        return
    for entry in platform_cache.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    logger.info(f"Cache cleared: {platform}")


def save_game_files(db_path: Path, table_name: str, files: list) -> None:
    """Save game files to database.
    
    Replaces all existing files in the table with the provided list.
    
    Args:
        db_path: Path to the SQLite database.
        table_name: Target table name.
        files: List of file entries (tuples of (path, url, hash_type, hash_value, size, bundle_files)).
        
    Raises:
        ValueError: If table name is invalid.
    """
    _validate_table_name(table_name)
    conn = _connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM {table_name}")
    
    for path, url, hash_type, hash_value, size, bundle_files in files:
        cursor.execute(f"""
            INSERT INTO {table_name} (path, url, hash_type, hash_value, size, bundle_files)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (path, url, hash_type, hash_value, size, bundle_files))
    
    conn.commit()
    conn.close()


def clear_platform_db(db_path: Path, platform: str) -> None:
    """Clear all DB entries for a platform (table rows + version record).
    
    Raises:
        ValueError: If table name lookup fails.
    """
    table_name = get_table_name(platform)
    if not table_name:
        raise ValueError(f"Unknown platform: {platform}")
    _validate_table_name(table_name)
    conn = _connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"DELETE FROM {table_name}")
    cursor.execute("DELETE FROM versions WHERE platform = ?", (platform,))
    conn.commit()
    conn.close()
    logger.info(f"DB cleared: {platform}")


def get_game_files(db_path: Path, table_name: str) -> list:
    """Get all game files from database.
    
    Args:
        db_path: Path to the SQLite database.
        table_name: Source table name.
        
    Returns:
        List of file entries (tuples of (path, url, hash_type, hash_value, size, bundle_files)).
        
    Raises:
        ValueError: If table name is invalid.
    """
    _validate_table_name(table_name)
    conn = _connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"SELECT path, url, hash_type, hash_value, size, bundle_files FROM {table_name}")
    results = cursor.fetchall()
    conn.close()
    return results
