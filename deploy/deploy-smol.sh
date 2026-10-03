#!/usr/bin/env bash
# Deploy the btc15m_favorite PAPER run (config.paper-run.yaml, see HANDOFF.md) with the repo's Docker
# setup, then check through the API that the running instance is what the run assumes. PAPER ONLY.
#
#   deploy/deploy-smol.sh            install the config, build + start (./deploy.sh up), wait for
#                                    /api/health, verify
#   deploy/deploy-smol.sh update     the same, rebuilding the image from the current code
#   deploy/deploy-smol.sh verify     only check the running instance (no changes)
#
# Run it on the host that runs the container (the repo root is found from the script's location).
# Port: KALSHIBOT_PORT (env or ./.env, default 8765), like deploy.sh.
#
# Why verify: values the dashboard saved to the database beat config.yaml (strategy toggles and
# params, PATCH /api/risk, and the account - starting balance and profit sweep - once it exists),
# and a bind-mounted config.yaml is only read at startup. A silently stale override would run a
# different experiment than the one in config.paper-run.yaml (for example a profit sweep that
# shrinks the fixed 50-lot to nothing), so the script fails loudly instead.
set -euo pipefail

cd "$(dirname "$0")/.."
SRC=config.paper-run.yaml
DST=config.yaml

# explicit env wins, then ./.env (the same keys deploy.sh reads)
if [[ -z "${KALSHIBOT_PORT:-}" && -f .env ]]; then
    KALSHIBOT_PORT="$(grep -E '^KALSHIBOT_PORT=' .env | tail -n1 | cut -d= -f2- || true)"
fi
PORT="${KALSHIBOT_PORT:-8765}"
BASE="http://127.0.0.1:${PORT}"
HEALTH_WAIT_S="${SMOL_HEALTH_WAIT_S:-240}"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

container_running() {
    [[ -n "$(docker compose ps -q --status running kalshibot 2>/dev/null)" ]]
}

install_config() {
    [[ -f "$SRC" ]] || die "$SRC not found (run from a checkout that has it)"
    CONFIG_CHANGED=0
    if [[ -f "$DST" ]] && cmp -s "$SRC" "$DST"; then
        say "$DST already is $SRC"
        return
    fi
    if [[ -f "$DST" ]]; then
        local bak="$DST.bak.$(date +%Y%m%dT%H%M%S)"
        cp -p "$DST" "$bak"
        say "kept the previous $DST as $bak"
    fi
    cp "$SRC" "$DST"  # cp onto the existing file keeps its inode: the container's bind mount sees it
    CONFIG_CHANGED=1
    say "installed $SRC as $DST"
}

wait_healthy() {
    say "waiting for ${BASE}/api/health (up to ${HEALTH_WAIT_S} s) ..."
    local deadline=$((SECONDS + HEALTH_WAIT_S)) last=""
    while ((SECONDS < deadline)); do
        if last="$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "${BASE}/api/health" 2>&1)" && [[ "$last" == 200 ]]; then
            say "healthy"
            return 0
        fi
        sleep 3
    done
    curl -sS -m 5 "${BASE}/api/health" >&2 || true
    echo >&2
    die "not healthy after ${HEALTH_WAIT_S} s (/api/health above; ./deploy.sh logs for the engine's)"
}

verify() {
    say "verifying the running instance at ${BASE}"
    python3 - "$BASE" <<'PY'
import json
import sys
import urllib.error
import urllib.request

base = sys.argv[1]
ONLY = "btc15m_favorite"
WANT_PARAMS = {"sizing": "fixed", "contracts": 50, "max_spot_age_s": 5}
WANT_BALANCE = 10000
JSON = "-H 'content-type: application/json'"


def get(path, *, any_status=False):
    """(status, json body). Anything but a 200 JSON answer ends the run, except where ``any_status``."""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        if any_status:
            try:
                return e.code, json.load(e)
            except ValueError:
                return e.code, {}
        sys.exit(f"FAIL  GET {base}{path} answered {e.code}: is this kalshibot (PORT / KALSHIBOT_PORT)?")
    except Exception as e:  # connection refused, timeout, not JSON, ...
        sys.exit(f"FAIL  cannot read {base}{path}: {type(e).__name__}: {e}")


problems = []


def check(ok, what, fix=""):
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        problems.append((what, fix))


code, health = get("/api/health", any_status=True)
check(code == 200, "GET /api/health is 200" + ("" if code == 200 else f" (got {code}: {health.get('detail')})"),
      "see ./deploy.sh logs; the engine must be running, Kalshi reachable and ticking")
if health.get("gated"):
    print(f"note  health is gated: {health['gated']}")

_, status = get("/api/status")
enabled = sorted((status.get("engine") or {}).get("strategies_enabled") or [])
check(enabled == [ONLY], f"only {ONLY} is enabled (enabled: {enabled or 'none'})",
      "; ".join(f"curl -X PATCH {base}/api/strategies/{n} {JSON} -d '{{\"enabled\": false}}'"
                for n in enabled if n != ONLY) or
      f"curl -X PATCH {base}/api/strategies/{ONLY} {JSON} -d '{{\"enabled\": true}}'")

_, strategies = get("/api/strategies")
by_name = {s["name"]: s for s in strategies}
btc = by_name.get(ONLY)
check(btc is not None, f"{ONLY} is registered")
if btc is not None:
    params = btc.get("params") or {}
    bad = {k: params.get(k) for k, v in WANT_PARAMS.items() if params.get(k) != v}
    check(not bad, "params took effect: " + ", ".join(f"{k}={params.get(k)}" for k in WANT_PARAMS) +
          ("" if not bad else f" (want {', '.join(f'{k}={WANT_PARAMS[k]}' for k in bad)})"),
          f"curl -X PATCH {base}/api/strategies/{ONLY} {JSON} -d '{json.dumps({'params': WANT_PARAMS})}'")
    dll = (btc.get("risk_limits") or {}).get("daily_loss_limit")
    check(not dll, f"no daily loss stop for {ONLY} (daily_loss_limit: {dll or 'off'})",
          f"set strategies.{ONLY}.daily_loss_limit: 0 in config.yaml and restart (the dashboard cannot change it)")

_, risk = get("/api/risk")
acct_dll = float((risk.get("limits") or {}).get("daily_loss_limit") or 0)
check(acct_dll == 0, f"no account daily loss stop (risk.daily_loss_limit: {acct_dll:g})",
      f"curl -X PATCH {base}/api/risk {JSON} -d '{{\"daily_loss_limit\": 0}}'")
check(not risk.get("kill_switch"), "kill switch is off" +
      (f" (on: {risk.get('kill_switch_reason')})" if risk.get("kill_switch") else ""),
      f"curl -X PATCH {base}/api/risk {JSON} -d '{{\"kill_switch\": false}}'")

_, acct = get("/api/account")
sweep_off = (not acct.get("profit_sweep_enabled")) or float(acct.get("profit_sweep_pct") or 0) == 0
check(sweep_off, "profit sweep is off (enabled: %s, pct: %s)" % (acct.get("profit_sweep_enabled"),
                                                                   acct.get("profit_sweep_pct")),
      f"curl -X PATCH {base}/api/account {JSON} -d '{{\"profit_sweep_enabled\": false, \"profit_sweep_pct\": 0}}'")
sb = float(acct.get("starting_balance") or 0)
check(sb == WANT_BALANCE, f"paper balance is ${WANT_BALANCE:,} (starting_balance: ${sb:,.2f})",
      f"an existing account keeps its balance: curl -X POST {base}/api/account/reset {JSON} "
      f"-d '{{\"starting_balance\": {WANT_BALANCE}}}'  (wipes the paper account, only before the run starts)")

print()
if problems:
    print(f"NOT READY: {len(problems)} check(s) failed. Fixes:")
    for what, fix in problems:
        print(f"  - {what}\n      {fix}")
    sys.exit(1)
print("READY: the instance matches config.paper-run.yaml.")
PY
}

cmd="${1:-up}"
case "$cmd" in
    up|start|deploy|update|rebuild)
        command -v docker >/dev/null || die "docker is not installed"
        was_running=0
        container_running && was_running=1
        install_config
        if [[ "$cmd" == update || "$cmd" == rebuild ]]; then
            ./deploy.sh update
        else
            ./deploy.sh up
            # `up` leaves a running container alone, and config.yaml is only read at startup
            if ((was_running && CONFIG_CHANGED)); then
                say "the config changed under a running container: restarting it"
                ./deploy.sh restart
            fi
        fi
        wait_healthy
        verify
        ;;
    verify|check)
        verify  # the health check is one of the checks: no waiting, no changes
        ;;
    *)
        sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
