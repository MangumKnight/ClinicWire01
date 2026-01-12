# Security Policy

## Reporting Security Vulnerabilities

Please report security vulnerabilities to the repository maintainer via private message.

## Security Best Practices

### 1. Never Commit Secrets

**CRITICAL:** Never commit any of the following to version control:
- API keys (Twilio, ElevenLabs, OpenAI, etc.)
- Database passwords
- JWT secret keys
- SMTP credentials
- Any `.env` files with actual values

### 2. Environment Variables

- Use `.env` files for local development only
- Never track `.env` files in git (they should be in `.gitignore`)
- Use `.env.example` as a template with placeholder values
- In production, use proper secret management (environment variables, secret stores)

### 3. API Key Patterns to Watch For

Common patterns that should NEVER be in code:
- `sk_` or `sk-` (API keys)
- `AC` followed by 32 hex characters (Twilio SID)
- `BEGIN PRIVATE KEY`
- `auth_token=`
- `api_key=`

### 4. Pre-commit Checks

Before committing, always run:
```bash
git grep -nE "sk_|sk-|AC[0-9a-f]{32}|auth_token|api_key|password"
```

If any results appear, DO NOT COMMIT.

### 5. If Secrets Are Exposed

If secrets are accidentally committed:
1. Immediately rotate/revoke the exposed credentials
2. Remove the secrets from the repository
3. Consider the keys compromised even if removed
4. Generate new credentials from the service provider

### 6. Development vs Production

- Use separate API keys for development and production
- Enable IP whitelisting where possible
- Use read-only credentials where full access isn't needed
- Enable 2FA on all service accounts

## Compliance

This project handles healthcare-related data. Always:
- Minimize PHI/PII in logs and error messages
- Use HTTPS/TLS for all communications
- Follow HIPAA guidelines for data handling
- Implement proper access controls