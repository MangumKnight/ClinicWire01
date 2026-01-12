# ClinicWire

Automated POC (Plan of Care) calling system with voice synthesis and SMS notifications.

## Quick Start

### Prerequisites
- Python 3.9+
- PostgreSQL
- Node.js 16+ (for development tools)

### Backend Setup

1. Install dependencies:
```bash
cd backend
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

2. Configure environment:
```bash
cp .env.example .env
# Edit .env with your credentials
```

3. Run database migrations:
```bash
alembic upgrade head
```

4. Start the backend:
```bash
python main.py
# API runs on http://localhost:8001
```

### Frontend (Legacy)

The current frontend is a temporary static HTML interface:

```bash
cd frontend/legacy
python -m http.server 8002
# Open http://localhost:8002/index.html
```

**Note:** This legacy interface will be replaced with a modern React/Next.js dashboard in the next release.

### Development

Run both backend and frontend:
```bash
npm run dev:all
```

Individual commands:
```bash
npm run dev:backend   # Start backend only
npm run dev:frontend  # Start frontend only
npm run test:backend  # Run tests
```

## API Documentation

Once running, visit:
- API Docs: http://localhost:8001/docs
- Health Check: http://localhost:8001/health

## Security

See [SECURITY.md](SECURITY.md) for important security guidelines.

## Configuration

See [docs/SECRETS_REQUIRED.md](docs/SECRETS_REQUIRED.md) for required environment variables.