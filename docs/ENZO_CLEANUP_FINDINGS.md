# ENZO_CLEANUP_FINDINGS.md

**Generated:** 2026-08-12 17:36 GMT+1
**Purpose:** Document issues found during audit that are **NOT** part of cleanup scope. These must be addressed separately after cleanup.

---

## F-01: `check_imports.py` Reports Import Failures (Expected, Not a Bug)

**Findings:** `check_imports.py` walks entire workspace and tries to import every `.py` file. It reports 28 failures.

**Root Cause:** The script imports **everything** including:
- `src/` tree (requires `aiohttp`, `pydantic` — not installed)
- `ENZO_GMGN/` (relative imports broken when run from root; expects `from analyzer import ...` but `ENZO_GMGN` not in sys.path)
- `agent/`, `.agents/` (skills with relative imports)
- `.venv/` internal modules

**Evidence:** All 24 core `enzo_*.py` modules import cleanly when tested individually (verified 2026-08-12 17:36).

**Severity:** LOW — `check_imports.py` is a dev tool with overly broad scope; runtime imports work.

**Recommendation:** Fix `check_imports.py` to exclude `src/`, `ENZO_GMGN/`, `agent/`, `.agents/`, `.venv/`, `__pycache__/`, `.trash-gmgn-migration/`. Or document that it's expected to show failures for non-runtime code.

---

## F-02: `config.yaml` Uses Different Schema Than Active Config

**Findings:** Two config files exist:
- `enzo-config.yaml` — **ACTIVE**, consumed by `enzo_config.load_config()`
- `config.yaml` — **LEGACY**, old schema (email notifications, different keys, `src/`-centric)

**Conflict:** Keys differ:
- `minimum_liquidity: 500000` vs `risk_management.risk_per_trade: 2.5`
- `notification_channel: "email"` vs `notifications.send_decision_notifications: true`
- `weight_*` vs `scoring_weights.*`

**Evidence:** No `enzo_*.py` imports `config.yaml`. Only `main.py` and `src/` reference old keys.

**Severity:** MEDIUM — potential confusion if someone edits wrong file.

**Recommendation:** Archive `config.yaml` (C-01). Document that `enzo-config.yaml` is the only active config.

---

## F-03: `enzo_botctl.py status` Hangs / Blocks

**Findings:** `python3 enzo_botctl.py status` hangs indefinitely (tested with 3s timeout).

**Context:** `enzo_botctl.py` process (pid 878) is running. The `status` subcommand may be waiting on a lock, pipe, or subprocess.

**Evidence:** `pgrep -f enzo_botctl` shows process alive.

**Severity:** MEDIUM — blocks CLI usability.

**Recommendation:** Debug `enzo_botctl.py status` separately (add timeout, check stdin/stdout handling). Not a cleanup issue.

---

## F-04: `enzo_serve.py /health` Endpoint Returns 404

**Findings:** Dashboard server (port 8077) serves `enzo-dashboard.html` correctly but `/health` returns 404.

**Evidence:** `enzo_serve.py` only implements `/api/prices` and static file serving for `enzo-dashboard.html`. No `/health` route.

**Severity:** LOW — cosmetic; server works.

**Recommendation:** Add `/health` endpoint to `enzo_serve.py` if needed for monitoring.

---

## F-05: Duplicate Skill Directories (`.agents/` + `agent/` + `skills/`)

**Findings:** Three directories contain GMGN skills:
- `skills/` — OpenClaw registered skills (active)
- `.agents/skills/` — duplicate of `agent/skills/`
- `agent/skills/` — duplicate

**Evidence:** Both `.agents/` and `agent/` have identical `gmgn-holder-analysis/analyze.py` which fails import (IndexError).

**Severity:** LOW — disk space only; no runtime impact.

**Recommendation:** Archive `.agents/` and `agent/` (D-03, D-04). Keep `skills/` as canonical.

---

## F-06: `memecoin_trading_specialist/` AND `memecoin-trading-specialist/` Both Exist

**Findings:** Two directories with same purpose (underscore vs hyphen), both only contain empty `__init__.py` stubs.

**Severity:** LOW — confusion only.

**Recommendation:** Archive both (C-03, D-06). ENZO runtime uses `enzo_*.py` not this package.

---

## F-07: Large Log Files (15+ MB Each) Not Rotated

**Findings:**
- `enzo-pump.log`: 15.6 MB
- `enzo-audit.jsonl`: 15.4 MB
- `enzo-log.jsonl`: 14.2 MB

**Evidence:** No log rotation config in `enzo-config.yaml` or code.

**Severity:** MEDIUM — disk growth unbounded.

**Recommendation:** Add log rotation (size/time based) to `enzo_log.py` or `enzo-config.yaml`. Not a cleanup issue.

---

## F-08: `.trash-gmgn-migration/` Already Exists (Previous Cleanup)

**Findings:** Directory `.trash-gmgn-migration/` contains:
- `enzo_fetch.py` (3.7 KB) — old Helius fetch module
- `enzo_ws.py` (4.0 KB) — old WebSocket client

**Context:** This was created during 2026-08-05 GMGN migration. It's already quarantined.

**Severity:** INFO — confirms cleanup pattern works.

**Recommendation:** Move into `.trash-enzo-cleanup/migration/` during this cleanup for consistency.

---

## F-09: `requirements.txt` May Be Incomplete/Outdated

**Findings:** `requirements.txt` exists (232 bytes) but:
- `aiohttp` not listed (needed by `src/` but not by `enzo_*.py`)
- `pydantic` not listed (needed by `src/`)
- `PyYAML` likely needed (used by `enzo_config`, `enzo_security`, `enzo_botctl`)
- `gmgn-cli` is external binary, not pip

**Evidence:** Current runtime works without `aiohttp`/`pydantic` because `enzo_*.py` uses stdlib + `gmgn-cli` subprocess.

**Severity:** LOW — `requirements.txt` appears to be for `src/` blueprint, not ENZO runtime.

**Recommendation:** Create `requirements-enzo.txt` for actual runtime deps (PyYAML, etc.) or document that ENZO runtime uses stdlib + external `gmgn-cli`.

---

## F-10: `enzo_pricefeed.py` Uses Helius WS But No WS URL Config

**Findings:** `enzo_pricefeed.py` mentions "Helius WS" and "accountSubscribe" but no Helius API key or WS URL in config/secrets.

**Evidence:** `enzo-secrets.json` only has Telegram. `enzo-config.yaml` has no Helius config. MEMORY.md mentions "Helius WS verified live" but this may be stale from pre-GMGN migration.

**Severity:** MEDIUM — if `enzo_pricefeed` still needs Helius WS, it's misconfigured.

**Recommendation:** Verify if `enzo_pricefeed.py` actually uses Helius WS in current GMGN-only architecture, or if it's fully migrated to GMGN/pump-adv. If WS path is dead, clean it up (separate task).

---

## F-11: `src/tests/` Exist But Are Empty Stubs

**Findings:** `src/tests/` directory tree exists with `__init__.py` files but test files are 0-byte.

**Severity:** LOW — no real tests to run.

**Recommendation:** Archive with `src/` (C-02).

---

## F-12: `openclaw-workspace-state.json` & `skills-lock.json` Are OpenClaw Internals

**Findings:** These are not ENZO files; they belong to OpenClaw runtime.

**Severity:** INFO — correctly classified as F (DO NOT TOUCH).

---

## F-13: No Automated Test Suite for ENZO Runtime

**Findings:** Only `enzo_tests_gapfill.py` exists (unit tests). No integration/smoke tests for:
- GMGN connectivity
- Serve endpoint
- Portfolio state persistence
- Exit monitor loop
- Botctl lifecycle

**Evidence:** `tests/` is empty stubs; `src/tests/` empty.

**Severity:** HIGH — cleanup verification step requires manual testing.

**Recommendation:** Build test suite AFTER cleanup. Not part of this cleanup.

---

## F-14: `enzo_gmgn.py` Global Ban State File Path Is Hardcoded

**Findings:** `enzo-gmgn-ban.json` path appears hardcoded in `enzo_gmgn.py`.

**Evidence:** File exists at root, shared across processes. Works but not configurable.

**Severity:** LOW — works correctly (verified: ban_status() used by serve.py).

**Recommendation:** Make configurable if needed; not a cleanup issue.

---

## F-15: `enzo-cache.json` Is 793 KB — GMGN Response Cache

**Findings:** Large cache file, regenerated by `enzo_gmgn.py` TTL logic.

**Evidence:** `enzo_cache.py` provides generic TTL cache; `enzo_gmgn.py` uses it for market/info/security/holders/curve caches.

**Severity:** INFO — expected.

**Recommendation:** Keep as runtime cache (archivable, regenerable).

---

## Summary of Actionable Findings (Post-Cleanup)

| ID | Issue | Priority | Effort |
|----|-------|----------|--------|
| F-01 | Fix `check_imports.py` exclusions | Low | 10 min |
| F-02 | Archive `config.yaml`; document active config | Medium | 5 min |
| F-03 | Debug `enzo_botctl.py status` hang | Medium | 30 min |
| F-04 | Add `/health` to `enzo_serve.py` | Low | 10 min |
| F-07 | Add log rotation | Medium | 30 min |
| F-09 | Create accurate `requirements-enzo.txt` | Low | 10 min |
| F-10 | Verify Helius WS path in `enzo_pricefeed.py` | Medium | 20 min |
| F-13 | Build integration test suite | High | 2-4 hrs |

---

**Note:** None of these findings block the cleanup. They are documented for future work per the rule: *"If you discover an issue during audit, record it only in ENZO_CLEANUP_FINDINGS.md and do not fix it now."*