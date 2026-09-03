#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ENZO bootstrap — make the workspace runnable in one command.
#
#   bash bootstrap.sh            install + verify
#   bash bootstrap.sh --check    verify only, change nothing (exit 0/1)
#
# Designed for an agent-supervised workspace (OpenClaw): every step prints a
# machine-readable OK/FAIL line and the script exits non-zero if the bot would
# not be able to run correctly. Nothing here touches your funds or config.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

FAIL=0
ok()   { printf '  [ OK ]   %s\n' "$1"; }
bad()  { printf '  [FAIL]   %s\n' "$1"; FAIL=1; }
warn() { printf '  [WARN]   %s\n' "$1"; }
hdr()  { printf '\n== %s\n' "$1"; }

PY="${PYTHON:-python3}"

hdr "Python interpreter"
if ! command -v "$PY" >/dev/null 2>&1; then
  bad "python3 not found on PATH"
  printf '\nBootstrap failed.\n'; exit 1
fi
PYVER="$("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null)"
PYMAJ="$("$PY" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null)"
if [[ "${PYMAJ:-0}" -ge 310 ]]; then ok "python $PYVER (>= 3.10 required)"; else bad "python $PYVER is too old (>= 3.10 required)"; fi

hdr "Python packages"
install_pkgs() {
  local pkgs="$1"
  if [[ $CHECK_ONLY -eq 1 ]]; then
    bad "missing: $pkgs  (re-run without --check to install)"
    return 1
  fi
  printf '  ....     installing %s\n' "$pkgs"
  # Try the plain install first; fall back to --user, then --break-system-packages
  # (PEP 668 "externally managed" distros such as Debian 12+/Ubuntu 24.04).
  if "$PY" -m pip install --quiet --disable-pip-version-check $pkgs 2>/dev/null; then return 0; fi
  if "$PY" -m pip install --quiet --disable-pip-version-check --user $pkgs 2>/dev/null; then return 0; fi
  if "$PY" -m pip install --quiet --disable-pip-version-check --break-system-packages $pkgs 2>/dev/null; then return 0; fi
  bad "pip could not install: $pkgs"
  return 1
}

MISSING=""
for spec in "yaml:PyYAML" "websockets:websockets"; do
  mod="${spec%%:*}"; pkg="${spec##*:}"
  if "$PY" -c "import $mod" 2>/dev/null; then
    ver="$("$PY" -c "import $mod;print(getattr($mod,'__version__','?'))" 2>/dev/null)"
    ok "$pkg $ver"
  else
    MISSING="$MISSING $pkg"
  fi
done
if [[ -n "$MISSING" ]]; then install_pkgs "$MISSING" || true; fi
# re-verify after install
for spec in "yaml:PyYAML" "websockets:websockets"; do
  mod="${spec%%:*}"; pkg="${spec##*:}"
  "$PY" -c "import $mod" 2>/dev/null && ok "$pkg importable" || bad "$pkg still not importable"
done

hdr "Configuration"
if [[ -f config/enzo-config.yaml ]]; then ok "config/enzo-config.yaml present"; else bad "config/enzo-config.yaml missing"; fi
if [[ -f config/enzo-secrets.json ]]; then ok "config/enzo-secrets.json present"; else warn "config/enzo-secrets.json missing (Telegram notifications disabled)"; fi
"$PY" - <<'PYEOF'
import sys
sys.path.insert(0, ".")
try:
    from enzo.core import config
except Exception as e:
    print(f"  [FAIL]   cannot import enzo.core.config: {e}"); sys.exit(1)
try:
    cfg = config.load_config()
except Exception as e:
    print(f"  [FAIL]   config load: {e}"); sys.exit(1)
probs = config.validate_config(cfg)
mode = "PAPER" if cfg.get("paper_mode", True) else "LIVE"
print(f"  [ OK ]   config parsed — {len(cfg)} sections, mode={mode}")
for p in probs:
    print(f"  [WARN]   config: {p}")
PYEOF
[[ $? -ne 0 ]] && FAIL=1

hdr "External CLIs (only needed for LIVE trading / market data)"
for bin in mp moonpay gmgn-cli; do
  p="$(command -v "$bin" 2>/dev/null || true)"
  if [[ -n "$p" ]]; then ok "$bin -> $p"; else warn "$bin not on PATH"; fi
done
for cand in "$HOME/.npm-global/bin" "$HOME/.local/bin" /usr/local/bin; do
  for bin in mp moonpay gmgn-cli; do
    [[ -x "$cand/$bin" ]] && ok "$bin found at $cand/$bin (not on PATH — ENZO will still locate it)"
  done
done

hdr "Node.js (required by the MoonPay CLI)"
if command -v node >/dev/null 2>&1; then ok "node $(node --version 2>/dev/null)"; else warn "node not found — 'npm i -g @moonpay/cli' will not work"; fi

hdr "Writable paths"
for d in data data/logs data/run config; do
  if [[ -w "$d" ]]; then ok "$d writable"; else bad "$d NOT writable"; fi
done

hdr "Import smoke test"
if "$PY" -c "
import sys; sys.path.insert(0,'.')
from enzo.core import config, db, engine, learn, audit, log, cache
from enzo.execution import portfolio, exit_monitor, executor
from enzo.ui import dashboard, serve, botctl, notify
from enzo.providers import gmgn, pump
from enzo.analyzers import analyze, dev, wallet, security, market_structure
" 2>/dev/null; then ok "all modules import cleanly"; else bad "module import failed — run: $PY -c 'import enzo.core.engine' to see the traceback"; fi

hdr "Dashboard generator"
if "$PY" -c "
import sys; sys.path.insert(0,'.')
from enzo.core import db; db.init_db()
from enzo.ui import dashboard; dashboard.generate()
" 2>/tmp/enzo-bootstrap-dash.err; then ok "dashboard.generate() succeeded"; else bad "dashboard.generate() failed:"; sed 's/^/           /' /tmp/enzo-bootstrap-dash.err | tail -5; fi

printf '\n'
if [[ $FAIL -eq 0 ]]; then
  echo "== Bootstrap OK — ENZO is ready to run."
  echo "   Next:  $PY enzo.py doctor      (full 25-point preflight)"
  echo "          $PY enzo.py start       (supervised: dashboard + engine + telegram)"
else
  echo "== Bootstrap found problems (see [FAIL] lines above). Fix them before starting the bot."
fi
exit $FAIL
