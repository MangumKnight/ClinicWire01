"""
Contacts API Router
Handles saved doctor/office contact information
"""

import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field, validator
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import get_session
from db.auth_session import get_auth_session
from db.repo_v2 import ContactRepository
from utils.phone import normalize_us_number
from auth.jwt_handler import get_current_user, AuthContext

router = APIRouter(prefix="/api/contacts", tags=["contacts"])

# Request/Response models
class ContactCreateRequest(BaseModel):
    patient_alias: Optional[str] = None
    doctor_name: str = Field(..., min_length=1)
    office_name: Optional[str] = None
    phone_raw: Optional[str] = None
    fax_raw: Optional[str] = None
    notes: Optional[str] = None
    org_id: Optional[str] = Field(None, description="Organization ID (optional, defaults to user's first org)")

class ContactUpdateRequest(BaseModel):
    patient_alias: Optional[str] = None
    doctor_name: str = Field(..., min_length=1)
    office_name: Optional[str] = None
    phone_raw: Optional[str] = None
    fax_raw: Optional[str] = None
    notes: Optional[str] = None

class ContactVerifyRequest(BaseModel):
    field: str = Field(..., pattern="^(phone|fax)$")

class ContactResponse(BaseModel):
    id: str
    patient_alias: Optional[str]
    doctor_name: str
    office_name: Optional[str]
    phone_e164: Optional[str]
    fax_e164: Optional[str]
    notes: Optional[str]
    last_verified_at: Optional[datetime]
    created_at_utc: datetime
    updated_at_utc: datetime

class ContactSearchResponse(BaseModel):
    items: List[ContactResponse]

# Endpoints
@router.get("", response_model=ContactSearchResponse)
async def search_contacts(
    q: Optional[str] = Query(None, description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Search contacts by doctor name or office name"""
    repo = ContactRepository(session)
    contacts = await repo.search_contacts(q, limit)
    
    items = [
        ContactResponse(
            id=str(contact.id),
            patient_alias=contact.patient_alias,
            doctor_name=contact.doctor_name,
            office_name=contact.office_name,
            phone_e164=contact.phone_e164,
            fax_e164=contact.fax_e164,
            notes=contact.notes,
            last_verified_at=contact.last_verified_at,
            created_at_utc=contact.created_at_utc,
            updated_at_utc=contact.updated_at_utc
        )
        for contact in contacts
    ]
    
    return ContactSearchResponse(items=items)

@router.post("", response_model=ContactResponse)
async def create_contact(
    request: ContactCreateRequest,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Create new contact"""
    # Determine which organization to use
    if request.org_id:
        # If org_id is provided, validate user has access
        try:
            org_uuid = uuid.UUID(request.org_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid organization ID format")
        
        if not auth.has_access_to_org(org_uuid):
            raise HTTPException(status_code=403, detail="Access denied to this organization")
        
        org_id = org_uuid
    else:
        # Default to user's first organization
        if not auth.org_ids:
            raise HTTPException(status_code=403, detail="User is not a member of any organization")
        
        org_id = auth.org_ids[0]
    
    # Normalize phone numbers
    data = {
        "org_id": org_id,
        "patient_alias": request.patient_alias.strip() if request.patient_alias else None,
        "doctor_name": request.doctor_name.strip(),
        "office_name": request.office_name.strip() if request.office_name else None,
        "notes": request.notes,
        "created_by_id": auth.user_id
    }
    
    # Normalize phone if provided
    if request.phone_raw:
        try:
            phone_normalized, _ = normalize_us_number(request.phone_raw)
            data["phone_e164"] = phone_normalized
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid phone number: {str(e)}")
    
    # Normalize fax if provided
    if request.fax_raw:
        try:
            fax_normalized, _ = normalize_us_number(request.fax_raw)
            data["fax_e164"] = fax_normalized
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid fax number: {str(e)}")
    
    repo = ContactRepository(session)
    contact = await repo.create_contact(data)
    
    return ContactResponse(
        id=str(contact.id),
        patient_alias=contact.patient_alias,
        doctor_name=contact.doctor_name,
        office_name=contact.office_name,
        phone_e164=contact.phone_e164,
        fax_e164=contact.fax_e164,
        notes=contact.notes,
        last_verified_at=contact.last_verified_at,
        created_at_utc=contact.created_at_utc,
        updated_at_utc=contact.updated_at_utc
    )

@router.put("/{contact_id}", response_model=ContactResponse)
async def update_contact(
    contact_id: str,
    request: ContactUpdateRequest,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Update existing contact"""
    try:
        contact_uuid = uuid.UUID(contact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contact ID")
    
    # Prepare update data
    data = {
        "patient_alias": request.patient_alias.strip() if request.patient_alias else None,
        "doctor_name": request.doctor_name.strip(),
        "office_name": request.office_name.strip() if request.office_name else None,
        "notes": request.notes
    }
    
    # Normalize phone if provided
    if request.phone_raw:
        try:
            phone_normalized, _ = normalize_us_number(request.phone_raw)
            data["phone_e164"] = phone_normalized
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid phone number: {str(e)}")
    else:
        data["phone_e164"] = None
    
    # Normalize fax if provided
    if request.fax_raw:
        try:
            fax_normalized, _ = normalize_us_number(request.fax_raw)
            data["fax_e164"] = fax_normalized
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Invalid fax number: {str(e)}")
    else:
        data["fax_e164"] = None
    
    repo = ContactRepository(session)
    
    # First check if contact exists and user has access to it
    existing_contact = await repo.get_by_id(contact_uuid)
    if not existing_contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    
    # The RLS policy will ensure the user can only see contacts in their orgs
    # But let's be explicit about checking
    if existing_contact.org_id not in auth.org_ids:
        raise HTTPException(status_code=403, detail="Access denied to this contact")
    
    contact = await repo.update_contact(contact_uuid, data)
    
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    
    return ContactResponse(
        id=str(contact.id),
        patient_alias=contact.patient_alias,
        doctor_name=contact.doctor_name,
        office_name=contact.office_name,
        phone_e164=contact.phone_e164,
        fax_e164=contact.fax_e164,
        notes=contact.notes,
        last_verified_at=contact.last_verified_at,
        created_at_utc=contact.created_at_utc,
        updated_at_utc=contact.updated_at_utc
    )

@router.patch("/{contact_id}/verify")
async def verify_contact_field(
    contact_id: str,
    request: ContactVerifyRequest,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Update last_verified_at for a contact field"""
    try:
        contact_uuid = uuid.UUID(contact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contact ID")
    
    repo = ContactRepository(session)
    success = await repo.verify_field(contact_uuid, request.field)
    
    if not success:
        raise HTTPException(status_code=404, detail="Contact not found")
    
    return {"status": "verified", "field": request.field}

@router.delete("/{contact_id}")
async def delete_contact(
    contact_id: str,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Delete a contact"""
    try:
        contact_uuid = uuid.UUID(contact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid contact ID")
    
    repo = ContactRepository(session)
    contact = await repo.get_by_id(contact_uuid)
    
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    
    await session.delete(contact)
    await session.commit()
    
    return {"status": "deleted", "id": contact_id}