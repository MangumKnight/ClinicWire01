"""
Phone number normalization utilities for US numbers
"""

import re
import os
from typing import Optional, Tuple

def normalize_us_number(raw: str, default_area_code: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """
    Normalize US phone number to E.164 format
    
    Args:
        raw: Raw phone input (messy format OK)
        default_area_code: Default area code for 7-digit numbers
        
    Returns:
        Tuple of (e164_number, extension)
        
    Raises:
        ValueError: If number is invalid or non-US
    """
    if not raw:
        raise ValueError("Phone number is required")
    
    # Use env var if default not provided
    if not default_area_code:
        default_area_code = os.getenv('DEFAULT_AREA_CODE', '919')
    
    # Extract extension if present
    extension = None
    ext_patterns = [
        r'(?:ext|x|extension)\.?\s*(\d+)',  # ext 123, x123, extension 123
        r',\s*(\d+)$',  # ,123 at end
        r'\s+(\d{3,6})$'  # Space followed by 3-6 digits at end
    ]
    
    for pattern in ext_patterns:
        match = re.search(pattern, raw, re.IGNORECASE)
        if match:
            extension = match.group(1)
            # Remove extension from raw number
            raw = raw[:match.start()]
            break
    
    # Remove all non-digits
    digits = re.sub(r'\D', '', raw)
    
    # Handle different lengths
    if len(digits) == 7:
        # Local number, add default area code
        digits = default_area_code + digits
    elif len(digits) == 10:
        # Standard US number
        pass
    elif len(digits) == 11 and digits[0] == '1':
        # US number with country code
        digits = digits[1:]
    else:
        raise ValueError(f"Invalid US phone number length: {len(digits)} digits")
    
    # Validate area code and exchange
    area_code = digits[:3]
    exchange = digits[3:6]
    
    # Basic US validation (area code and exchange can't start with 0 or 1)
    if area_code[0] in '01' or exchange[0] in '01':
        raise ValueError("Invalid US phone number format")
    
    # Format as E.164
    e164 = f"+1{digits}"
    
    return e164, extension

def is_valid_e164(phone: str) -> bool:
    """Check if phone number is valid E.164 format"""
    return bool(re.match(r'^\+1\d{10}$', phone))

def format_display(e164: str) -> str:
    """Format E.164 for display (XXX) XXX-XXXX"""
    if not is_valid_e164(e164):
        return e164
    
    # Remove +1
    digits = e164[2:]
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

def extract_base_number(e164: str) -> str:
    """Extract base 10-digit number from E.164"""
    if not is_valid_e164(e164):
        raise ValueError("Invalid E.164 format")
    return e164[2:]  # Remove +1