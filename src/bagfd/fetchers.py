"""
Platform-specific fetching logic for Blue Archive game files.
"""

import logging
import json
import re
import zipfile
from pathlib import Path
from typing import Dict, Tuple
from io import BytesIO
import requests

from .crypto import extract_json_from_string, create_key, encrypt_string, decrypt_string
from .database import (
    should_check_version, get_stored_version, update_version,
    get_table_name, save_game_files
)

logger = logging.getLogger(__name__)


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


def fetch_global_android(session: requests.Session, db_path: Path,
                        force: bool = False, check_interval=None) -> bool:
    """Fetch the Global Android game-file catalog into the database.

    Checks for a new version on the Global Android servers and, if found (or
    ``force``), rewrites the catalog rows in the database. Does not touch any
    download cache — cache invalidation is the caller's responsibility.

    Args:
        session: Requests session with proper headers configured.
        db_path: Path to the SQLite database.
        force: Force fetch regardless of version check interval.
        check_interval: Version check interval (uses default if None).

    Returns:
        True if a new version was found, False otherwise.
    """
    from datetime import timedelta
    if check_interval is None:
        check_interval = timedelta(hours=4)
    
    GLOBAL_API_URL = "https://api-pub.nexon.com/patch/v1.1/version-check"
    PUREAPK_GLOBAL_URL = "https://api.pureapk.com/m/v3/cms/app_version?hl=en-US&package_name=com.nexon.bluearchive"
    
    platform = "global-android"
    
    if not should_check_version(db_path, platform, force, check_interval):
        logger.debug(f"Skipped {platform} (checked recently)")
        return False
    
    logger.info(f"Fetching {platform}...")
    
    # Get version from PureAPK
    response = session.get(PUREAPK_GLOBAL_URL)
    response.raise_for_status()
    
    version_pattern = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
    match = version_pattern.search(response.text)
    
    if not match:
        raise ValueError("Could not extract version from PureAPK")
    
    version = match.group(0)
    stored_version = get_stored_version(db_path, platform)
    is_new_version = version != stored_version
    
    if is_new_version:
        logger.info(f"New version: {stored_version} → {version}")
    else:
        logger.info(f"Version unchanged: {version}")
    
    if is_new_version or force:
        build_number = version.split('.')[-1]
        payload = {
            "market_game_id": "com.nexon.bluearchive",
            "market_code": "playstore",
            "curr_build_version": version,
            "curr_build_number": build_number
        }
        
        addressable = session.post(GLOBAL_API_URL, json=payload).json()
        resource_path = addressable['patch']['resource_path']
        resources = session.get(resource_path).json()
        
        catalog_url = resource_path.replace('/resource-data.json', '')
        
        table_name = get_table_name(platform)
        files_to_save = []
        
        for resource in resources.get('resources', []):
            if '/Android/' in resource['resource_path']:
                files_to_save.append((
                    resource['resource_path'],
                    f"{catalog_url}/{resource['resource_path']}",
                    'md5',
                    resource['resource_hash'],
                    resource['resource_size'],
                    None
                ))
        
        save_game_files(db_path, table_name, files_to_save)
        logger.info(f"Updated {platform}")
    
    update_version(db_path, platform, version, is_new_version)
    
    return is_new_version


def fetch_japan_servers(session: requests.Session, db_path: Path,
                       force: bool = False, check_interval=None) -> Dict[str, bool]:
    """Fetch the Japan Android & Windows game-file catalogs into the database.

    Checks for a new version on the Japan servers and, if found (or ``force``),
    rewrites the catalog rows for both Japan platforms. Does not touch any
    download cache — cache invalidation is the caller's responsibility.

    Args:
        session: Requests session with proper headers configured.
        db_path: Path to the SQLite database.
        force: Force fetch regardless of version check interval.
        check_interval: Version check interval (uses default if None).

    Returns:
        Dict mapping each Japan platform name to whether it has a new version.
    """
    from datetime import timedelta
    if check_interval is None:
        check_interval = timedelta(hours=4)
    
    PUREAPK_JAPAN_URL = "https://api.pureapk.com/m/v3/cms/app_version?hl=en-US&package_name=com.YostarJP.BlueArchive"
    
    results = {}
    current_version = None
    
    japan_platforms = ["japan-android", "japan-windows"]
    
    # Check versions for both platforms
    for platform_name in japan_platforms:
        if not should_check_version(db_path, platform_name, force, check_interval):
            logger.debug(f"Skipped {platform_name} (checked recently)")
            results[platform_name] = False
            continue
        
        logger.info(f"Checking {platform_name}...")
        
        if not current_version:
            response = session.get(PUREAPK_JAPAN_URL)
            response.raise_for_status()
            
            version_pattern = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
            match = version_pattern.search(response.text)
            
            if not match:
                raise ValueError("Could not extract version from PureAPK Japan")
            
            current_version = match.group(0)
        
        stored_version = get_stored_version(db_path, platform_name)
        is_new_version = current_version != stored_version
        
        if is_new_version:
            logger.info(f"New version: {stored_version} → {current_version}")
        else:
            logger.info(f"Version unchanged: {current_version}")
        
        results[platform_name] = is_new_version
    
    # Download and process if any platform has new version
    if any(results.values()) or force:
        logger.info("Downloading Japan APK...")
        
        response = session.get(PUREAPK_JAPAN_URL)
        url_pattern = re.compile(
            r'(X?APKJ)..(https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*))'
        )
        url_match = url_pattern.search(response.text)
        
        if not url_match or len(url_match.groups()) < 2:
            raise ValueError("Could not extract APK URL")
        
        download_url = url_match.group(2)
        logger.info("Downloading XAPK...")
        
        xapk_data = BytesIO(session.get(download_url, stream=True).content)
        logger.info("Extracting config...")
        api_url = _extract_japan_api_url(session, xapk_data)
        
        logger.info("Fetching catalogs...")
        addressable = session.get(api_url).json()
        connection_groups = addressable.get("ConnectionGroups", [])
        if not connection_groups:
            raise ValueError("No ConnectionGroups in addressable response")
        
        override_groups = connection_groups[0].get("OverrideConnectionGroups", [])
        if len(override_groups) < 2:
            raise ValueError(f"Expected at least 2 OverrideConnectionGroups, got {len(override_groups)}")
        
        catalog_url = override_groups[1].get("AddressablesCatalogUrlRoot", "")
        if not catalog_url:
            raise ValueError("AddressablesCatalogUrlRoot not found or empty in OverrideConnectionGroups[1]")
        
        # Process both platforms
        for platform_name in japan_platforms:
            platform_key = "Windows" if platform_name == "japan-windows" else "Android"
            patch_pack = f"{platform_key}_PatchPack"
            logger.info(f"Downloading {platform_name} catalog...")
            
            bundle_url = f"{catalog_url}/{patch_pack}/BundlePackingInfo.json"
            bundle_data = session.get(bundle_url).json()
            
            table_name = get_table_name(platform_name)
            files_to_save = []
            
            all_packs = bundle_data.get('FullPatchPacks', []) + bundle_data.get('UpdatePacks', [])
            
            for pack in all_packs:
                bundle_files = json.dumps([bf['Name'] for bf in pack.get('BundleFiles', [])])
                
                files_to_save.append((
                    pack['PackName'],
                    f"{catalog_url}/{patch_pack}/{pack['PackName']}",
                    'crc32',
                    str(pack['Crc']),
                    pack['PackSize'],
                    bundle_files
                ))
            
            save_game_files(db_path, table_name, files_to_save)
            logger.info(f"Updated {platform_name}")
    
    # Update version info for all platforms
    if current_version:
        for platform_name in japan_platforms:
            update_version(db_path, platform_name, current_version, results[platform_name])
    
    return results
