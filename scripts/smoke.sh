#!/bin/sh
# Post-deploy smoke check. Sends nothing (uses dry_run).
# Usage: scripts/smoke.sh [BASE_URL] TOKEN_FILE [TARGET]   (TARGET e.g. user:alex enables the routing dry run)
set -u
BASE=${1:-http://127.0.0.1:8930}
TOKEN=$(grep -v "^#" "${2:?token file}" | grep -m1 .)
TARGET=${3:-}
AUTH="Authorization: Bearer $TOKEN"

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "live:        $(curl -s "$BASE/health/live")"
echo "ready:       $(code "$BASE/health/ready") $(curl -s "$BASE/health/ready")"
echo "no token:    $(code -X POST "$BASE/v1/notifications") (expect 401)"
echo "whoami:      $(curl -s -H "$AUTH" "$BASE/v1/whoami")"
if [ -n "$TARGET" ]; then
  echo "dry run:     $(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
    -d "{\"targets\":[\"$TARGET\"],\"message\":\"smoke\",\"severity\":\"critical\",\"dry_run\":true}" \
    "$BASE/v1/notifications")"
else
  echo "dry run:     skipped (pass a target such as user:NAME as the third argument)"
fi
echo "mcp no auth: $(code -X POST "$BASE/mcp") (expect 401)"
echo "mcp tools:   $(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' "$BASE/mcp" | grep -o '"name":"[a-z_]*"' | tr '\n' ' ')"
echo
