"""
Authentication routes
Handles login, logout, and session management
"""

import os
import uuid
import secrets
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from db.database import get_session
from db.models_multitenant import UserSession, Organization, OrgMembership, User
from auth.jwt_handler import JWTHandler, get_current_user, get_optional_user, AuthContext
from auth.magic_link import MagicLinkHandler, get_or_create_user

router = APIRouter(prefix="/api/auth", tags=["authentication"])

# Request/Response models
class LoginRequest(BaseModel):
    email: EmailStr
    org_slug: Optional[str] = Field(None, description="Organization to join/access")
    name: Optional[str] = Field(None, description="Name for new users")

class LoginResponse(BaseModel):
    message: str
    email: str

class VerifyRequest(BaseModel):
    email: EmailStr
    code: str
    org_slug: Optional[str] = None

class VerifyResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict
    organizations: list[dict]

class ProfileResponse(BaseModel):
    id: str
    email: str
    name: str
    email_verified: bool
    created_at: datetime
    organizations: list[dict]

class PasswordLoginRequest(BaseModel):
    email: EmailStr
    password: str

class PasswordLoginResponse(BaseModel):
    token: str
    is_demo: bool
    user: dict
    organizations: list[dict]

# Endpoints
@router.post("/login", response_model=LoginResponse, status_code=status.HTTP_202_ACCEPTED)
async def login(
    request: LoginRequest,
    req: Request,
    session: AsyncSession = Depends(get_session)
):
    """Send magic link to email"""

    # Generate auth code
    code = await MagicLinkHandler.create_auth_code(
        request.email,
        session,
        req.client.host if req.client else None
    )

    # Send magic link
    success, send_status = await MagicLinkHandler.send_magic_link(
        request.email,
        code,
        request.org_slug
    )

    if not success:
        if send_status == "smtp_not_configured":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Email service unavailable"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to send login email"
            )

    # Return appropriate message based on mode
    if send_status == "dev_mode":
        return LoginResponse(
            message="Development mode: check server console for login link",
            email=request.email
        )

    return LoginResponse(
        message="Check your email for login link",
        email=request.email
    )

@router.post("/verify", response_model=VerifyResponse)
async def verify_magic_link(
    request: VerifyRequest,
    req: Request,
    session: AsyncSession = Depends(get_session)
):
    """Verify magic link code and create session"""
    
    # Validate code
    valid = await MagicLinkHandler.validate_code(
        request.email,
        request.code,
        session
    )
    
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired login link"
        )
    
    # Get or create user
    user, org = await get_or_create_user(
        request.email,
        None,  # Name will be set from email if new user
        request.org_slug,
        session
    )
    
    # Create session
    session_id = str(uuid.uuid4())
    token, expires_at = JWTHandler.create_token(
        str(user.id),
        user.email,
        session_id
    )
    
    # Store session
    user_session = UserSession(
        id=uuid.UUID(session_id),
        user_id=user.id,
        token_hash=JWTHandler.hash_token(token),
        expires_at=expires_at,
        ip_address=req.client.host if req.client else None,
        user_agent=req.headers.get("user-agent", "")[:500]
    )
    session.add(user_session)
    await session.commit()
    
    # Get user's organizations
    await session.refresh(user, ["orgs"])
    organizations = [
        {
            "id": str(m.org.id),
            "name": m.org.name,
            "slug": m.org.slug,
            "role": m.role
        }
        for m in user.orgs
        if m.org.deleted_at is None
    ]
    
    return VerifyResponse(
        access_token=token,
        user={
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "email_verified": user.email_verified_at is not None
        },
        organizations=organizations
    )

@router.get("/me", response_model=ProfileResponse)
async def get_profile(
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Get current user profile"""
    
    organizations = [
        {
            "id": str(m.org.id),
            "name": m.org.name,
            "slug": m.org.slug,
            "role": m.role,
            "joined_at": m.created_at
        }
        for m in auth.org_memberships
        if m.org.deleted_at is None
    ]
    
    return ProfileResponse(
        id=str(auth.user.id),
        email=auth.user.email,
        name=auth.user.name,
        email_verified=auth.user.email_verified_at is not None,
        created_at=auth.user.created_at,
        organizations=organizations
    )

@router.post("/logout")
async def logout(
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Logout current session"""
    
    # Delete the session
    await session.delete(auth.session)
    await session.commit()
    
    return {"message": "Logged out successfully"}

@router.delete("/sessions/{session_id}")
async def revoke_session(
    session_id: str,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """Revoke a specific session"""
    
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid session ID"
        )
    
    # Find session
    user_session = await session.get(UserSession, session_uuid)
    
    if not user_session or user_session.user_id != auth.user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
    
    # Delete session
    await session.delete(user_session)
    await session.commit()
    
    return {"message": "Session revoked"}

@router.get("/sessions")
async def list_sessions(
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session)
):
    """List all active sessions for current user"""
    
    # Get all active sessions
    result = await session.execute(
        select(UserSession)
        .where(
            and_(
                UserSession.user_id == auth.user.id,
                UserSession.expires_at > datetime.now(timezone.utc)
            )
        )
        .order_by(UserSession.last_used_at.desc())
    )
    sessions = result.scalars().all()
    
    return [
        {
            "id": str(s.id),
            "created_at": s.created_at,
            "last_used_at": s.last_used_at,
            "expires_at": s.expires_at,
            "ip_address": s.ip_address,
            "user_agent": s.user_agent,
            "is_current": s.id == auth.session.id
        }
        for s in sessions
    ]

@router.post("/login-password", response_model=PasswordLoginResponse)
async def login_password(
    request: PasswordLoginRequest,
    req: Request,
    session: AsyncSession = Depends(get_session)
):
    """Password login for demo account only"""
    
    # Only allow demo account to use password login
    if request.email.lower() != "demo@clinicwire.com":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password login is only available for demo account. Please use magic link."
        )
    
    # Constant-time compare for password
    demo_password = os.getenv("DEMO_PASSWORD", "ClinicWireDemo")
    if not secrets.compare_digest(request.password, demo_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )
    
    # Get or create demo organization
    demo_org_slug = os.getenv("GUEST_ORG_SLUG", "demo")
    result = await session.execute(
        select(Organization)
        .where(
            and_(
                Organization.slug == demo_org_slug,
                Organization.deleted_at.is_(None)
            )
        )
    )
    demo_org = result.scalar_one_or_none()
    
    if not demo_org:
        # Create demo org if it doesn't exist
        demo_org = Organization(
            name=os.getenv("GUEST_ORG_NAME", "Demo Organization"),
            slug=demo_org_slug,
            settings={"is_demo": True}
        )
        session.add(demo_org)
        await session.flush()
    
    # Get or create demo user
    result = await session.execute(
        select(User).where(User.email == "demo@clinicwire.com")
    )
    user = result.scalar_one_or_none()
    
    if not user:
        # Create demo user
        user = User(
            email="demo@clinicwire.com",
            name="Demo User",
            email_verified_at=datetime.now(timezone.utc),
            last_login_at=datetime.now(timezone.utc)
        )
        session.add(user)
        await session.flush()
    else:
        # Update last login
        user.last_login_at = datetime.now(timezone.utc)
    
    # Ensure membership exists
    result = await session.execute(
        select(OrgMembership)
        .where(
            and_(
                OrgMembership.user_id == user.id,
                OrgMembership.org_id == demo_org.id
            )
        )
    )
    membership = result.scalar_one_or_none()
    
    if not membership:
        membership = OrgMembership(
            user_id=user.id,
            org_id=demo_org.id,
            role='admin'  # Demo user gets admin role
        )
        session.add(membership)
    
    await session.commit()
    
    # Create JWT session
    session_id = str(uuid.uuid4())
    token, expires_at = JWTHandler.create_token(
        str(user.id),
        user.email,
        session_id
    )
    
    # Store session
    user_session = UserSession(
        id=uuid.UUID(session_id),
        user_id=user.id,
        token_hash=JWTHandler.hash_token(token),
        expires_at=expires_at,
        ip_address=req.client.host if req.client else None,
        user_agent=req.headers.get("user-agent", "")[:500]
    )
    session.add(user_session)
    await session.commit()
    
    return PasswordLoginResponse(
        token=token,
        is_demo=True,
        user={
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "email_verified": True
        },
        organizations=[{
            "id": str(demo_org.id),
            "name": demo_org.name,
            "slug": demo_org.slug,
            "role": "admin"
        }]
    )

# Guest demo endpoints
@router.post("/demo/login", response_model=VerifyResponse)
async def demo_login(
    req: Request,
    session: AsyncSession = Depends(get_session)
):
    """Create a guest demo session"""
    
    if not os.getenv("ENABLE_GUEST_DEMO", "false").lower() == "true":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Demo mode not available"
        )
    
    # Get demo organization
    demo_org_slug = os.getenv("GUEST_ORG_SLUG", "demo")
    result = await session.execute(
        select(Organization)
        .where(
            and_(
                Organization.slug == demo_org_slug,
                Organization.deleted_at.is_(None)
            )
        )
    )
    demo_org = result.scalar_one_or_none()
    
    if not demo_org:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Demo organization not configured"
        )
    
    # Create guest user
    guest_email = f"guest-{uuid.uuid4().hex[:8]}@demo.clinicwire.com"
    guest_name = f"Guest User {uuid.uuid4().hex[:4].upper()}"
    
    user, _ = await get_or_create_user(
        guest_email,
        guest_name,
        demo_org_slug,
        session
    )
    
    # Set guest role
    result = await session.execute(
        select(OrgMembership)
        .where(
            and_(
                OrgMembership.user_id == user.id,
                OrgMembership.org_id == demo_org.id
            )
        )
    )
    membership = result.scalar_one()
    membership.role = 'guest'
    
    # Create session with shorter expiry for guests
    session_id = str(uuid.uuid4())
    token, expires_at = JWTHandler.create_token(
        str(user.id),
        user.email,
        session_id
    )
    
    # Store session
    user_session = UserSession(
        id=uuid.UUID(session_id),
        user_id=user.id,
        token_hash=JWTHandler.hash_token(token),
        expires_at=expires_at,
        ip_address=req.client.host if req.client else None,
        user_agent=req.headers.get("user-agent", "")[:500]
    )
    session.add(user_session)
    await session.commit()
    
    return VerifyResponse(
        access_token=token,
        user={
            "id": str(user.id),
            "email": user.email,
            "name": user.name,
            "email_verified": True
        },
        organizations=[{
            "id": str(demo_org.id),
            "name": demo_org.name,
            "slug": demo_org.slug,
            "role": "guest"
        }]
    )