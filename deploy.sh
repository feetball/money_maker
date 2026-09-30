#!/usr/bin/env bash
# kalshibot deploy/launch script (Docker). PAPER TRADING ONLY.
#
#   ./deploy.sh up          build (if needed) and start in the background, wait until healthy
#   ./deploy.sh update      rebuild the image from the current code and recreate the container
#   ./deploy.sh down        stop and remove the container (data/ and config.yaml are kept)
#   ./deploy.sh restart     restart the running container
#   ./deploy.sh status      container state + engine status
#   ./deploy.sh logs        follow logs (Ctrl-C to stop following)
#   ./deploy.sh backtest …  run a backtest in a one-off container, e.g.
#                           ./deploy.sh backtest --strategy btc15m_favorite
#   ./deploy.sh shell       open a shell in the running container
#
# Options (env, or persistently in ./.env): KALSHIBOT_PORT (default 8765),
# KALSHIBOT_BIND (default 127.0.0.1; 0.0.0.0 = reachable from the network - there is
# no authentication, so only on a trusted network).
set -euo pipefail

cd "$(dirname "$0")"
# .env holds persistent settings (docker compose reads it too); explicit env vars win.
if [[ -f .env ]]; then
    while IFS='=' read -r key val; do
        [[ "$key" =~ ^KALSHIBOT_(PORT|BIND)$ ]] && [[ -z "${!key:-}" ]] && export "$key=$val"
    done < <(grep -E '^[A-Z_]+=' .env)
fi
PORT="${KALSHIBOT_PORT:-8765}"
BIND="${KALSHIBOT_BIND:-127.0.0.1}"
export KALSHIBOT_PORT="$PORT" KALSHIBOT_BIND="$BIND"
SERVICE=kalshibot

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

compose() { docker compose "$@"; }

preflight() {
    command -v docker >/dev/null || die "docker is not installed"
    docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you in the docker group?)"
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"

    # Persistent state lives on the host so it survives rebuilds.
    mkdir -p data
    if [[ ! -f config.yaml ]]; then
        cp config.example.yaml config.yaml
        say "created config.yaml from config.example.yaml"
    fi

    # Free disk check (the image build needs ~1-2 GB of scratch space).
    local free_gb
    free_gb=$(df -Pk . | awk 'NR==2 {printf "%.1f", $4/1048576}')
    if awk "BEGIN {exit !($free_gb < 2)}"; then
        warn "only ${free_gb} GB free on this disk; the build may fail"
    fi
}

native_pids() {
    # `kalshibot serve` processes started outside Docker. pgrep also sees the server
    # running inside our own container, so skip anything in a container cgroup.
    local p
    for p in $(pgrep -f "kalshibot serve" 2>/dev/null); do
        grep -qE 'docker|containerd|kubepods|libpod' "/proc/$p/cgroup" 2>/dev/null || echo "$p"
    done
}

native_server_running() {
    # A native server holds the same paper account (data/) and usually the same port.
    # Only one writer may own an account.
    [[ -n "$(native_pids)" ]]
}

container_running() {
    [[ -n "$(compose ps -q --status running "$SERVICE" 2>/dev/null)" ]]
}

check_conflicts() {
    if native_server_running; then
        die "a native 'kalshibot serve' is running (pid $(native_pids | tr '\n' ' ')).
       Stop it first (Ctrl-C in its terminal, or: pkill -INT -f 'kalshibot serve'),
       then re-run ./deploy.sh up. The container uses the same paper account in data/."
    fi
    if ! container_running && (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -qE "[:.]${PORT}[[:space:]]"; then
        die "port ${PORT} is already in use. Pick another: KALSHIBOT_PORT=8766 ./deploy.sh up"
    fi
}

wait_healthy() {
    say "waiting for the dashboard on http://${BIND}:${PORT} ..."
    local url="http://127.0.0.1:${PORT}/api/status"
    for _ in $(seq 1 60); do
        if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
            say "kalshibot is up: http://${BIND/0.0.0.0/localhost}:${PORT}  (paper trading)"
            status_line
            return 0
        fi
        if ! container_running; then
            compose logs --tail 40 "$SERVICE" >&2 || true
            die "the container exited during startup (logs above)"
        fi
        sleep 2
    done
    compose logs --tail 40 "$SERVICE" >&2 || true
    die "no response from the API after 120 s (logs above)"
}

status_line() {
    curl -fsS -m 5 "http://127.0.0.1:${PORT}/api/status" 2>/dev/null | python3 -c '
import json, sys
try:
    e = json.load(sys.stdin)["engine"]
except Exception:
    sys.exit("  engine status unavailable")
print("  engine running=%s ticks=%s markets=%s kill_switch=%s" % (
    e.get("running"), e.get("tick_count"), e.get("universe_size"), e.get("kill_switch")))
print("  strategies: %s" % (", ".join(e.get("strategies_enabled") or []) or "none"))
if e.get("last_error"):
    print("  last error: %s" % e["last_error"])
' || true
}

cmd="${1:-up}"
shift || true

case "$cmd" in
    up|start|deploy)
        preflight
        check_conflicts
        say "building image (first build takes a few minutes) ..."
        compose build
        compose up -d
        wait_healthy
        ;;
    update|rebuild)
        preflight
        check_conflicts
        say "rebuilding from current code ..."
        compose build --pull
        compose up -d --force-recreate
        wait_healthy
        docker image prune -f >/dev/null 2>&1 || true
        ;;
    down|stop)
        compose down
        say "stopped (paper account kept in data/)"
        ;;
    restart)
        compose restart "$SERVICE"
        wait_healthy
        ;;
    status|ps)
        compose ps
        container_running && status_line || true
        ;;
    logs)
        compose logs -f --tail 200 "$SERVICE"
        ;;
    backtest)
        preflight
        # One-off container; no ports, reads research/ and prints the results.
        compose run --rm --no-deps "$SERVICE" kalshibot backtest "$@"
        ;;
    shell)
        compose exec "$SERVICE" bash
        ;;
    *)
        sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
