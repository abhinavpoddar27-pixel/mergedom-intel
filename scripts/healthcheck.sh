#!/usr/bin/env bash
# Health check script for monitoring systems (cron, uptime robot, etc.)
# Exits 0 if healthy, 1 otherwise.
set -euo pipefail

RESPONSE=$(curl -sf http://localhost:8000/health 2>/dev/null) || {
    echo "UNHEALTHY: service unreachable"
    exit 1
}

STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status',''))" 2>/dev/null)

if [ "$STATUS" = "ok" ]; then
    echo "$RESPONSE" | python3 -m json.tool
    exit 0
else
    echo "UNHEALTHY: status=$STATUS"
    echo "$RESPONSE"
    exit 1
fi
