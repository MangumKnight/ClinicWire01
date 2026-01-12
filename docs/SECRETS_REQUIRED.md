# Required Secrets for ClinicWire

This document lists all environment variables required to run ClinicWire in production.

## Critical Secrets (Required)

### Database
- `POSTGRES_URL` - PostgreSQL connection string with asyncpg driver

### Twilio (Voice/SMS)
- `TWILIO_ACCOUNT_SID` - Account identifier from Twilio console
- `TWILIO_AUTH_TOKEN` - Authentication token (keep secret!)
- `TWILIO_FROM_NUMBER` - Your Twilio phone number in E.164 format

### ElevenLabs (Voice Synthesis)
- `ELEVENLABS_API_KEY` - API key for voice synthesis

### Authentication
- `JWT_SECRET_KEY` - Secret key for JWT tokens (generate a strong random key)

## Optional Configuration

### Email (Magic Link Login)
- `SMTP_HOST` - SMTP server hostname
- `SMTP_PORT` - SMTP port (usually 587 for TLS)
- `SMTP_USER` - SMTP username
- `SMTP_PASSWORD` - SMTP password or app-specific password

### Feature Flags
- `ENABLE_WATCHDOG` - Enable stuck call detection (default: false)
- `MAX_HOLD_SECONDS` - Maximum call duration before timeout (default: 1200)
- `ENABLE_OUTCOME_V2` - Enable enhanced outcome tracking (default: false)

### Development/Testing
- `SIMULATE` - Run in simulation mode without real calls (default: true)
- `APP_ENV` - Environment (development/staging/production)

## GitHub Repository Secrets

For GitHub Actions, configure these secrets in your repository settings:

1. `POSTGRES_URL`
2. `TWILIO_ACCOUNT_SID`
3. `TWILIO_AUTH_TOKEN`
4. `TWILIO_FROM_NUMBER`
5. `ELEVENLABS_API_KEY`
6. `JWT_SECRET_KEY`

## Security Notes

- Never commit these values to version control
- Rotate keys immediately if exposed
- Use different keys for development/staging/production
- Enable 2FA on all service accounts