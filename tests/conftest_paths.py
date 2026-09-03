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
