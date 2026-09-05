"""Shared test bootstrap: locate the bundled mock MoonPay CLI.

The mock ships inside the repo (tests/mockbin/) so the suites run anywhere with
zero setup — no /tmp paths, no network, no real wallet. It reproduces
@moonpay/cli 1.96.0's commander semantics: global --json, per-command option
tables, the auto-added --explanation, and "error: unknown option" + exit 1.

Resolution order:
  1. ENZO_MOCK_BIN_DIR  (explicit override)
  2. tests/mockbin/     (bundled, the normal case)
  3. an `mp` already on PATH
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLED = os.path.join(HERE, "mockbin")


def mock_bin_dir():
    override = os.environ.get("ENZO_MOCK_BIN_DIR")
    if override and os.path.isdir(override):
        return override
    if os.path.isdir(BUNDLED) and (os.path.exists(os.path.join(BUNDLED, "mp"))
                                   or os.path.exists(os.path.join(BUNDLED, "moonpay"))):
        return BUNDLED
    return None


def install_mock_on_path():
    """Put the mock CLI on PATH. Returns the directory used, or None."""
    d = mock_bin_dir()
    if d:
        if d not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        return d
    if shutil.which("mp") or shutil.which("moonpay"):
        return "(already on PATH)"
    return None


def isolate_home(prefix="enzo-test-", copy_config=True):
    """Point ENZO_HOME at a throwaway workspace. Call BEFORE importing enzo.

    Why this exists
    ---------------
    `enzo.core.config` resolves every state path at import time — WORKSPACE_ROOT,
    DATA_DIR, PORTFOLIO_DB_PATH, the capital cache under data/run, the log file.
    A suite that imports enzo first and sets ENZO_HOME later therefore writes
    into the LIVE workspace no matter what it does afterwards. Eight suites did
    exactly that, so running the tests created or modified data/enzo.db,
    data/logs/enzo.log, data/enzo-trade-gate.json, the dashboard HTML and —
    worst — data/run/enzo-capital.json holding the mock wallet's figures.

    That last file is a money-path hazard, not litter: when the real wallet read
    fails, LIVE sizing trusts the cached snapshot for
    `execution.capital_sync_grace_sec` (300s). A test run before the first engine
    cycle in a fresh deployment could therefore hand position sizing a balance
    that does not exist, and `enzoctl doctor` would report it as a recent
    reading. Secrets are deliberately NOT copied into the sandbox.

    Returns the sandbox path; cleanup is registered with atexit.
    """
    import atexit
    import tempfile

    home = tempfile.mkdtemp(prefix=prefix)
    for sub in ("config", os.path.join("data", "logs"), os.path.join("data", "run")):
        os.makedirs(os.path.join(home, sub), exist_ok=True)
    if copy_config:
        repo = os.path.dirname(HERE)
        for name in ("enzo-config.yaml", "enzo-watchlist.json",
                     "enzo-decision-schema.json"):
            src = os.path.join(repo, "config", name)
            if os.path.exists(src):
                shutil.copy(src, os.path.join(home, "config", name))
    os.environ["ENZO_HOME"] = home
    for mod in [m for m in list(sys.modules) if m == "enzo" or m.startswith("enzo.")]:
        del sys.modules[mod]
    atexit.register(shutil.rmtree, home, True)
    return home
