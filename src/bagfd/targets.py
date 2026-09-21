"""Pure normalization and selection for supported server/platform targets."""

from .enums import Platform, Server


SUPPORTED_TARGETS = (
    "global-android",
    "global-ios",
    "japan-android",
    "japan-ios",
    "japan-windows",
)

_LEGACY_TARGETS = (
    Platform.GLOBAL_ANDROID.value,
    Platform.JAPAN_ANDROID.value,
    Platform.JAPAN_WINDOWS.value,
)
_DEVICE_PLATFORMS = (
    Platform.ANDROID.value,
    Platform.IOS.value,
    Platform.WINDOWS.value,
)
_SERVER_VALUES = (Server.GLOBAL.value, Server.JAPAN.value)


def normalize_target(
    platform: str | Platform,
    *,
    server: str | Server | None = None,
) -> str:
    """Validate one public target and return its composite storage key.

    Without ``server``, only the three legacy composite values are accepted.
    With ``server``, ``platform`` must be a bare device platform.
    """
    if not isinstance(platform, str):
        raise ValueError(f"Invalid platform: {platform!r}")

    if server is None:
        if platform in _LEGACY_TARGETS and platform in SUPPORTED_TARGETS:
            return str(platform)
        raise ValueError(f"A supported server is required for platform: {platform}")

    if not isinstance(server, str) or server not in _SERVER_VALUES:
        raise ValueError(f"Invalid server: {server!r}")
    if platform not in _DEVICE_PLATFORMS:
        raise ValueError(f"Expected a bare device platform, got: {platform}")

    target = f"{server}-{platform}"
    if target not in SUPPORTED_TARGETS:
        raise ValueError(f"Unsupported server/platform pair: {server}/{platform}")
    return target


def resolve_targets(
    platform: str | Platform | list[str] | tuple[str, ...] = "all",
    *,
    server: str | Server | None = None,
) -> list[str]:
    """Expand and validate a bulk selection in supported-target order.

    Legacy composite lists and tuples remain accepted when ``server`` is
    omitted. Split selectors accept ``all`` for either selector and only
    return combinations present in :data:`SUPPORTED_TARGETS`.
    """
    if server is None:
        if isinstance(platform, (list, tuple)):
            normalized = [normalize_target(item) for item in platform]
            return list(dict.fromkeys(normalized))
        if platform == "all":
            return list(SUPPORTED_TARGETS)
        return [normalize_target(platform)]

    if isinstance(platform, (list, tuple)) or not isinstance(platform, str):
        raise ValueError("Split bulk selection requires one platform value")
    if not isinstance(server, str) or server not in (*_SERVER_VALUES, "all"):
        raise ValueError(f"Invalid server selector: {server!r}")
    if platform != "all" and platform not in _DEVICE_PLATFORMS:
        raise ValueError(f"Invalid platform selector: {platform!r}")

    selected = []
    for target in SUPPORTED_TARGETS:
        target_server, target_platform = split_target(target)
        if server != "all" and target_server != server:
            continue
        if platform != "all" and target_platform != platform:
            continue
        selected.append(target)

    if not selected:
        raise ValueError(f"Unsupported server/platform selection: {server}/{platform}")
    return selected


def split_target(target: str) -> tuple[Server, Platform]:
    """Validate an internal composite key and return its separate enums."""
    if not isinstance(target, str) or target not in SUPPORTED_TARGETS:
        raise ValueError(f"Unsupported target: {target!r}")

    server, platform = target.split("-", maxsplit=1)
    return Server(server), Platform(platform)
