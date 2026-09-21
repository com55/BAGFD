"""Tests for target normalization and selection."""

import pytest

from bagfd.enums import Platform, Server
from bagfd.targets import SUPPORTED_TARGETS, normalize_target, resolve_targets, split_target


def test_public_enum_values_keep_legacy_members_and_add_split_values():
    assert Server.GLOBAL.value == "global"
    assert Server.JAPAN.value == "japan"
    assert Platform.ANDROID.value == "android"
    assert Platform.IOS.value == "ios"
    assert Platform.WINDOWS.value == "windows"
    assert Platform.GLOBAL_ANDROID.value == "global-android"
    assert Platform.JAPAN_ANDROID.value == "japan-android"
    assert Platform.JAPAN_WINDOWS.value == "japan-windows"


def test_supported_targets_have_the_publicly_defined_order():
    assert SUPPORTED_TARGETS == (
        "global-android",
        "global-ios",
        "japan-android",
        "japan-ios",
        "japan-windows",
    )


def test_split_target_returns_server_and_bare_platform_for_every_supported_pair():
    expected = (
        ("global-android", (Server.GLOBAL, Platform.ANDROID)),
        ("global-ios", (Server.GLOBAL, Platform.IOS)),
        ("japan-android", (Server.JAPAN, Platform.ANDROID)),
        ("japan-ios", (Server.JAPAN, Platform.IOS)),
        ("japan-windows", (Server.JAPAN, Platform.WINDOWS)),
    )
    for target, parts in expected:
        assert split_target(target) == parts


def test_split_target_rejects_unknown_and_unsupported_keys():
    for target in ("global-windows", "china-android", "ios", None, []):
        with pytest.raises(ValueError):
            split_target(target)


def test_normalize_target_accepts_legacy_composites_and_split_pairs():
    for target in (
        Platform.GLOBAL_ANDROID,
        Platform.JAPAN_ANDROID,
        Platform.JAPAN_WINDOWS,
    ):
        assert normalize_target(target) == target.value

    assert normalize_target(Platform.ANDROID, server=Server.GLOBAL) == "global-android"
    assert normalize_target("ios", server="global") == "global-ios"
    assert normalize_target(Platform.IOS, server=Server.JAPAN) == "japan-ios"
    assert normalize_target("windows", server="japan") == "japan-windows"


def test_normalize_target_rejects_missing_server_composites_with_server_and_bad_inputs():
    invalid = (
        ("android", None),
        ("ios", None),
        ("windows", None),
        ("global-ios", None),
        ("japan-ios", None),
        ("global-android", "global"),
        ("japan-windows", "japan"),
        ("windows", "global"),
        ("unknown", "japan"),
        ([], "global"),
        ("android", 4),
        (4, None),
    )
    for platform, server in invalid:
        with pytest.raises(ValueError):
            normalize_target(platform, server=server)


def test_resolve_targets_defaults_to_all_supported_targets():
    assert resolve_targets() == list(SUPPORTED_TARGETS)
    assert resolve_targets("all") == list(SUPPORTED_TARGETS)


def test_resolve_targets_expands_split_selectors_in_supported_order():
    assert resolve_targets("all", server="global") == [
        "global-android",
        "global-ios",
    ]
    assert resolve_targets("all", server="japan") == [
        "japan-android",
        "japan-ios",
        "japan-windows",
    ]
    assert resolve_targets("ios", server="all") == ["global-ios", "japan-ios"]
    assert resolve_targets("windows", server="all") == ["japan-windows"]
    assert resolve_targets("all", server="all") == list(SUPPORTED_TARGETS)


def test_resolve_targets_preserves_legacy_order_and_deduplicates():
    assert resolve_targets("japan-windows") == ["japan-windows"]
    assert resolve_targets([
        "japan-android",
        "global-android",
        "japan-android",
    ]) == ["japan-android", "global-android"]
    assert resolve_targets(("global-android", "japan-android")) == [
        "global-android",
        "japan-android",
    ]
    assert resolve_targets([]) == []
    assert resolve_targets(()) == []


def test_resolve_targets_validates_the_entire_legacy_selection():
    for selection in (
        ["global-android", "unknown"],
        ("japan-android", "global-ios"),
        ["global-android", "all"],
        {"global-android"},
        "unknown",
    ):
        with pytest.raises(ValueError):
            resolve_targets(selection)


def test_resolve_targets_rejects_mixed_or_unsupported_split_selectors():
    for platform, server in (
        (["android"], "global"),
        ("global-android", "global"),
        ("windows", "global"),
        ("console", "japan"),
        ("android", "china"),
        ("all", 1),
        (None, "global"),
    ):
        with pytest.raises(ValueError):
            resolve_targets(platform, server=server)
