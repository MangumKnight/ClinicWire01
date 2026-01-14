"""
Magic link authentication system
Handles passwordless authentication via email
"""

import os
import secrets
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
import aiosmtplib
from email.message import EmailMessage

from db.models_multitenant import User, AuthCode, Organization, OrgMembership

logger = logging.getLogger(__name__)

# Email configuration
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("EMAIL_FROM", "noreply@clinicwire.com")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000")
APP_ENV = os.getenv("APP_ENV", "development")

# Magic link settings
MAGIC_LINK_EXPIRY_MINUTES = 15
MAGIC_LINK_CODE_LENGTH = 32

class MagicLinkHandler:
    """Handles magic link generation and validation"""
    
    @staticmethod
    def generate_code() -> str:
        """Generate a secure random code"""
        return secrets.token_urlsafe(MAGIC_LINK_CODE_LENGTH)
    
    @staticmethod
    def hash_code(code: str) -> str:
        """Create SHA256 hash of code for storage"""
        return hashlib.sha256(code.encode()).hexdigest()
    
    @staticmethod
    async def create_auth_code(
        email: str,
        session: AsyncSession,
        ip_address: Optional[str] = None
    ) -> str:
        """Create a new auth code for email"""
        
        # Generate code
        code = MagicLinkHandler.generate_code()
        code_hash = MagicLinkHandler.hash_code(code)
        
        # Set expiration
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=MAGIC_LINK_EXPIRY_MINUTES)
        
        # Delete any existing unused codes for this email
        await session.execute(
            select(AuthCode)
            .where(
                and_(
                    AuthCode.email == email.lower(),
                    AuthCode.used_at.is_(None),
                    AuthCode.expires_at > datetime.now(timezone.utc)
                )
            )
        )
        
        # Create new code
        auth_code = AuthCode(
            email=email.lower(),
            code_hash=code_hash,
            expires_at=expires_at,
            ip_address=ip_address
        )
        session.add(auth_code)
        await session.commit()
        
        return code
    
    @staticmethod
    async def validate_code(
        email: str,
        code: str,
        session: AsyncSession
    ) -> bool:
        """Validate an auth code"""
        
        code_hash = MagicLinkHandler.hash_code(code)
        
        # Find the auth code
        result = await session.execute(
            select(AuthCode)
            .where(
                and_(
                    AuthCode.email == email.lower(),
                    AuthCode.code_hash == code_hash,
                    AuthCode.used_at.is_(None),
                    AuthCode.expires_at > datetime.now(timezone.utc)
                )
            )
        )
        auth_code = result.scalar_one_or_none()
        
        if not auth_code:
            return False
        
        # Mark as used
        auth_code.used_at = datetime.now(timezone.utc)
        await session.commit()
        
        return True
    
    @staticmethod
    async def send_magic_link(
        email: str,
        code: str,
        org_slug: Optional[str] = None
    ) -> Tuple[bool, str]:
        """
        Send magic link email.

        Returns:
            Tuple of (success: bool, status: str)
            - (True, "sent") - Email sent successfully
            - (True, "dev_mode") - Dev mode, link logged
            - (False, "smtp_not_configured") - Production without SMTP
            - (False, "send_failed") - SMTP send failed
        """

        # Build magic link URL
        magic_link = f"{FRONTEND_URL}/auth/verify?"
        magic_link += f"email={email}&code={code}"
        if org_slug:
            magic_link += f"&org={org_slug}"

        # Check environment and SMTP configuration
        is_dev = APP_ENV.lower() in ("development", "dev", "local")

        # In development mode, always use dev fallback (don't attempt SMTP)
        if is_dev:
            # Log only that a magic link was generated (no URLs or codes in logs)
            logger.info(f"[DEV] Magic link generated for {email}")
            # Print redacted info to console for local testing
            print(f"\n{'='*60}")
            print(f"DEVELOPMENT MODE - Magic Link")
            print(f"Email: {email}")
            print(f"Verify at: {FRONTEND_URL}/auth/verify")
            print(f"Code: {code[:4]}...{code[-4:]}")
            print(f"{'='*60}\n")
            return (True, "dev_mode")

        # In production, check SMTP configuration
        smtp_configured = bool(SMTP_USER and SMTP_PASSWORD)
        if not smtp_configured:
            logger.error("SMTP not configured in production environment")
            return (False, "smtp_not_configured")

        # Create email message
        msg = EmailMessage()
        msg['Subject'] = 'Your ClinicWire Login Link'
        msg['From'] = EMAIL_FROM
        msg['To'] = email

        # Plain text body (set FIRST, before adding alternatives)
        text_body = f"""Login to ClinicWire

Click this link to log in to your account:
{magic_link}

This link will expire in {MAGIC_LINK_EXPIRY_MINUTES} minutes.

If you didn't request this email, you can safely ignore it.
"""
        msg.set_content(text_body)

        # HTML body (add as alternative AFTER setting plain text)
        html_body = f"""<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .button {{
            display: inline-block;
            padding: 12px 24px;
            background-color: #667eea;
            color: white;
            text-decoration: none;
            border-radius: 5px;
            font-weight: bold;
        }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #666; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Login to ClinicWire</h2>

        <p>Click the button below to log in to your ClinicWire account:</p>

        <p style="margin: 30px 0;">
            <a href="{magic_link}" class="button">Log In to ClinicWire</a>
        </p>

        <p>Or copy and paste this link into your browser:</p>
        <p style="word-break: break-all; color: #667eea;">{magic_link}</p>

        <div class="footer">
            <p>This link will expire in {MAGIC_LINK_EXPIRY_MINUTES} minutes.</p>
            <p>If you didn't request this email, you can safely ignore it.</p>
        </div>
    </div>
</body>
</html>"""
        msg.add_alternative(html_body, subtype='html')

        # Send email
        try:
            await aiosmtplib.send(
                msg,
                hostname=SMTP_HOST,
                port=SMTP_PORT,
                username=SMTP_USER,
                password=SMTP_PASSWORD,
                start_tls=True
            )
            logger.info(f"Magic link email sent to {email}")
            return (True, "sent")
        except Exception as e:
            logger.error(f"Failed to send magic link email: {e}")
            return (False, "send_failed")

async def get_or_create_user(
    email: str,
    name: Optional[str],
    org_slug: Optional[str],
    session: AsyncSession
) -> tuple[User, Optional[Organization]]:
    """Get existing user or create new one with optional org"""
    
    # Check if user exists
    result = await session.execute(
        select(User).where(User.email == email.lower())
    )
    user = result.scalar_one_or_none()
    
    org = None
    
    # If org_slug provided, find the organization
    if org_slug:
        result = await session.execute(
            select(Organization)
            .where(
                and_(
                    Organization.slug == org_slug,
                    Organization.deleted_at.is_(None)
                )
            )
        )
        org = result.scalar_one_or_none()
    
    if user:
        # Update last login
        user.last_login_at = datetime.now(timezone.utc)
        
        # If org provided and user not a member, add them
        if org:
            result = await session.execute(
                select(OrgMembership)
                .where(
                    and_(
                        OrgMembership.user_id == user.id,
                        OrgMembership.org_id == org.id
                    )
                )
            )
            membership = result.scalar_one_or_none()
            
            if not membership:
                membership = OrgMembership(
                    user_id=user.id,
                    org_id=org.id,
                    role='member'
                )
                session.add(membership)
    else:
        # Create new user
        user = User(
            email=email.lower(),
            name=name or email.split('@')[0],
            email_verified_at=datetime.now(timezone.utc),
            last_login_at=datetime.now(timezone.utc)
        )
        session.add(user)
        await session.flush()
        
        # If org provided, add user as member
        if org:
            membership = OrgMembership(
                user_id=user.id,
                org_id=org.id,
                role='member'
            )
            session.add(membership)
        else:
            # Create a new org for the user
            org_name = f"{user.name}'s Organization"
            org_slug_base = email.split('@')[0].lower()
            org_slug_candidate = org_slug_base
            
            # Ensure unique slug
            counter = 1
            while True:
                result = await session.execute(
                    select(Organization).where(Organization.slug == org_slug_candidate)
                )
                if not result.scalar_one_or_none():
                    break
                counter += 1
                org_slug_candidate = f"{org_slug_base}-{counter}"
            
            org = Organization(
                name=org_name,
                slug=org_slug_candidate
            )
            session.add(org)
            await session.flush()
            
            # Add user as owner
            membership = OrgMembership(
                user_id=user.id,
                org_id=org.id,
                role='owner'
            )
            session.add(membership)
    
    await session.commit()
    
    return user, org