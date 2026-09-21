"""Blue Archive Game Files Downloader for five server/platform targets.

Supported targets are Global Android/iOS and Japan Android/iOS/Windows. New
calls pair a server with a device platform; the old three composite target
values remain available for calls that omit ``server``.

Option enums live in `bagfd.enums` and are imported explicitly:

    >>> from bagfd import BlueArchiveGameFilesDownloader
    >>> from bagfd.enums import Platform, Server, VerifyMethod, FilterMethod
    >>> client = BlueArchiveGameFilesDownloader()
    >>> # search either iOS catalog
    >>> files = client.query(
    ...     'azusa_swimsuit_spr', server=Server.GLOBAL, platform=Platform.IOS
    ... )
    >>> files = client.query(
    ...     'azusa_swimsuit_spr', server=Server.JAPAN, platform=Platform.IOS
    ... )
    >>> # old composite calls remain supported
    >>> result = client.download('ch0230', Platform.GLOBAL_ANDROID, output_dir='./out')
    >>> print(result.count, result.total_bytes)
    >>> # or cache + get local paths (latest version, from cache or fresh)
    >>> paths = client.get_latest_files(
    ...     'azusa_swimsuit_spr', server='global', platform='ios'
    ... )
"""

from .client import BlueArchiveGameFilesDownloader
from .models import (
    DownloadResult,
    FileInfo,
    PackInfo,
    ResourceUnavailableError,
    TooManyFilesError,
)

__all__ = [
    'BlueArchiveGameFilesDownloader',
    'DownloadResult',
    'FileInfo',
    'PackInfo',
    'ResourceUnavailableError',
    'TooManyFilesError',
]
