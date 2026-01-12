"""
Validation utilities for 33Health MCP
"""

import re
import hashlib
from typing import Dict, Any, Optional

def validate_phone_number(phone: str) -> Optional[str]:
    """
    Validate and normalize phone number
    Returns normalized number or None if invalid
    """
    if not phone:
        return None
    
    # Remove all non-numeric characters
    digits = re.sub(r'\D', '', phone)
    
    # Check length (10 digits for US numbers, 11 if starts with 1)
    if len(digits) == 11 and digits[0] == '1':
        digits = digits[1:]  # Remove country code
    
    if len(digits) != 10:
        return None
    
    # Format as standard US number
    return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"

def generate_idempotency_key(data: Dict[str, Any]) -> str:
    """
    Generate idempotency key for task deduplication
    Based on: patient_name + doctor_name + date_sent + workflow_type
    """
    key_parts = [
        data.get('patient_name', '').lower().strip(),
        data.get('doctor_name', '').lower().strip(),
        data.get('date_sent', '').strip(),
        data.get('workflow_type', 'poc_followup').lower()
    ]
    
    key_string = "|".join(key_parts)
    return hashlib.sha256(key_string.encode()).hexdigest()

def validate_task_data(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and clean task data
    Returns cleaned data or raises ValueError
    """
    errors = []
    
    # Required fields
    required_fields = ['patient_name', 'doctor_name', 'doctor_phone', 'therapist_phone']
    for field in required_fields:
        if not data.get(field):
            errors.append(f"Missing required field: {field}")
    
    # Validate phone numbers
    doctor_phone = validate_phone_number(data.get('doctor_phone', ''))
    if not doctor_phone:
        errors.append("Invalid doctor phone number")
    else:
        data['doctor_phone'] = doctor_phone
    
    therapist_phone = validate_phone_number(data.get('therapist_phone', ''))
    if not therapist_phone:
        errors.append("Invalid therapist phone number")
    else:
        data['therapist_phone'] = therapist_phone
    
    # Validate fax if provided
    if data.get('fax_number'):
        fax_number = validate_phone_number(data['fax_number'])
        if fax_number:
            data['fax_number'] = fax_number
    
    # Clean names
    if 'patient_name' in data:
        data['patient_name'] = data['patient_name'].strip()
    if 'doctor_name' in data:
        data['doctor_name'] = data['doctor_name'].strip()
    
    if errors:
        raise ValueError("; ".join(errors))
    
    return data