#!/usr/bin/env bash
# OnePage: deployment check. Run on the target machine AFTER startup.
#
#   ./check.sh                          # local checks only
#   ./check.sh https://onepage.example.com  # plus a check through the reverse proxy
#
# If your user is not in the docker group, run it with sudo, otherwise
# checks 1 and 4 cannot find anything out.
#
# The script changes nothing, it only reads.
#
# Principle: [BAD] means "the deployment is broken". If the script could not find
# something out (no access to the daemon, a name does not resolve), that is [~~], not [BAD].
# An acceptance check that shouts about its own blindness is useless: by
# the third false [BAD] people stop reading the real one too.
set -uo pipefail

URL="${1:-}"
PORT=8743
ok=0; bad=0; warn=0
say()  { printf '  [%s] %s\n' "$1" "$2"; }
pass() { say " ok" "$1"; ok=$((ok+1)); }
fail() { say "BAD" "$1"; bad=$((bad+1)); }
skip() { say " ~~" "$1"; warn=$((warn+1)); }

CID=""
DOCKER_OK=0

echo "== 1. Container =="
if ! command -v docker >/dev/null 2>&1; then
  skip "docker not found in PATH, checks 1 and 4 skipped"
elif ! docker info >/dev/null 2>&1; then
  skip "no access to the Docker daemon, checks 1 and 4 skipped"
  echo "        repeat with sudo: sudo $0 ${URL:-}"
else
  DOCKER_OK=1
  CID=$(docker compose ps -q onepage 2>/dev/null | head -1)
  if [ -z "$CID" ]; then
    fail "onepage container not found (was compose started from this directory?)"
  else
    state=$(docker inspect --format '{{.State.Status}}' "$CID" 2>/dev/null || echo '')
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no healthcheck{{end}}' \
               "$CID" 2>/dev/null || echo '-')
    [ "$state" = "running" ] && pass "container running" || fail "container not running (state: ${state:-none})"
    case "$health" in
      healthy)          pass "healthcheck: healthy" ;;
      starting)         skip "healthcheck: starting, wait ~15 s and repeat" ;;
      "no healthcheck") skip "healthcheck not configured" ;;
      *)                fail "healthcheck: $health" ;;
    esac
  fi
fi

echo "== 2. Application responds =="
# curl itself prints 000 on failure, so `|| echo 000` would give "000000".
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" 2>/dev/null; true)
[ "$code" = "200" ] && pass "/health -> 200" || fail "/health -> $code"

deep=$(curl -s "http://127.0.0.1:$PORT/health?deep=1" 2>/dev/null || echo '')
if echo "$deep" | grep -q '"status": *"ok"'; then
  pass "/health?deep=1 -> ok (SQLite readable, uploads directory writable)"
else
  fail "/health?deep=1 -> ${deep:-no answer}"
  echo "        this is the main check: the port is taken, but the instance cannot serve"
fi

echo "== 3. Network mode =="
# About +34% p50 / +41% p99 (ADR-2): this was measured on the path through the PUBLISHED
# port, where docker-proxy sits. A proxy container (Traefik and the like) reaches the
# application over the Docker network via veth, where this hop does not exist at all, and that path
# we have never measured. So the presence of docker-proxy is not in itself
# a defect; it is one only where the working traffic really goes through
# the published port.
netmode=""; nets=""
if [ -n "$CID" ]; then
  netmode=$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$CID" 2>/dev/null || echo '')
  nets=$(docker inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CID" 2>/dev/null || echo '')
fi

if [ "$netmode" = "host" ]; then
  pass "host networking enabled, no docker-proxy in the path"
elif ! command -v ss >/dev/null 2>&1; then
  skip "ss not found, network mode not determined"
else
  listener=$(ss -ltnp 2>/dev/null | grep ":$PORT " || true)
  # Own network = something reaches the container from inside Docker (usually a proxy).
  own_net=$(echo "$nets" | tr ' ' '\n' | grep -vE '^(bridge|host|none|)$' | head -1)
  if [ -z "$listener" ] && [ -n "$own_net" ]; then
    pass "port not published; container on network \"$own_net\", the proxy goes over veth"
  elif [ -z "$listener" ]; then
    fail "nobody listens on $PORT and the container is not on its own Docker network"
  elif echo "$listener" | grep -q docker-proxy; then
    if [ -n "$own_net" ]; then
      skip "port $PORT is published (docker-proxy), but the container is on network \"$own_net\""
      echo "        if the working traffic goes through a proxy over this network, the ADR-2 penalty"
      echo "        does not affect you; it was measured on the path through the published port."
      echo "        Remove the publication as a redundant listener on the host, not for the µs."
    else
      fail "the port is held by docker-proxy and there is no own Docker network: all traffic goes through it"
      echo "        that is exactly the +34% p50 / +41% p99: enable network_mode: host"
    fi
  else
    pass "the port is held by the application process, no docker-proxy"
  fi
fi

echo "== 4. Access log disabled =="
if [ "$DOCKER_OK" -eq 0 ] || [ -z "$CID" ]; then
  skip "no access to the container logs, check skipped"
else
  lines=$(docker compose logs --tail 200 onepage 2>/dev/null | grep -c '"GET /\|"POST /' || true)
  [ "${lines:-0}" -eq 0 ] \
    && pass "requests are not logged (expected: OP_ACCESS_LOG=false)" \
    || fail "the log has $lines request lines: the access log is on, that is ~91-96 µs per request"
fi

echo "== 5. Data in place =="
if [ -f ./data/onepage.db ]; then
  pass "./data/onepage.db exists ($(du -h ./data/onepage.db | cut -f1))"
else
  fail "./data/onepage.db missing: the database may have ended up inside the container"
fi

if [ -n "$URL" ]; then
  echo "== 6. Through the reverse proxy =="
  # A server often cannot resolve its own external name (split DNS) and almost always
  # cannot do hairpin NAT to its own external address. That is NOT a proxy failure, and
  # it differs from one by curl's exit code, not by HTTP status.
  body=$(curl -s -o /dev/null -w '%{http_code} %{scheme}' --max-time 10 "$URL/health" 2>/dev/null)
  rc=$?
  code=$(echo "$body" | cut -d' ' -f1)
  scheme=$(echo "$body" | cut -d' ' -f2)
  host=${URL#*://}; host=${host%%/*}; host=${host%%:*}
  case "$rc" in
    0)
      [ "$code" = "200" ] && pass "$URL/health -> 200" || fail "$URL/health -> $code"
      [ "$scheme" = "https" ] && pass "https from outside" \
        || fail "not https from outside: the cookie has no Secure flag, the report iframe will break"
      ;;
    6)
      skip "$host does not resolve FROM THE SERVER ITSELF, check not applicable"
      echo "        common with split DNS. Check from your own machine:"
      echo "          curl -sI $URL/health | head -3"
      ;;
    7|28)
      skip "no connection from the server to $host (no hairpin NAT?), not applicable"
      echo "        check from your own machine:  curl -sI $URL/health | head -3"
      ;;
    *)
      skip "curl exited with code $rc, result cannot be interpreted"
      ;;
  esac

  # Cross-origin guard check. It fires BEFORE authentication, so
  # its state is visible anonymously and without side effects: the request never reaches
  # the handler, nothing is toggled.
  #
  # Read it like this: 403 on a foreign origin = protection on; NOT 403 on our own = this
  # address is on the allow-list. 403 on our own is exactly the refusal where the site
  # opens but nothing gets saved (see docs/DEPLOY_SECURITY_20260726.md).
  if [ "$rc" = "0" ]; then
    foreign=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
              -H 'Origin: https://not-onepage.invalid' "$URL/api/admin/perf/toggle" 2>/dev/null)
    own=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -X POST \
          -H "Origin: $URL" "$URL/api/admin/perf/toggle" 2>/dev/null)
    case "$foreign:$own" in
      403:403) fail "the origin guard rejects OUR OWN address: writes are broken for everyone"
               echo "        OP_SAML_SP_BASE_URL does not match $URL."
               echo "        Add the address to OP_TRUSTED_ORIGINS, or temporarily"
               echo "        lift the protection: OP_ORIGIN_GUARD=false + compose up -d" ;;
      403:*)   pass "origin guard on, $URL allowed" ;;
      *:*)     skip "the origin guard does not answer 403 to a foreign origin (disabled?)"
               echo "        that is intended with OP_ORIGIN_GUARD=false; otherwise check the build" ;;
    esac
  fi
fi

echo
printf 'Total: %d ok, %d problems' "$ok" "$bad"
[ "$warn" -gt 0 ] && printf ', %d not checked' "$warn"
echo
if [ "$bad" -eq 0 ]; then
  echo
  echo "The automated checks are happy. What remains is manual, in exactly this order:"
  echo "  1. sign in via SSO as a real user"
  echo "  2. the report list is in place"
  echo "  3. open a report: preview and render"
  echo "  4. open an OLD link that was sent out, /s/{token}"
fi
exit $(( bad > 0 ))
