"""
Organization management routes
Handles organization CRUD and member management
"""

import uuid
from typing import Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field, validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload

from db.database import get_session
from db.models_multitenant import Organization, User, OrgMembership
from auth.jwt_handler import get_current_user, AuthContext, require_org_admin, require_org_owner

router = APIRouter(prefix="/api/organizations", tags=["organizations"])

# Request/Response models
class OrgCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=3, max_length=100)
    
    @validator('slug')
    def validate_slug(cls, v):
        import re
        if not re.match(r'^[a-z0-9-]+$', v):
            raise ValueError('Slug must contain only lowercase letters, numbers, and hyphens')
        return v

class OrgUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    settings: Optional[dict] = None

class OrgResponse(BaseModel):
    id: str
    name: str
    slug: str
    created_at: datetime
    member_count: int
    settings: dict

class MemberInviteRequest(BaseModel):
    email: str
    role: str = Field(..., pattern="^(admin|member)$")
    name: Optional[str] = None

class MemberUpdateRequest(BaseModel):
    role: str = Field(..., pattern="^(admin|member)$")

class MemberResponse(BaseModel):
    id: str
    user: dict
    role: str
    joined_at: datetime

# Endpoints
@router.post("", response_model=OrgResponse)
async def create_organization(
    request: OrgCreateRequest,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Create a new organization"""
    
    # Check if slug is taken
    result = await session.execute(
        select(Organization).where(Organization.slug == request.slug)
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Organization slug already taken"
        )
    
    # Create organization
    org = Organization(
        name=request.name,
        slug=request.slug
    )
    session.add(org)
    await session.flush()
    
    # Add creator as owner
    membership = OrgMembership(
        org_id=org.id,
        user_id=auth.user_id,
        role='owner'
    )
    session.add(membership)
    
    await session.commit()
    
    return OrgResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        created_at=org.created_at,
        member_count=1,
        settings=org.settings
    )

@router.get("/{org_id}", response_model=OrgResponse)
async def get_organization(
    org_id: str,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get organization details"""
    
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization ID"
        )
    
    # Check access
    if not auth.has_access_to_org(org_uuid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    # Get organization with member count
    result = await session.execute(
        select(
            Organization,
            func.count(OrgMembership.id).label('member_count')
        )
        .outerjoin(OrgMembership, OrgMembership.org_id == Organization.id)
        .where(
            and_(
                Organization.id == org_uuid,
                Organization.deleted_at.is_(None)
            )
        )
        .group_by(Organization.id)
    )
    row = result.first()
    
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found"
        )
    
    org, member_count = row
    
    return OrgResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
        created_at=org.created_at,
        member_count=member_count,
        settings=org.settings
    )

@router.patch("/{org_id}")
async def update_organization(
    org_id: str,
    request: OrgUpdateRequest,
    auth: AuthContext = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session)
):
    """Update organization details"""
    
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization ID"
        )
    
    org = await session.get(Organization, org_uuid)
    if not org or org.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found"
        )
    
    # Update fields
    if request.name is not None:
        org.name = request.name
    
    if request.settings is not None:
        org.settings = {**org.settings, **request.settings}
    
    org.updated_at = datetime.now(timezone.utc)
    
    await session.commit()
    
    return {"message": "Organization updated"}

@router.delete("/{org_id}")
async def delete_organization(
    org_id: str,
    auth: AuthContext = Depends(require_org_owner),
    session: AsyncSession = Depends(get_session)
):
    """Soft delete organization"""
    
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization ID"
        )
    
    org = await session.get(Organization, org_uuid)
    if not org or org.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found"
        )
    
    # Soft delete
    org.deleted_at = datetime.now(timezone.utc)
    org.updated_at = datetime.now(timezone.utc)
    
    await session.commit()
    
    return {"message": "Organization deleted"}

# Member management endpoints
@router.get("/{org_id}/members", response_model=List[MemberResponse])
async def list_members(
    org_id: str,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List organization members"""
    
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization ID"
        )
    
    if not auth.has_access_to_org(org_uuid):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied"
        )
    
    # Get members
    result = await session.execute(
        select(OrgMembership)
        .where(OrgMembership.org_id == org_uuid)
        .options(selectinload(OrgMembership.user))
        .order_by(OrgMembership.created_at)
    )
    memberships = result.scalars().all()
    
    return [
        MemberResponse(
            id=str(m.id),
            user={
                "id": str(m.user.id),
                "email": m.user.email,
                "name": m.user.name
            },
            role=m.role,
            joined_at=m.created_at
        )
        for m in memberships
    ]

@router.post("/{org_id}/members")
async def invite_member(
    org_id: str,
    request: MemberInviteRequest,
    auth: AuthContext = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session)
):
    """Invite user to organization"""
    
    try:
        org_uuid = uuid.UUID(org_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid organization ID"
        )
    
    # Check if user exists
    result = await session.execute(
        select(User).where(User.email == request.email.lower())
    )
    user = result.scalar_one_or_none()
    
    if not user:
        # Create invited user
        user = User(
            email=request.email.lower(),
            name=request.name or request.email.split('@')[0]
        )
        session.add(user)
        await session.flush()
    
    # Check if already a member
    result = await session.execute(
        select(OrgMembership)
        .where(
            and_(
                OrgMembership.org_id == org_uuid,
                OrgMembership.user_id == user.id
            )
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is already a member"
        )
    
    # Add membership
    membership = OrgMembership(
        org_id=org_uuid,
        user_id=user.id,
        role=request.role
    )
    session.add(membership)
    
    await session.commit()
    
    # TODO: Send invitation email
    
    return {"message": f"Invited {request.email} as {request.role}"}

@router.patch("/{org_id}/members/{member_id}")
async def update_member_role(
    org_id: str,
    member_id: str,
    request: MemberUpdateRequest,
    auth: AuthContext = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session)
):
    """Update member role"""
    
    try:
        org_uuid = uuid.UUID(org_id)
        member_uuid = uuid.UUID(member_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid ID"
        )
    
    # Get membership
    membership = await session.get(OrgMembership, member_uuid)
    
    if not membership or membership.org_id != org_uuid:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found"
        )
    
    # Can't change owner role
    if membership.role == 'owner':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change owner role"
        )
    
    # Update role
    membership.role = request.role
    
    await session.commit()
    
    return {"message": "Member role updated"}

@router.delete("/{org_id}/members/{member_id}")
async def remove_member(
    org_id: str,
    member_id: str,
    auth: AuthContext = Depends(require_org_admin),
    session: AsyncSession = Depends(get_session)
):
    """Remove member from organization"""
    
    try:
        org_uuid = uuid.UUID(org_id)
        member_uuid = uuid.UUID(member_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid ID"
        )
    
    # Get membership
    membership = await session.get(OrgMembership, member_uuid)
    
    if not membership or membership.org_id != org_uuid:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found"
        )
    
    # Can't remove owner
    if membership.role == 'owner':
        # Check if there are other owners
        result = await session.execute(
            select(func.count(OrgMembership.id))
            .where(
                and_(
                    OrgMembership.org_id == org_uuid,
                    OrgMembership.role == 'owner',
                    OrgMembership.id != member_uuid
                )
            )
        )
        other_owners = result.scalar()
        
        if other_owners == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove last owner"
            )
    
    # Can't remove self
    if membership.user_id == auth.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove yourself"
        )
    
    # Remove membership
    await session.delete(membership)
    await session.commit()
    
    return {"message": "Member removed"}