"""
JWT authentication handler
Manages JWT token creation, validation, and session management
"""

import os
import jwt
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
import uuid

from fastapi import HTTPException, status, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from db.models_multitenant import User, UserSession, OrgMembership
from db.database import get_session

# Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-this-secret-key")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_EXPIRATION_HOURS = int(os.getenv("JWT_EXPIRATION_HOURS", "720"))  # 30 days default

# Security scheme
security = HTTPBearer()

class JWTHandler:
    """Handles JWT token operations"""
    
    @staticmethod
    def create_token(user_id: str, email: str, session_id: str) -> tuple[str, datetime]:
        """Create a JWT token for a user"""
        expires_at = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
        
        payload = {
            "user_id": user_id,
            "email": email,
            "session_id": session_id,
            "exp": expires_at,
            "iat": datetime.now(timezone.utc)
        }
        
        token = jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
        return token, expires_at
    
    @staticmethod
    def decode_token(token: str) -> Dict[str, Any]:
        """Decode and validate a JWT token"""
        try:
            payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired"
            )
        except jwt.InvalidTokenError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )
    
    @staticmethod
    def hash_token(token: str) -> str:
        """Create SHA256 hash of token for storage"""
        return hashlib.sha256(token.encode()).hexdigest()

class AuthContext:
    """Authentication context for the current request"""
    def __init__(self, user: User, session: UserSession, org_memberships: list[OrgMembership]):
        self.user = user
        self.session = session
        self.org_memberships = org_memberships
        self.org_ids = [m.org_id for m in org_memberships]
        
    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id
    
    @property
    def email(self) -> str:
        return self.user.email
    
    def has_access_to_org(self, org_id: uuid.UUID) -> bool:
        """Check if user has access to a specific organization"""
        return org_id in self.org_ids
    
    def get_role_in_org(self, org_id: uuid.UUID) -> Optional[str]:
        """Get user's role in a specific organization"""
        for membership in self.org_memberships:
            if membership.org_id == org_id:
                return membership.role
        return None

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    session: AsyncSession = Depends(get_session),
    request: Request = None
) -> AuthContext:
    """Get the current authenticated user from JWT token"""
    
    token = credentials.credentials
    
    # Decode token
    payload = JWTHandler.decode_token(token)
    user_id = payload.get("user_id")
    session_id = payload.get("session_id")
    
    if not user_id or not session_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload"
        )
    
    # Verify session exists and is valid
    token_hash = JWTHandler.hash_token(token)
    db_session = await session.get(UserSession, uuid.UUID(session_id))
    
    if not db_session or db_session.token_hash != token_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session"
        )
    
    if db_session.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has expired"
        )
    
    # Get user
    user = await session.get(User, uuid.UUID(user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found"
        )
    
    # Get user's org memberships
    result = await session.execute(
        select(OrgMembership)
        .where(OrgMembership.user_id == user.id)
        .options(selectinload(OrgMembership.org))
    )
    memberships = result.scalars().all()
    
    # Update session last used
    db_session.last_used_at = datetime.now(timezone.utc)
    if request:
        db_session.ip_address = request.client.host
        db_session.user_agent = request.headers.get("user-agent", "")[:500]
    
    await session.commit()
    
    # Set current user ID for RLS
    await session.execute(
        text(f"SET LOCAL app.current_user_id = '{str(user.id)}'")
    )
    
    return AuthContext(user, db_session, memberships)

async def get_optional_user(
    request: Request,
    session: AsyncSession = Depends(get_session)
) -> Optional[AuthContext]:
    """Get the current user if authenticated, None otherwise"""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    
    try:
        token = auth_header.split(" ")[1]
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        return await get_current_user(credentials, session, request)
    except HTTPException:
        return None

class RequireOrgAccess:
    """Dependency to require access to a specific organization"""
    
    def __init__(self, role: Optional[str] = None):
        self.required_role = role
    
    async def __call__(
        self,
        org_id: str,
        auth: AuthContext = Depends(get_current_user)
    ) -> AuthContext:
        """Verify user has access to the organization"""
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
                detail="Access denied to this organization"
            )
        
        # Check role if specified
        if self.required_role:
            user_role = auth.get_role_in_org(org_uuid)
            if not self._has_required_role(user_role, self.required_role):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Requires {self.required_role} role or higher"
                )
        
        return auth
    
    def _has_required_role(self, user_role: str, required_role: str) -> bool:
        """Check if user role meets requirement"""
        role_hierarchy = {
            "owner": 4,
            "admin": 3,
            "member": 2,
            "guest": 1
        }
        
        user_level = role_hierarchy.get(user_role, 0)
        required_level = role_hierarchy.get(required_role, 0)
        
        return user_level >= required_level

# Convenience dependencies
require_org_member = RequireOrgAccess()
require_org_admin = RequireOrgAccess(role="admin")
require_org_owner = RequireOrgAccess(role="owner")