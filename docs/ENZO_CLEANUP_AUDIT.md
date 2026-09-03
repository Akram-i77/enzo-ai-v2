# ENZO_CLEANUP_AUDIT.md

**Generated:** 2026-08-12 17:36 GMT+1
**Scope:** Full repository `/home/openclaw/.openclaw/workspace`
**Method:** Static inspection (inventory + import graph + CLI + config + runtime), NO deletions, NO refactoring.

---

## ⚠️ Git Safety Notice

```bash
git status --short   → ALL files untracked (??)
git branch --show-current → master
git log -5 --oneline → fatal: branch 'master' has no commits yet
```

**The repository is NOT committed to Git.** Every file is untracked and there is **no history to fall back on**. Therefore the **only rollback path is `.trash-enzo-cleanup/`** — files must be moved there (not `rm`), preserving structure, per the cleanup rules.

---

## 🔍 Runtime Baseline (captured 2026-08-12 17:36)

| Check | Result |
|-------|--------|
| Active ENZO processes | `enzo_serve.py 8077` (pid 877), `enzo_botctl.py` (pid 878) |
| `enzo_serve.py` HTTP | ✅ Serves `enzo-dashboard.html` on `127.0.0.1:8077`; `/api/prices` returns JSON |
| Core module imports (24 modules) | ✅ ALL import cleanly (enzo_config, enzo_log, enzo_cache, enzo_gmgn, enzo_security, enzo_analyze, enzo_run, enzo_engine, enzo_pump, enzo_pump_adv, enzo_pricefeed, enzo_portfolio, enzo_learn, enzo_notify, enzo_botctl, enzo_serve, enzo_wallet_behavior, enzo_wallet_quality, enzo_dev_analysis, enzo_market_structure, enzo_fetch_jupiter, enzo_curve, enzo_trades, enzo_dashboard, enzo_daily, enzo_audit) |
| `check_imports.py` full scan | ⚠️ FAILURES (expected — see Findings F-01) |
| `enzo_botctl.py status` | → hangs (waits on something; process 878 is alive, confirms botctl loop running) |
| Config files | `enzo-config.yaml` (ACTIVE, consumed by enzo_*.py), `config.yaml` (LEGACY, see C-01), `ENZO_GMGN/config.yaml` (experiment) |

---

## 📊 Classification Summary

| Class | Count | Meaning |
|-------|-------|---------|
| **A — ACTIVE** | 28 | Used in runtime ENZO pipeline |
| **B — REQUIRED SUPPORT** | 12 | Config, state, secrets, dashboard, reports, skills |
| **C — LEGACY / SUPERSEDED** | 9 | Old architecture replaced by GMGN migration |
| **D — UNUSED / DEAD** | 7 | No active path references them |
| **E — ARTIFACTS** | 6 | Logs, caches, trash, generated |
| **F — DO NOT TOUCH** | 8 | Secrets, credentials, OpenClaw, runtime state |

---

## 📋 Full Audit Table

### A — ACTIVE (used in runtime ENZO pipeline)

| File | Used By | Evidence | Action |
|------|---------|----------|--------|
| `enzo_analyze.py` | enzo_engine, enzo_run, enzo_portfolio, enzo_notify | imports enzo_security/wallet_behavior/dev_analysis/market_structure/gmgn; has `__main__` | KEEP |
| `enzo_audit.py` | CLI (`python3 enzo_audit.py`) | standalone audit tool; reads enzo-log.jsonl | KEEP |
| `enzo_botctl.py` | process 878 (running) | subprocess.Popen launches enzo_serve/enzo_pump; imports enzo_analyze/portfolio/notify | KEEP |
| `enzo_cache.py` | enzo_dev_analysis, enzo_market_structure, enzo_gmgn | imported for TTL cache | KEEP |
| `enzo_config.py` | enzo_dev_analysis, enzo_market_structure, enzo_wallet_behavior, enzo_wallet_quality | `from enzo_config import clamp, load_config` | KEEP |
| `enzo_curve.py` | enzo_pricefeed, enzo_gmgn | bonding-curve PDA; compat aliases | KEEP |
| `enzo_daily.py` | CLI | imports portfolio/notify; daily report | KEEP |
| `enzo_dashboard.py` | CLI | generates enzo-dashboard.html | KEEP |
| `enzo_dev_analysis.py` | enzo_analyze | dev behavior axis | KEEP |
| `enzo_engine.py` | process (watchlist scan) | imports enzo_run/log/notify/portfolio/learn/analyze | KEEP |
| `enzo_fetch_jupiter.py` | enzo_run | market-data layer (DexScreener-primary, Jupiter-fallback) | KEEP |
| `enzo_gmgn.py` | 14 modules | SOLE data source since 2026-08-05; subprocess gmgn-cli | KEEP |
| `enzo_learn.py` | enzo_engine, enzo_pump | learning engine | KEEP |
| `enzo_log.py` | enzo_gmgn, enzo_wallet_quality, enzo_engine, enzo_pump | logging | KEEP |
| `enzo_market_structure.py` | enzo_analyze | market structure axis | KEEP |
| `enzo_notify.py` | enzo_botctl, enzo_engine, enzo_pump, enzo_daily | Telegram+console | KEEP |
| `enzo_portfolio.py` | enzo_botctl, enzo_engine, enzo_pump, enzo_trades, enzo_dashboard, enzo_serve, enzo_analyze | paper ledger, exits | KEEP |
| `enzo_pricefeed.py` | runtime (WS/poll) | imports enzo_gmgn, enzo_curve | KEEP |
| `enzo_pump.py` | process (pump monitor) | imports enzo_run/log/notify/portfolio/learn/gmgn | KEEP |
| `enzo_pump_adv.py` | enzo_engine, enzo_pump (hybrid) | pump advanced-v2 discovery + live prices | KEEP |
| `enzo_run.py` | enzo_engine, enzo_pump | orchestrator; imports fetch_jupiter/security/analyze | KEEP |
| `enzo_security.py` | enzo_analyze, enzo_run, enzo_dev_analysis, enzo_wallet_behavior | security axis | KEEP |
| `enzo_serve.py` | process 877 (running) | dashboard server; imports enzo_portfolio, enzo_gmgn | KEEP |
| `enzo_trades.py` | CLI (`python3 enzo_trades.py`) | trade-log viewer; imports portfolio | KEEP |
| `enzo_wallet_behavior.py` | enzo_analyze | wallet behavior axis | KEEP |
| `enzo_wallet_quality.py` | enzo_analyze (via scoring) | wallet quality score | KEEP |
| `enzo_tests_gapfill.py` | CLI (`python3 enzo_tests_gapfill.py`) | unit tests | KEEP |
| `check_imports.py` | dev tool | import graph validator | KEEP (B-support) |

### B — REQUIRED SUPPORT (config, state, secrets, dashboard, skills, reports)

| File | Used By | Evidence | Action |
|------|---------|----------|--------|
| `enzo-config.yaml` | enzo_config.load_config | active config (risk, scam, scoring, data_sources.gmgn) | KEEP |
| `enzo-secrets.json` | enzo_notify (Telegram) | **CLASS F** — API keys; do not touch | DO NOT TOUCH |
| `enzo-dashboard.html` | enzo_serve.py (ALLOWED) | served live on 8077 | KEEP |
| `enzo-portfolio.json` | enzo_portfolio.load_state | **CLASS F** — open positions state | DO NOT TOUCH |
| `enzo-learning.json` | enzo_learn | **CLASS F** — learning data | DO NOT TOUCH |
| `enzo-state.json` | enzo_engine/enzo_pump | runtime state | KEEP (runtime) |
| `enzo-watchlist.json` | enzo_engine | WIF + POPCAT watchlist | KEEP |
| `enzo-gmgn-ban.json` | enzo_gmgn.ban_status | shared cross-process ban state | KEEP (runtime) |
| `enzo-control.json` | enzo_botctl | bot control state | KEEP (runtime) |
| `enzo-panel.json` | dashboard/control | UI panel state | KEEP (runtime) |
| `skills/enzo/SKILL.md` | OpenClaw skill | ENZO sub-agent definition | KEEP |
| `skills/gmgn-*/SKILL.md` | OpenClaw skills | GMGN data skills | KEEP |

### C — LEGACY / SUPERSEDED (replaced by GMGN migration or later upgrades)

| File | Superseded By | Evidence | Action |
|------|---------------|----------|--------|
| `config.yaml` | `enzo-config.yaml` | OLD schema (minimum_liquidity: 500000, email notify); NOT imported by any enzo_*.py (only `src/` and `main.py` reference old keys) | ARCHIVE → `.trash-enzo-cleanup/` |
| `src/` (entire tree) | `enzo_*.py` standalone scripts | Blueprint "production" package; 0-byte stub files; imports `aiohttp`/`pydantic` (not installed); no enzo_*.py imports it | ARCHIVE → `.trash-enzo-cleanup/` |
| `memecoin_trading_specialist/` | `enzo_*.py` | Only `__init__.py` stubs + README; no active runtime import | ARCHIVE → `.trash-enzo-cleanup/` |
| `main.py` | `enzo_engine.py` / `enzo_run.py` | Imports `src.*` (broken, aiohttp missing); not launched by botctl | ARCHIVE → `.trash-enzo-cleanup/` |
| `ENZO_GMGN/` | `enzo_*.py` + `enzo_pump_adv.py` | Self-contained GMGN experiment (2026-08-05); proven alternative but ENZO uses enzo_gmgn.py | ARCHIVE → `.trash-enzo-cleanup/` (keep as reference) |
| `enzo-system-prompt.md` | `skills/enzo/SKILL.md` | Old system prompt doc; superseded by skill | ARCHIVE → `.trash-enzo-cleanup/` |
| `README.md` | — | Generic workspace README; mentions old architecture | KEEP (workspace doc) or ARCHIVE |
| `ENZO-REPORT.md` | newer reports | Old report (Jul 13); superseded by ENZO_GMGN_* reports | ARCHIVE → `.trash-enzo-cleanup/` |
| `enzo_audit.py` vs `enzo-audit.jsonl` | — | `enzo_audit.py` is ACTIVE tool; `enzo-audit.jsonl` is its output log (ARTIFACT E) | KEEP .py / ARCHIVE .jsonl |

### D — UNUSED / DEAD (no active path)

| File | Reason | Evidence | Action |
|------|--------|----------|--------|
| `utils/` (empty `__init__.py`, 0-byte `helpers.py`) | No imports; dead stub dir | grep shows no `import utils` in enzo_*.py | ARCHIVE → `.trash-enzo-cleanup/` |
| `tests/` (0-byte `__init__.py`, 0-byte `test_market_analyzer.py`) | Empty stubs; no real tests | same as utils | ARCHIVE → `.trash-enzo-cleanup/` |
| `.claude/` | Claude skills mirror (not used by ENZO runtime) | no enzo import; OpenClaw uses `.agents/` + `skills/` | ARCHIVE → `.trash-enzo-cleanup/` |
| `.agents/` | Duplicate of `agent/` + `skills/` | gmgn-holder-analysis duplicated in `agent/`; not imported by enzo_*.py | ARCHIVE → `.trash-enzo-cleanup/` |
| `agent/` | gmgn-* skills (not ENZO runtime) | analyze.py imports fail (IndexError); not in enzo pipeline | ARCHIVE → `.trash-enzo-cleanup/` |
| `memecoin-trading-specialist/` | duplicate of `memecoin_trading_specialist/` | both empty stubs | ARCHIVE → `.trash-enzo-cleanup/` |
| `cdp_axiom.js` | CDP/JS script; no Python import; not referenced | orphan file | ARCHIVE → `.trash-enzo-cleanup/` |
| `openclaw-workspace-state.json` | OpenClaw internal state (not ENZO) | not ENZO-related | DO NOT TOUCH (F) |
| `skills-lock.json` | OpenClaw skill lock | not ENZO | DO NOT TOUCH (F) |
| `requirements.txt` | May list deps; check if needed | verify | KEEP (B) or ARCHIVE |

### E — ARTIFACTS (logs, caches, trash, generated)

| File | Size | Evidence | Action |
|------|------|----------|--------|
| `enzo-pump.log` | 15.6 MB | pump monitor log; regenerated each run | ARCHIVE → `.trash-enzo-cleanup/` |
| `enzo-audit.jsonl` | 15.4 MB | enzo_audit.py output | ARCHIVE → `.trash-enzo-cleanup/` |
| `enzo-log.jsonl` | 14.2 MB | general ENZO log | ARCHIVE → `.trash-enzo-cleanup/` |
| `enzo-botctl.log` | 690 KB | botctl log | ARCHIVE → `.trash-enzo-cleanup/` |
| `enzo-serve.log` | 9.8 KB | serve log | ARCHIVE → `.trash-enzo-cleanup/` |
| `enzo-cache.json` | 793 KB | GMGN cache; regenerated | ARCHIVE → `.trash-enzo-cleanup/` |
| `enzo-market-structure.json` | 136 KB | generated market structure cache | ARCHIVE → `.trash-enzo-cleanup/` |
| `.trash-gmgn-migration/` | — | OLD trash from 2026-08-05 migration (enzo_fetch.py, enzo_ws.py) | ARCHIVE → `.trash-enzo-cleanup/migration/` (already quarantined) |
| `__pycache__/` | — | Python bytecode cache; regenerated | SAFE TO DELETE (not archived) |
| `.venv/` | — | Virtual env; regenerated via pip | DO NOT TOUCH (F) |

### F — DO NOT TOUCH (sensitive / out-of-scope / runtime state)

| File | Reason |
|------|--------|
| `enzo-secrets.json` | Telegram bot token + chat ID (API keys) |
| `enzo-portfolio.json` | Open positions (paper ledger) — live state |
| `enzo-learning.json` | Learning engine data |
| `.venv/` | Python virtual environment |
| `openclaw-workspace-state.json` | OpenClaw internal |
| `skills-lock.json` | OpenClaw skill lock |
| `AGENTS.md`, `SOUL.md`, `USER.md`, `IDENTITY.md`, `TOOLS.md`, `MEMORY.md`, `HEARTBEAT.md` | OpenClaw workspace persona/config |
| `memory/` | Daily memory logs (OpenClaw continuity) |
| `.git/`, `.gitignore` | Git internals |
| `skills/` (enzo, gmgn-*) | Active OpenClaw skills |

---

## 🎯 Candidate Deletion / Archive List (STAGE 1 — PROPOSED ONLY)

**To ARCHIVE into `.trash-enzo-cleanup/` (NOT deleted):**

1. `config.yaml` — superseded by `enzo-config.yaml` (C-01)
2. `src/` — blueprint package, never used by runtime (C-02)
3. `memecoin_trading_specialist/` — empty stub duplicate (C-03)
4. `main.py` — broken entrypoint, not launched (C-04)
5. `ENZO_GMGN/` — proven GMGN experiment, ENZO uses `enzo_gmgn.py` (C-05)
6. `enzo-system-prompt.md` — superseded by skill (C-06)
7. `ENZO-REPORT.md` — old report (C-07)
8. `utils/` — dead stubs (D-01)
9. `tests/` — empty stubs (D-02)
10. `.claude/` — unused mirror (D-03)
11. `.agents/` — duplicate skills (D-04)
12. `agent/` — unused gmgn skills (D-05)
13. `memecoin-trading-specialist/` — duplicate stub (D-06)
14. `cdp_axiom.js` — orphan (D-07)
15. `enzo-pump.log`, `enzo-audit.jsonl`, `enzo-log.jsonl`, `enzo-botctl.log`, `enzo-serve.log` — logs (E-01..05)
16. `enzo-cache.json`, `enzo-market-structure.json` — caches (E-06..07)
17. `.trash-gmgn-migration/` — already quarantined (E-08)

**To DELETE (safe, regenerable):**
- `__pycache__