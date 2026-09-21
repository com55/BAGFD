"""
Platform-specific fetching logic for Blue Archive game files.
"""

import logging
import json
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Tuple
from io import BytesIO
from urllib.parse import urljoin
import requests

from .crypto import extract_json_from_string, create_key, encrypt_string, decrypt_string
from .database import (
    should_check_version, get_stored_version, update_version,
    get_table_name, save_game_files, set_defer, get_game_files,
    get_cached_japan_api_url, set_cached_japan_api_url,
)
from .models import ResourceUnavailableError
from .targets import split_target

logger = logging.getLogger(__name__)


def _row_key(row: tuple) -> tuple:
    """Normalize a catalog row for equality comparison (case-insensitive hash).

    `_hash_matches` (used elsewhere for cache verification) already treats
    hash strings as case-insensitive; the catalog-diff comparison here does
    the same, so a hash-casing-only change from the upstream API isn't
    mistaken for real content drift.
    """
    path, url, hash_type, hash_value, size, bundle_files = row
    return (path, url, hash_type, str(hash_value).lower(), size, bundle_files)


def _decrypt_japan_config(encrypted_data: bytes) -> str:
    """Decrypt Japan game config.
    
    Decrypts the encrypted game configuration data from Japan server,
    extracting the server API URL.
    
    Args:
        encrypted_data: Raw encrypted configuration bytes.
        
    Returns:
        Decrypted server API URL.
        
    Raises:
        ValueError: If decryption fails or required key not found.
    """
    import base64
    
    encoded_data = base64.b64encode(encrypted_data).decode('ascii')
    game_config_key = create_key("GameMainConfig")
    server_data_key = create_key("ServerInfoDataUrl")
    decrypted_data = decrypt_string(encoded_data, game_config_key)
    
    try:
        loaded_data = extract_json_from_string(decrypted_data)
    except Exception:
        last_brace = decrypted_data.rfind('}')
        if last_brace > 0:
            json_str = decrypted_data[:last_brace + 1]
            loaded_data = json.loads(json_str)
        else:
            raise
    
    if not isinstance(loaded_data, dict):
        raise ValueError("Decrypted config is not a JSON object")
    
    encrypted_key = encrypt_string("ServerInfoDataUrl", server_data_key)
    encrypted_value = loaded_data.get(encrypted_key)
    if not encrypted_value:
        raise ValueError("Key 'ServerInfoDataUrl' not found in decrypted config")
    
    result = decrypt_string(encrypted_value, server_data_key)
    if not result or not result.strip():
        raise ValueError("Decrypted ServerInfoDataUrl is empty")
    
    return result


def _extract_japan_api_url(session: requests.Session, xapk_data: BytesIO) -> str:
    """Extract API URL from Japan XAPK.
    
    Extracts the game configuration from the XAPK file and decrypts it
    to get the server API URL.
    
    Args:
        session: Requests session for HTTP operations.
        xapk_data: BytesIO containing XAPK file data.
        
    Returns:
        Decrypted server API URL.
        
    Raises:
        ValueError: If game config not found in APK.
    """
    config_pattern = bytes([0x47, 0x61, 0x6D, 0x65, 0x4D, 0x61, 0x69, 0x6E, 0x43, 0x6F, 0x6E, 0x66, 0x69, 0x67,
                           0x00, 0x00, 0x92, 0x03, 0x00, 0x00])
    
    with zipfile.ZipFile(xapk_data, 'r') as xapk:
        unity_apk_data = xapk.read('UnityDataAssetPack.apk')
        
        with zipfile.ZipFile(BytesIO(unity_apk_data), 'r') as unity_apk:
            for filename in unity_apk.namelist():
                if 'assets/bin/Data/' in filename and not filename.endswith('/'):
                    data = unity_apk.read(filename)
                    offset = data.find(config_pattern)
                    
                    if offset != -1:
                        data_start = offset + len(config_pattern)
                        encrypted_data = data[data_start:data_start + 1024]
                        return _decrypt_japan_config(encrypted_data)
    
    raise ValueError("Could not find game config")


_GLOBAL_API_URL = "https://api-pub.nexon.com/patch/v1.1/version-check"
_GLOBAL_ANDROID_VERSION_URL = "https://apptopia.com/google-play/app/com.nexon.bluearchive/about"
_GLOBAL_IOS_VERSION_URL = "https://itunes.apple.com/lookup?id=1571873795&country=us"
_GLOBAL_IOS_TRACK_ID = 1571873795
_GLOBAL_IOS_BUNDLE_ID = "com.nexon.bluearchive"
_GLOBAL_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def _discover_global_version(session: requests.Session, target: str) -> str:
    """Discover and validate the store version for one Global target."""
    _server, device = split_target(target)
    if device.value == "android":
        response = session.get(_GLOBAL_ANDROID_VERSION_URL)
        response.raise_for_status()
        match = _GLOBAL_VERSION_RE.search(response.text)
        if not match:
            raise ValueError("Could not extract version from Apptopia")
        return match.group(0)

    response = session.get(_GLOBAL_IOS_VERSION_URL)
    response.raise_for_status()
    lookup = response.json()
    if not isinstance(lookup, dict) or not isinstance(lookup.get("results"), list):
        raise ValueError("Malformed Apple lookup response")

    result = next((
        item for item in lookup["results"]
        if isinstance(item, dict)
        and type(item.get("trackId")) is int
        and item["trackId"] == _GLOBAL_IOS_TRACK_ID
    ), None)
    if result is None:
        raise ValueError("Apple lookup did not contain the Blue Archive trackId")
    if "bundleId" in result and result["bundleId"] != _GLOBAL_IOS_BUNDLE_ID:
        raise ValueError("Apple lookup bundleId did not match Blue Archive")

    version = result.get("version")
    if not isinstance(version, str) or not _GLOBAL_VERSION_RE.fullmatch(version):
        raise ValueError("Apple lookup did not contain a numeric three-part version")
    return version


def _fetch_global_catalog(
    session: requests.Session,
    target: str,
    version: str,
) -> list[tuple]:
    """Fetch and validate one Global target's direct-file catalog."""
    _server, device = split_target(target)
    is_ios = device.value == "ios"
    payload = {
        "market_game_id": "1571873795" if is_ios else "com.nexon.bluearchive",
        "market_code": "appstore" if is_ios else "playstore",
        "curr_build_version": version,
        "curr_build_number": version.split(".")[-1],
    }

    addressable_resp = session.post(_GLOBAL_API_URL, json=payload)
    addressable_resp.raise_for_status()
    addressable = addressable_resp.json()
    if not isinstance(addressable, dict):
        raise ValueError("Malformed Global version-check response")
    patch = addressable.get("patch")
    if not isinstance(patch, dict):
        raise ValueError("Global version-check response is missing patch")
    manifest_url = patch.get("resource_path")
    if not isinstance(manifest_url, str) or not manifest_url:
        raise ValueError("Global version-check response is missing resource_path")

    resources_resp = session.get(manifest_url)
    resources_resp.raise_for_status()
    manifest = resources_resp.json()
    if not isinstance(manifest, dict) or not isinstance(manifest.get("resources"), list):
        raise ValueError("Malformed Global resource manifest")

    asset_segment = "iOS" if is_ios else "Android"
    catalog_url = urljoin(manifest_url, ".")
    files_to_save = []
    for resource in manifest["resources"]:
        if not isinstance(resource, dict):
            raise ValueError("Malformed Global resource record")
        resource_path = resource.get("resource_path")
        if not isinstance(resource_path, str) or not resource_path:
            raise ValueError("Global resource record is missing resource_path")
        if asset_segment not in resource_path.split("/"):
            continue

        size = resource.get("resource_size")
        hash_value = resource.get("resource_hash")
        if type(size) is not int or size < 0:
            raise ValueError("Global resource has an invalid resource_size")
        if not isinstance(hash_value, str) or not hash_value:
            raise ValueError("Global resource has an invalid resource_hash")

        files_to_save.append((
            resource_path,
            urljoin(catalog_url, resource_path.lstrip("/")),
            "md5",
            hash_value,
            size,
            None,
        ))

    if not files_to_save:
        raise ValueError(f"Global {asset_segment} catalog is empty")
    return files_to_save


def fetch_global(session: requests.Session, db_path: Path,
                 force: bool = False, check_interval=None,
                 defer_on_failure: timedelta = timedelta(minutes=10),
                 *, target: str = "global-android") -> bool:
    """Fetch a validated Global Android or iOS catalog.

    A due check always fetches the selected catalog so same-version hotfixes
    are visible. Catalog failures keep cached rows and defer only that target;
    a target with no stored version raises :class:`ResourceUnavailableError`.
    """
    _server, _device = split_target(target)
    if target not in ("global-android", "global-ios"):
        raise ValueError(f"Unsupported Global target: {target!r}")
    if check_interval is None:
        check_interval = timedelta(hours=4)

    if not should_check_version(db_path, target, force, check_interval):
        logger.debug("Skipped %s (checked recently)", target)
        return False

    stored_version = get_stored_version(db_path, target)
    logger.info("Fetching %s...", target)
    try:
        version = _discover_global_version(session, target)
        files_to_save = _fetch_global_catalog(session, target, version)
    except (requests.RequestException, ValueError, KeyError) as exc:
        if stored_version is None:
            raise ResourceUnavailableError(
                f"Could not fetch the first {target} catalog: {exc}"
            ) from exc

        until = datetime.now() + defer_on_failure
        logger.warning(
            "Global catalog fetch failed (%s); keeping cached catalog and "
            "deferring re-check until %s", exc, until.isoformat(timespec="seconds"),
        )
        set_defer(db_path, target, until)
        return False

    is_new_version = version != stored_version
    table_name = get_table_name(target)
    existing_files = get_game_files(db_path, table_name)
    catalog_changed = {_row_key(row) for row in existing_files} != {
        _row_key(row) for row in files_to_save
    }
    if catalog_changed:
        save_game_files(db_path, table_name, files_to_save)
        logger.info("Updated %s", target)
    else:
        logger.info("%s catalog unchanged", target)

    update_version(db_path, target, version, is_new_version)
    return is_new_version or catalog_changed


def fetch_global_android(session: requests.Session, db_path: Path,
                        force: bool = False, check_interval=None,
                        defer_on_failure: timedelta = timedelta(minutes=10)) -> bool:
    """Compatibility wrapper for the original Global Android fetcher."""
    return fetch_global(
        session, db_path, force, check_interval, defer_on_failure,
        target="global-android",
    )


_DEFAULT_JAPAN_TARGETS = ("japan-android", "japan-windows")
_JAPAN_PATCH_PACKS = {
    "japan-android": "Android_PatchPack",
    "japan-ios": "iOS_PatchPack",
    "japan-windows": "Windows_PatchPack",
}
_CATALOG_FETCH_ERRORS = (
    requests.RequestException, ValueError, KeyError, zipfile.BadZipFile,
)
_YOSTAR_FETCH_ERRORS = (
    *_CATALOG_FETCH_ERRORS, TypeError, AttributeError, IndexError,
)


def _select_japan_targets(requested_targets) -> list[str]:
    if requested_targets is None:
        return list(_DEFAULT_JAPAN_TARGETS)
    if not isinstance(requested_targets, (list, tuple)):
        raise ValueError("requested_targets must be a list or tuple of Japan targets")

    selected = []
    for target in requested_targets:
        try:
            server, _device = split_target(target)
        except ValueError as exc:
            raise ValueError(f"Invalid Japan target: {target!r}") from exc
        if server.value != "japan" or target not in _JAPAN_PATCH_PACKS:
            raise ValueError(f"Invalid Japan target: {target!r}")
        if target not in selected:
            selected.append(target)
    return selected


def _japan_catalog_root(addressable) -> str:
    if not isinstance(addressable, dict):
        raise ValueError("Malformed Japan addressables response")
    connection_groups = addressable.get("ConnectionGroups")
    if not isinstance(connection_groups, list) or not connection_groups:
        raise ValueError("No ConnectionGroups in addressable response")
    first_group = connection_groups[0]
    if not isinstance(first_group, dict):
        raise ValueError("Malformed first ConnectionGroup")
    override_groups = first_group.get("OverrideConnectionGroups")
    if not isinstance(override_groups, list) or len(override_groups) < 2:
        raise ValueError("Expected at least 2 OverrideConnectionGroups")
    catalog_group = override_groups[1]
    if not isinstance(catalog_group, dict):
        raise ValueError("Malformed OverrideConnectionGroups[1]")
    catalog_root = catalog_group.get("AddressablesCatalogUrlRoot")
    if not isinstance(catalog_root, str) or not catalog_root:
        raise ValueError("AddressablesCatalogUrlRoot not found or empty")
    return catalog_root.rstrip("/")


def _japan_pack_rows(bundle_data, catalog_root: str, patch_pack: str) -> list[tuple]:
    if not isinstance(bundle_data, dict):
        raise ValueError("Malformed Japan bundle catalog")
    full_packs = bundle_data.get("FullPatchPacks")
    update_packs = bundle_data.get("UpdatePacks")
    if not isinstance(full_packs, list) or not isinstance(update_packs, list):
        raise ValueError("Japan FullPatchPacks and UpdatePacks must be lists")

    all_packs = full_packs + update_packs
    if not all_packs:
        raise ValueError("Japan bundle catalog contains no packs")

    files_to_save = []
    for pack in all_packs:
        if not isinstance(pack, dict):
            raise ValueError("Malformed Japan pack record")
        pack_name = pack.get("PackName")
        crc = pack.get("Crc")
        pack_size = pack.get("PackSize")
        if not isinstance(pack_name, str) or not pack_name:
            raise ValueError("Japan pack is missing PackName")
        if type(crc) is int:
            crc_text = str(crc)
        elif isinstance(crc, str) and re.fullmatch(r"[+-]?[0-9]+", crc):
            crc_text = crc
        else:
            raise ValueError(f"Invalid Japan pack Crc for {pack_name}")
        if type(pack_size) is not int or pack_size < 0:
            raise ValueError(f"Invalid Japan PackSize for {pack_name}")

        members = pack.get("BundleFiles")
        if not isinstance(members, list):
            raise ValueError(f"Invalid BundleFiles for {pack_name}")
        member_names = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError(f"Invalid BundleFiles member in {pack_name}")
            member_name = member.get("Name")
            if not isinstance(member_name, str) or not member_name:
                raise ValueError(f"BundleFiles member has invalid Name in {pack_name}")
            member_names.append(member_name)

        files_to_save.append((
            pack_name,
            f"{catalog_root}/{patch_pack}/{pack_name}",
            "crc32",
            crc_text,
            pack_size,
            json.dumps(sorted(member_names)),
        ))
    return files_to_save


def _defer_japan_failures(
    db_path: Path,
    targets: list[str],
    stored_versions: dict[str, str | None],
    defer_on_failure: timedelta,
) -> list[str]:
    until = datetime.now() + defer_on_failure
    uncached = []
    for target in targets:
        if stored_versions[target] is None:
            uncached.append(target)
            continue
        set_defer(db_path, target, until)
    return uncached


def fetch_japan_servers(session: requests.Session, db_path: Path,
                       force: bool = False, check_interval=None,
                       defer_on_failure: timedelta = timedelta(minutes=10),
                       *, requested_targets=None) -> Dict[str, bool]:
    """Fetch due requested Japan catalogs; defaults to Android and Windows.

    Each target's complete pack catalog is validated before its rows or version
    are updated. Cached failures are deferred independently; first-fetch
    failures raise :class:`ResourceUnavailableError` after other targets finish.
    """
    targets = _select_japan_targets(requested_targets)
    if not targets:
        return {}

    from .yostar import get_yostar_base_config, resolve_japan_server_info_url

    if check_interval is None:
        check_interval = timedelta(hours=4)

    results = {target: False for target in targets}
    due_targets = []
    for target in targets:
        if should_check_version(db_path, target, force, check_interval):
            logger.info("Checking %s...", target)
            due_targets.append(target)
        else:
            logger.debug("Skipped %s (checked recently)", target)
    if not due_targets:
        return results

    stored_versions = {
        target: get_stored_version(db_path, target)
        for target in due_targets
    }
    cached = get_cached_japan_api_url(db_path)
    cache_update = None
    bootstrap_error = None
    current_version = None
    resolved_api_url = None

    try:
        need_fresh_api = force or not cached
        if need_fresh_api:
            logger.info(
                "Resolving Japan server info "
                "(YoStar resources.assets, XAPK fallback)..."
            )
            current_version, resolved_api_url = resolve_japan_server_info_url(session)
            if not isinstance(current_version, str) or not current_version:
                raise ValueError("Japan server-info version is invalid")
            if not isinstance(resolved_api_url, str) or not resolved_api_url:
                raise ValueError("Japan server-info URL is invalid")
            cache_update = (current_version, resolved_api_url)
        else:
            try:
                base_config = get_yostar_base_config(session)
                if not isinstance(base_config, dict):
                    raise ValueError("Malformed YoStar base config")
                discovered_version = base_config.get("game_latest_version")
                if not isinstance(discovered_version, str) or not discovered_version:
                    raise ValueError("YoStar base config has an invalid version")
                current_version = discovered_version
            except _YOSTAR_FETCH_ERRORS as exc:
                logger.warning(
                    "YoStar version check failed (%s); using cached version %s",
                    exc, cached[0],
                )
                current_version = cached[0]

            if cached[0] == current_version:
                resolved_api_url = cached[1]
                logger.info("Reusing cached API URL for version %s", current_version)
            else:
                logger.info(
                    "Version changed %s → %s; refreshing server info...",
                    cached[0], current_version,
                )
                current_version, resolved_api_url = resolve_japan_server_info_url(session)
                if not isinstance(current_version, str) or not current_version:
                    raise ValueError("Japan server-info version is invalid")
                if not isinstance(resolved_api_url, str) or not resolved_api_url:
                    raise ValueError("Japan server-info URL is invalid")
                cache_update = (current_version, resolved_api_url)

        if not isinstance(current_version, str) or not current_version:
            raise ValueError("Japan version is invalid")
        if not isinstance(resolved_api_url, str) or not resolved_api_url:
            raise ValueError("Japan server-info URL is invalid")

    except _YOSTAR_FETCH_ERRORS as exc:
        bootstrap_error = exc

    if cache_update is not None:
        set_cached_japan_api_url(db_path, *cache_update)

    if bootstrap_error is None:
        try:
            addressable_resp = session.get(resolved_api_url)
            addressable_resp.raise_for_status()
            addressable = addressable_resp.json()
            catalog_root = _japan_catalog_root(addressable)
        except _CATALOG_FETCH_ERRORS as exc:
            bootstrap_error = exc

    if bootstrap_error is not None:
        logger.warning(
            "Japan catalog bootstrap failed (%s); keeping cached catalogs and "
            "deferring requested due targets", bootstrap_error,
        )
        uncached = _defer_japan_failures(
            db_path, due_targets, stored_versions, defer_on_failure,
        )
        if uncached:
            raise ResourceUnavailableError(
                "Could not fetch first Japan catalog(s): " + ", ".join(uncached)
            ) from bootstrap_error
        return results

    failed_first_fetches = []
    for target in due_targets:
        patch_pack = _JAPAN_PATCH_PACKS[target]
        bundle_url = f"{catalog_root}/{patch_pack}/BundlePackingInfo.json"
        logger.info("Downloading %s catalog...", target)
        try:
            bundle_resp = session.get(bundle_url)
            bundle_resp.raise_for_status()
            bundle_data = bundle_resp.json()
            files_to_save = _japan_pack_rows(bundle_data, catalog_root, patch_pack)
        except _CATALOG_FETCH_ERRORS as exc:
            logger.warning(
                "Japan catalog fetch failed for %s (%s); keeping its cached "
                "catalog and deferring re-check", target, exc,
            )
            if stored_versions[target] is None:
                failed_first_fetches.append((target, exc))
            else:
                set_defer(db_path, target, datetime.now() + defer_on_failure)
            results[target] = False
            continue

        table_name = get_table_name(target)
        existing_files = get_game_files(db_path, table_name)
        catalog_changed = {_row_key(row) for row in existing_files} != {
            _row_key(row) for row in files_to_save
        }
        is_new_version = current_version != stored_versions[target]
        if catalog_changed:
            save_game_files(db_path, table_name, files_to_save)
            logger.info("Updated %s", target)
        else:
            logger.info("%s catalog unchanged", target)
        update_version(db_path, target, current_version, is_new_version)
        results[target] = is_new_version or catalog_changed

    if failed_first_fetches:
        target, exc = failed_first_fetches[0]
        raise ResourceUnavailableError(
            "Could not fetch first Japan catalog for " + target
        ) from exc
    return results
