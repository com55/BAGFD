"""Blue Archive Game Files Downloader for 3 platforms: Global Android, Japan Android, Japan Windows.

Option enums live in `bagfd.enums` and are imported explicitly:

    >>> from bagfd import BlueArchiveGameFilesDownloader
    >>> from bagfd.enums import Platform, VerifyMethod, FilterMethod
    >>> client = BlueArchiveGameFilesDownloader()
    >>> # search the catalog
    >>> files = client.query('ch0230', platform=Platform.GLOBAL_ANDROID)
    >>> # download into ./out
    >>> result = client.download('ch0230', Platform.GLOBAL_ANDROID, output_dir='./out')
    >>> print(result.count, result.total_bytes)
    >>> # or cache + get local paths (latest version, from cache or fresh)
    >>> paths = client.get_latest_files('ch0230', Platform.GLOBAL_ANDROID)
"""

from .client import BlueArchiveGameFilesDownloader
from .models import DownloadResult, FileInfo, PackInfo, TooManyFilesError

__all__ = [
    'BlueArchiveGameFilesDownloader',
    'DownloadResult',
    'FileInfo',
    'PackInfo',
    'TooManyFilesError',
]
