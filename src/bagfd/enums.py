"""Option enumerations for bagfd.

These are kept in a dedicated module and are intentionally **not** re-exported
from the top-level ``bagfd`` package — import them explicitly:

    >>> from bagfd.enums import Platform, Server, VerifyMethod, FilterMethod

All four are `StrEnum`s, so a member and its string value are interchangeable
(``Platform.GLOBAL_ANDROID == "global-android"``). That lets the library accept
the enum while CLI / config code can pass plain strings.
"""
from enum import StrEnum


class Platform(StrEnum):
    """A device platform or a retained legacy composite target.

    New callers pair ANDROID, IOS, or WINDOWS with a :class:`Server`. The
    three composite members remain for compatibility with existing callers.
    """

    ANDROID = "android"
    IOS = "ios"
    WINDOWS = "windows"

    GLOBAL_ANDROID = "global-android"
    JAPAN_ANDROID = "japan-android"
    JAPAN_WINDOWS = "japan-windows"


class Server(StrEnum):
    """A Blue Archive server region for a split server/platform selection."""

    GLOBAL = "global"
    JAPAN = "japan"


class VerifyMethod(StrEnum):
    """How a cached file is checked before being reused."""

    HASH = "hash"   # verify via md5/crc32 against the catalog's expected hash
    SIZE = "size"   # verify via byte size only
    NONE = "none"   # reuse if the file exists, no verification


class FilterMethod(StrEnum):
    """How a pattern is matched against a filename."""

    AUTO = "auto"               # detect from the pattern's characters
    GLOB = "glob"               # fnmatch-style (*, ?, [...])
    REGEX = "regex"             # re.search
    CONTAINS = "contains"       # substring
    STARTS_WITH = "starts_with" # str.startswith
    ENDS_WITH = "ends_with"     # str.endswith
