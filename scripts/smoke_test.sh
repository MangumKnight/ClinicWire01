#!/bin/bash
#
# ClinicWire Smoke Test Script
# Validates core backend functionality
#
# Usage: ./scripts/smoke_test.sh [PORT]
# Default port: 8001
#

set -e

# Configuration
PORT="${1:-8001}"
BASE_URL="http://localhost:${PORT}"
PASS_COUNT=0
FAIL_COUNT=0
RESULTS=()

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
log_pass() {
    echo -e "${GREEN}[PASS]${NC} $1"
    RESULTS+=("PASS: $1")
    ((PASS_COUNT++)) || true
}

log_fail() {
    echo -e "${RED}[FAIL]${NC} $1"
    RESULTS+=("FAIL: $1")
    ((FAIL_COUNT++)) || true
}

log_info() {
    echo -e "${YELLOW}[INFO]${NC} $1"
}

# Check if server is running
check_server() {
    if ! curl -s --connect-timeout 2 "${BASE_URL}/health" > /dev/null 2>&1; then
        echo -e "${RED}ERROR: Server not running on ${BASE_URL}${NC}"
        echo "Start the server with: cd backend && uvicorn main:app --port ${PORT}"
        exit 1
    fi
}

# Test 1: Health check with DB status
test_health() {
    log_info "Test 1: GET /health - expect 200 with services.db == connected"

    RESPONSE=$(curl -s -w "\n%{http_code}" "${BASE_URL}/health")
    HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" != "200" ]; then
        log_fail "/health returned $HTTP_CODE (expected 200)"
        return
    fi

    # Check if db is connected
    DB_STATUS=$(echo "$BODY" | python3 -c "import sys, json; print(json.load(sys.stdin).get('services', {}).get('db', 'unknown'))" 2>/dev/null || echo "parse_error")

    if [ "$DB_STATUS" == "connected" ]; then
        log_pass "/health returns 200, services.db == connected"
    else
        log_fail "/health db status: $DB_STATUS (expected: connected)"
    fi
}

# Test 2: Auth login endpoint
test_auth_login() {
    log_info "Test 2: POST /api/auth/login - expect 202 (dev) or 200 (prod)"

    RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST "${BASE_URL}/api/auth/login" \
        -H "Content-Type: application/json" \
        -d '{"email":"smoketest@example.com"}')
    HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" == "202" ] || [ "$HTTP_CODE" == "200" ]; then
        log_pass "/api/auth/login returns $HTTP_CODE"
    elif [ "$HTTP_CODE" == "503" ]; then
        # 503 is acceptable in production without SMTP
        log_pass "/api/auth/login returns 503 (SMTP not configured - expected in prod)"
    else
        log_fail "/api/auth/login returned $HTTP_CODE (expected 200/202/503)"
        echo "  Response: $BODY"
    fi
}

# Test 3: OpenAPI docs
test_docs() {
    log_info "Test 3: GET /docs - expect 200"

    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${BASE_URL}/docs")

    if [ "$HTTP_CODE" == "200" ]; then
        log_pass "/docs returns 200"
    else
        log_fail "/docs returned $HTTP_CODE (expected 200)"
    fi
}

# Test 4: Protected route without auth
test_tasks_unauth() {
    log_info "Test 4: GET /tasks without auth - expect 401 (not 500)"

    RESPONSE=$(curl -s -w "\n%{http_code}" "${BASE_URL}/tasks")
    HTTP_CODE=$(echo "$RESPONSE" | tail -n1)

    if [ "$HTTP_CODE" == "401" ] || [ "$HTTP_CODE" == "403" ]; then
        log_pass "/tasks without auth returns $HTTP_CODE (auth rejected)"
    elif [ "$HTTP_CODE" == "500" ]; then
        log_fail "/tasks returned 500 (server error - broken auth middleware)"
    else
        log_fail "/tasks returned $HTTP_CODE (expected 401 or 403)"
    fi
}

# Test 5: Twilio webhook
test_webhook() {
    log_info "Test 5: POST /webhooks/twilio/status - expect 200 or controlled 4xx"

    RESPONSE=$(curl -s -w "\n%{http_code}" \
        -X POST "${BASE_URL}/webhooks/twilio/status" \
        -H "Content-Type: application/x-www-form-urlencoded" \
        -d "CallSid=SMOKE_TEST_123&CallStatus=completed&CallDuration=30")
    HTTP_CODE=$(echo "$RESPONSE" | tail -n1)

    if [ "$HTTP_CODE" == "200" ]; then
        log_pass "/webhooks/twilio/status returns 200"
    elif [[ "$HTTP_CODE" =~ ^4[0-9][0-9]$ ]]; then
        log_pass "/webhooks/twilio/status returns $HTTP_CODE (controlled error)"
    elif [ "$HTTP_CODE" == "500" ]; then
        log_fail "/webhooks/twilio/status returned 500 (unhandled error)"
    else
        log_fail "/webhooks/twilio/status returned $HTTP_CODE (unexpected)"
    fi
}

# Main execution
main() {
    echo ""
    echo "========================================"
    echo "  ClinicWire Smoke Test Suite"
    echo "  Target: ${BASE_URL}"
    echo "  Time: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "========================================"
    echo ""

    # Check server is running
    check_server

    # Run tests
    test_health
    test_auth_login
    test_docs
    test_tasks_unauth
    test_webhook

    # Summary
    echo ""
    echo "========================================"
    echo "  RESULTS SUMMARY"
    echo "========================================"
    echo ""
    for result in "${RESULTS[@]}"; do
        echo "  $result"
    done
    echo ""
    echo "----------------------------------------"
    echo -e "  Total: $((PASS_COUNT + FAIL_COUNT)) | ${GREEN}Pass: ${PASS_COUNT}${NC} | ${RED}Fail: ${FAIL_COUNT}${NC}"
    echo "----------------------------------------"
    echo ""

    # Exit with appropriate code
    if [ "$FAIL_COUNT" -gt 0 ]; then
        echo -e "${RED}SMOKE TEST FAILED${NC}"
        exit 1
    else
        echo -e "${GREEN}SMOKE TEST PASSED${NC}"
        exit 0
    fi
}

main "$@"
