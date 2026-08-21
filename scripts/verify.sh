#!/usr/bin/env bash
#
# End-to-end verification of the newsletter-services stack under Docker.
# Builds both images, boots the stack with an API_KEY, and exercises auth,
# SSRF protection, a real screenshot (container Chromium), and the redirect-SSRF
# guard — then tears everything down and prints a pass/fail tally.
#
# Usage (from anywhere):  ./scripts/verify.sh
# Requires: a running Docker daemon with the compose plugin.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO" || { echo "cannot cd to repo root: $REPO"; exit 1; }

# Pick compose command (v2 plugin preferred, fall back to legacy binary)
if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
else echo "docker compose not available — is the Docker daemon running?"; exit 1; fi
echo "Using: $COMPOSE   (repo: $REPO)"

# Generate a throwaway API key for the run
if command -v openssl >/dev/null 2>&1; then API_KEY="$(openssl rand -hex 16)"
else API_KEY="verify-$(date +%s)"; fi
export API_KEY
echo "API_KEY=$API_KEY"

# Always tear the stack down on exit (success, failure, or Ctrl-C)
trap '$COMPOSE down >/dev/null 2>&1 || true' EXIT

pass=0; fail=0
check() { # name expected actual
  if [ "$2" = "$3" ]; then echo "  OK  $1 (got $3)"; pass=$((pass+1));
  else echo "  XX  $1 (expected $2, got $3)"; fail=$((fail+1)); fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
S=http://localhost:5001
R=http://localhost:5002

echo "== build =="
$COMPOSE build || { echo "BUILD FAILED"; exit 1; }

echo "== up =="
$COMPOSE up -d || { echo "UP FAILED"; exit 1; }

echo "== wait for health (up to 90s) =="
for i in $(seq 1 45); do
  sh=$(code $S/stats); rh=$(code $R/health)
  echo "  t+$((i*2))s scraper/stats=$sh screener/health=$rh"
  [ "$sh" = "200" ] && [ "$rh" = "200" ] && break
  sleep 2
done

echo "== scraper (:5001) =="
check "stats open (no key)"        200 "$(code $S/stats)"
check "extract no key -> 401"      401 "$(code -X POST $S/extract -H 'Content-Type: application/json' --data '{"url":"https://example.com"}')"
check "extract wrong key -> 401"   401 "$(code -X POST $S/extract -H 'Content-Type: application/json' -H "X-API-Key: nope" --data '{"url":"https://example.com"}')"
check "extract metadata IP -> 400" 400 "$(code -X POST $S/extract -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" --data '{"url":"http://169.254.169.254/latest/meta-data/"}')"
sleep 3  # avoid the per-IP rate limit (default 2s) between requests from one client
check "extract file:// -> 400"     400 "$(code -X POST $S/extract -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" --data '{"url":"file:///etc/passwd"}')"
sleep 3
check "extract real URL -> 200"    200 "$(code -X POST $S/extract -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" --data '{"url":"https://example.com"}')"

echo "== screener (:5002) =="
check "health open (no key)"          200 "$(code $R/health)"
check "screenshot no key -> 401"      401 "$(code -X POST $R/screenshot -H 'Content-Type: application/json' --data '{"url":"https://example.com"}')"
check "screenshot metadata IP -> 400" 400 "$(code -X POST $R/screenshot -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" --data '{"url":"http://169.254.169.254/"}')"
check "screenshot file:// -> 400"     400 "$(code -X POST $R/screenshot -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" --data '{"url":"file:///etc/passwd"}')"

echo "== screener real screenshot (Chromium path) =="
SHOT=$(curl -s -X POST $R/screenshot -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" \
  --data '{"url":"https://example.com","format":"jpeg","quality":80}')
echo "  response: $SHOT"
if echo "$SHOT" | grep -qE '"success":\s*true'; then echo "  OK  screenshot success=true"; pass=$((pass+1));
else echo "  XX  screenshot did not succeed"; fail=$((fail+1)); fi

echo "== redirect-SSRF (#1): public URL that 302s the browser to an internal IP must be blocked =="
# Uses a public redirector that WILL 302 to an internal target. Skipped if unreachable.
REDIR="https://nghttp2.org/httpbin/redirect-to?url=http://169.254.169.254/&status_code=302"
if curl -s -o /dev/null --max-time 15 "$REDIR"; then
  RS=$(curl -s -X POST $R/screenshot -H 'Content-Type: application/json' -H "X-API-Key: $API_KEY" \
    --data "{\"url\":\"$REDIR\"}")
  echo "  response: $RS"
  if echo "$RS" | grep -qE '"success":\s*true'; then
    echo "  XX  redirect to internal produced a screenshot — SSRF NOT blocked"; fail=$((fail+1))
  else
    echo "  OK  redirect to internal was blocked (no successful screenshot)"; pass=$((pass+1))
  fi
else
  echo "  -- SKIP: redirector $REDIR unreachable (network); redirect-SSRF test not run"
fi

echo "== recent screener logs =="
$COMPOSE logs --tail=20 screener

echo "======================================"
echo "RESULT: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
