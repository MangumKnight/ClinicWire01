"""
Validation utilities for ClinicWire
"""

import hashlib
import re
from datetime import datetime
from typing import Optional, Dict, Any, Tuple
from .phone import normalize_us_number, is_valid_e164


def validate_phone_number(phone: str, field_name: str = "phone") -> str:
    """
    Validate and normalize a phone number to E.164 format.

    Args:
        phone: Raw phone number input
        field_name: Name of field for error messages

    Returns:
        E.164 formatted phone number

    Raises:
        ValueError: If phone number is invalid
    """
    if not phone or not phone.strip():
        raise ValueError(f"{field_name} is required")

    try:
        e164, _ = normalize_us_number(phone.strip())
        return e164
    except ValueError as e:
        raise ValueError(f"Invalid {field_name}: {str(e)}")


def generate_idempotency_key(
    patient_alias: str,
    doctor_name: str,
    doctor_phone: str,
    workflow_type: str = "POC_SIGNATURE",
    date_str: Optional[str] = None
) -> str:
    """
    Generate SHA256 hash for idempotency key.

    Args:
        patient_alias: Patient identifier/alias
        doctor_name: Doctor's name
        doctor_phone: Doctor's phone (E.164 format preferred)
        workflow_type: Type of workflow
        date_str: Date string (defaults to today's date)

    Returns:
        64-character SHA256 hex digest
    """
    if date_str is None:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    key_data = f"{patient_alias.lower()}|{doctor_name.lower()}|{doctor_phone}|{workflow_type.lower()}|{date_str}"
    return hashlib.sha256(key_data.encode()).hexdigest()


def validate_task_data(data: Dict[str, Any]) -> Tuple[Dict[str, Any], list]:
    """
    Validate task creation data.

    Args:
        data: Dictionary containing task data

    Returns:
        Tuple of (validated_data, errors)
        - validated_data: Cleaned and validated data dict
        - errors: List of validation error messages
    """
    errors = []
    validated = {}

    # Required fields
    required_fields = ['patient_alias', 'doctor_name', 'doctor_phone', 'therapist_phone']

    for field in required_fields:
        value = data.get(field, '').strip() if data.get(field) else ''
        if not value:
            errors.append(f"{field} is required")
        else:
            validated[field] = value

    # Validate phone numbers
    if 'doctor_phone' in validated:
        try:
            validated['doctor_phone'] = validate_phone_number(validated['doctor_phone'], 'doctor_phone')
        except ValueError as e:
            errors.append(str(e))

    if 'therapist_phone' in validated:
        try:
            validated['therapist_phone'] = validate_phone_number(validated['therapist_phone'], 'therapist_phone')
        except ValueError as e:
            errors.append(str(e))

    # Optional fields
    if data.get('workflow_type'):
        validated['workflow_type'] = data['workflow_type'].strip()
    else:
        validated['workflow_type'] = 'POC_SIGNATURE'

    if data.get('notes'):
        validated['notes'] = data['notes'].strip()

    return validated, errors
