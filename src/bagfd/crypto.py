"""
Cryptography utilities for Blue Archive game files.
"""

import base64
import json
import struct
from typing import Dict


class MersenneTwister:
    """Mersenne Twister PRNG implementation.
    
    A pseudo-random number generator based on the Mersenne Twister algorithm.
    Used for generating cryptographic keys from seed values.
    """
    
    def __init__(self, seed: int):
        """Initialize the Mersenne Twister with a seed.
        
        Args:
            seed: Integer seed value for the PRNG.
        """
        self.mt = [0] * 624
        self.index = 624
        self.mt[0] = seed & 0xFFFFFFFF
        
        for i in range(1, 624):
            self.mt[i] = (0x6C078965 * (self.mt[i-1] ^ (self.mt[i-1] >> 30)) + i) & 0xFFFFFFFF
    
    def _twist(self) -> None:
        """Perform the twist transformation on the state."""
        for i in range(624):
            y = (self.mt[i] & 0x80000000) + (self.mt[(i + 1) % 624] & 0x7FFFFFFF)
            self.mt[i] = self.mt[(i + 397) % 624] ^ (y >> 1)
            
            if y % 2 != 0:
                self.mt[i] ^= 0x9908B0DF
        
        self.index = 0
    
    def next_u32(self) -> int:
        """Generate next unsigned 32-bit integer.
        
        Returns:
            A random unsigned 32-bit integer.
        """
        if self.index >= 624:
            self._twist()
        
        y = self.mt[self.index]
        self.index += 1
        
        y ^= (y >> 11)
        y ^= (y << 7) & 0x9D2C5680
        y ^= (y << 15) & 0xEFC60000
        y ^= (y >> 18)
        
        return y & 0xFFFFFFFF
    
    def next_bytes(self, length: int) -> bytes:
        """Generate random bytes.
        
        Args:
            length: Number of bytes to generate.
            
        Returns:
            Random bytes of specified length.
        """
        result = bytearray()
        full_chunks = length // 4
        for _ in range(full_chunks):
            num = self.next_u32() >> 1
            result.extend(num.to_bytes(4, 'little'))
        
        remainder = length % 4
        if remainder > 0:
            num = self.next_u32() >> 1
            result.extend(num.to_bytes(4, 'little')[:remainder])
        
        return bytes(result)


def xxhash32(data: bytes, seed: int = 0) -> int:
    """Calculate XXHash32 of data.
    
    Args:
        data: Bytes to hash.
        seed: Optional seed for the hash (default: 0).
        
    Returns:
        Hash value as integer.
        
    Raises:
        ImportError: If xxhash library is not installed.
    """
    try:
        import xxhash
        return xxhash.xxh32(data, seed=seed).intdigest()
    except ImportError:
        raise ImportError("Please install xxhash: pip install xxhash")


def xxhash32_str(s: str) -> int:
    """Calculate XXHash32 from string.
    
    Args:
        s: String to hash.
        
    Returns:
        Hash value as integer.
    """
    if not s:
        return 0
    return xxhash32(s.encode('utf-8'))


def create_key(name: str) -> bytes:
    """Create 8-byte encryption key from name.
    
    Uses XXHash32 of the name as seed for Mersenne Twister PRNG.
    
    Args:
        name: Key name (e.g., "GameMainConfig").
        
    Returns:
        8 bytes of key material.
    """
    hash_value = xxhash32_str(name)
    mt = MersenneTwister(hash_value)
    return mt.next_bytes(8)


def xor_inplace(data: bytearray, key: bytes) -> None:
    """XOR data with key in place (cycling key).
    
    Args:
        data: Data to XOR (modified in place).
        key: Key bytes (cycled if data is longer than key).
    """
    key_len = len(key)
    for i in range(len(data)):
        data[i] ^= key[i % key_len]


def decrypt_string(value: str, key: bytes) -> str:
    """Decrypt base64-encoded encrypted string.
    
    The encrypted string is base64-encoded, then XORed with a key,
    then UTF-16LE decoded.
    
    Args:
        value: Base64-encoded encrypted string.
        key: Decryption key bytes.
        
    Returns:
        Decrypted string.
    """
    if not value:
        return ""
    
    # 1. Decode base64
    data = bytearray(base64.b64decode(value))
    
    # 2. XOR with key
    xor_inplace(data, key)
    
    # 3. Convert from UTF-16LE to string
    utf16_values = []
    for i in range(0, len(data), 2):
        if i + 1 < len(data):
            char_code = struct.unpack('<H', data[i:i+2])[0]
            utf16_values.append(char_code)
    
    result = ''.join(chr(c) for c in utf16_values)
    return result


def encrypt_string(value: str, key: bytes) -> str:
    """Encrypt string to base64.
    
    The string is UTF-16LE encoded, then XORed with a key,
    then base64-encoded.
    
    Args:
        value: String to encrypt.
        key: Encryption key bytes.
        
    Returns:
        Base64-encoded encrypted string.
    """
    if not value:
        return ""
    
    # 1. Convert to UTF-16LE bytes
    data = bytearray()
    for char in value:
        data.extend(struct.pack('<H', ord(char)))
    
    # 2. XOR with key
    xor_inplace(data, key)
    
    # 3. Encode to base64
    return base64.b64encode(data).decode('ascii')


def extract_json_from_string(text: str) -> Dict:
    """Extract first JSON object from string.
    
    Finds the first complete JSON object in the text,
    handling escaped characters and string boundaries properly.
    
    Args:
        text: Text containing JSON data.
        
    Returns:
        Parsed JSON object as dictionary.
        
    Raises:
        ValueError: If text doesn't start with a JSON object.
        json.JSONDecodeError: If JSON parsing fails.
    """
    if not text.startswith('{'):
        raise ValueError("Data doesn't start with {")
    
    brace_count = 0
    in_string = False
    escape_next = False
    
    for i, char in enumerate(text):
        if escape_next:
            escape_next = False
            continue
            
        if char == '\\':
            escape_next = True
            continue
            
        if char == '"' and not escape_next:
            in_string = not in_string
            continue
            
        if not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                
                if brace_count == 0:
                    json_str = text[:i+1]
                    return json.loads(json_str)
    
    return json.loads(text)
