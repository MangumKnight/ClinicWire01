"""
Auth-aware database session management
Sets RLS context for multi-tenant queries
"""

from typing import AsyncGenerator, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from fastapi import Depends

from db.database import async_session_maker
from auth.jwt_handler import get_current_user, get_optional_user, AuthContext


async def get_auth_session(
    auth: AuthContext = Depends(get_current_user)
) -> AsyncGenerator[AsyncSession, None]:
    """Get database session with RLS context set"""
    async with async_session_maker() as session:
        # Set the current user ID for RLS policies
        await session.execute(
            text(f"SET LOCAL app.current_user_id = '{str(auth.user.id)}'")
        )
        yield session


async def get_optional_auth_session(
    auth: Optional[AuthContext] = Depends(get_optional_user)
) -> AsyncGenerator[AsyncSession, None]:
    """Get database session with optional RLS context"""
    async with async_session_maker() as session:
        if auth:
            # Set the current user ID for RLS policies
            await session.execute(
                text(f"SET LOCAL app.current_user_id = '{str(auth.user.id)}'")
            )
        yield session