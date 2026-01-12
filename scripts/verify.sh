#!/bin/bash
# ClinicWire Verification Script
# Ensures the system can start from a clean clone

set -e  # Exit on error

echo "=== ClinicWire Verification Script ==="
echo "Testing clean install and basic functionality..."
echo

# Check Python version
echo "1. Checking Python version..."
python3 --version || { echo "ERROR: Python 3 not found"; exit 1; }
echo "✓ Python installed"
echo

# Check PostgreSQL
echo "2. Checking PostgreSQL..."
if command -v pg_isready &> /dev/null; then
    echo "✓ PostgreSQL client found"
else
    echo "⚠ PostgreSQL client not found (pg_isready)"
    echo "  You'll need PostgreSQL running to use the system"
fi
echo

# Check for .env file
echo "3. Checking configuration..."
if [ -f "backend/.env" ]; then
    echo "✓ .env file exists"
else
    echo "⚠ No .env file found"
    echo "  Copy backend/.env.example to backend/.env and configure"
    exit 1
fi
echo

# Install backend dependencies
echo "4. Installing backend dependencies..."
cd backend
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate || . venv/Scripts/activate 2>/dev/null || { echo "ERROR: Failed to activate venv"; exit 1; }
pip install -r requirements.txt -q
echo "✓ Dependencies installed"
echo

# Test backend startup
echo "5. Testing backend startup..."
# Start backend in background
python main.py &
BACKEND_PID=$!
echo "Backend PID: $BACKEND_PID"

# Wait for startup
echo "Waiting for backend to start..."
sleep 5

# Test health endpoint
echo "6. Testing health endpoint..."
if curl -s http://localhost:8001/health > /dev/null; then
    echo "✓ Health endpoint responding"
    HEALTH_RESPONSE=$(curl -s http://localhost:8001/health)
    echo "Response: $HEALTH_RESPONSE"
else
    echo "✗ Health endpoint failed"
    kill $BACKEND_PID 2>/dev/null
    exit 1
fi

# Cleanup
echo
echo "7. Cleaning up..."
kill $BACKEND_PID 2>/dev/null
wait $BACKEND_PID 2>/dev/null

echo
echo "=== Verification Complete ==="
echo "✓ Backend starts successfully"
echo "✓ Health endpoint responds"
echo
echo "To run the system:"
echo "  Backend: cd backend && python main.py"
echo "  Frontend: cd frontend/legacy && python -m http.server 8002"