"""
Database connection and session management
"""

import os
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get database URL from environment
DATABASE_URL = os.getenv("POSTGRES_URL", "postgresql+asyncpg://user:pass@localhost/dbname")

# Create async engine
engine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("DB_ECHO", "false").lower() == "true",
    poolclass=NullPool,  # Disable connection pooling for serverless
)

# Create async session factory
async_session_maker = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get database session for dependency injection"""
    async with async_session_maker() as session:
        yield session

async def close_database() -> None:
    """Close database connections"""
    await engine.dispose()