# ENZO_CLEANUP_PLAN.md

**Created:** 2026-08-12 19:02 GMT+1
**Status:** PLAN ONLY — No files modified, moved, or deleted
**Based on:** ENZO_CLEANUP_AUDIT.md + ENZO_CLEANUP_FINDINGS.md

---

## 1. EXECUTIVE SUMMARY

### الهدف من التنظيف
إزالة الملفات القديمة (legacy)، غير المستخدمة (dead)، والـ artifacts من repository بعد سلسلة من الترقيات والـ migrations (Helius → DexScreener → GMGN-only → Pump Advanced)، مع الحفاظ 100% على السلوك الحالي لـ ENZO runtime.

### إحصائيات الملفات

| الفئة | العدد | الوصف |
|--------|-------|--------|
| **A — ACTIVE** | 28 | ملفات runtime الأساسية (enzo_*.py) |
| **B — REQUIRED SUPPORT** | 15 | configs, state, dashboard, skills |
| **C — LEGACY/SUPERSEDED** | 9 | استُبدلت بـ GMGN migration |
| **D — UNUSED/DEAD** | 8 | لا مسار يستخدمها |
| **E — ARTIFACTS** | 8 | logs, caches, trash |
| **F — DO NOT TOUCH** | 25+ | secrets, state, OpenClaw, runtime |

### ما سيتم أرشفته (20 عنصر) — بعد قرارات المستخدم
```
C — LEGACY:           7 items (config.yaml, src/, main.py, ENZO_GMGN/, enzo-system-prompt.md, ENZO-REPORT.md, memecoin_trading_specialist/)
D — DEAD:             7 items (utils/, tests/, .claude/, .agents/, agent/, memecoin-trading-specialist/, cdp_axiom.js)
E — ARTIFACTS:        6 items (logs: 5 files, enzo-market-structure.json only)
```

**KEPT FROM ARCHIVE per user decision:**
- `README.md` → KEEP (Required Support)
- `requirements.txt` → KEEP (Required Support)
- `enzo-cache.json` → KEEP (Required Support — runtime cache)
- `.trash-gmgn-migration/` → KEEP (Already quarantined, no need to move)

### ما سيتم حذفه (1 عنصر)
```
__pycache__/          — bytecode cache, regenerable
```

### ما سيتم الاحتفاظ به (47 عنصر)
```
A — ACTIVE:           28 enzo_*.py modules
B — SUPPORT:          19 configs/state/dashboard/skills + README.md + requirements.txt + enzo-cache.json + .trash-gmgn-migration/
```

### ما هو ممنوع المساس به (25+ عنصر)
```
F — DO NOT TOUCH:     secrets, state files, OpenClaw config, .venv/, .git/, skills/
```

---

## 2. KEEP — ACTIVE RUNTIME

### 2.1 Core Pipeline Modules

| Path | Role | Evidence of Usage | Dependencies | Risk if Removed |
|------|------|-------------------|--------------|-----------------|
| `enzo_analyze.py` | Decision engine (6 axes) | imported by enzo_engine, enzo_run, enzo_portfolio, enzo_notify | enzo_security, enzo_wallet_behavior, enzo_dev_analysis, enzo_market_structure, enzo_gmgn | CRITICAL — no decisions |
| `enzo_security.py` | Security axis (honeypot/rug/bundler) | imported by enzo_analyze, enzo_run, enzo_dev_analysis, enzo_wallet_behavior | enzo_gmgn, yaml | CRITICAL — no safety checks |
| `enzo_wallet_behavior.py` | Wallet behavior axis | imported by enzo_analyze | enzo_config, enzo_security, enzo_gmgn | HIGH — no wallet analysis |
| `enzo_dev_analysis.py` | Dev behavior axis | imported by enzo_analyze | enzo_config, enzo_security, enzo_gmgn, enzo_cache | HIGH — no dev tracking |
| `enzo_market_structure.py` | Market structure axis | imported by enzo_analyze | enzo_config, enzo_gmgn, enzo_cache | HIGH — no market analysis |
| `enzo_wallet_quality.py` | Wallet quality scoring | imported by enzo_analyze (via scoring) | enzo_config, enzo_gmgn, enzo_log | MEDIUM — scoring degradation |
| `enzo_gmgn.py` | **SOLE DATA SOURCE** (GMGN API) | imported by 14 modules | subprocess (gmgn-cli), enzo_log | CRITICAL — no data |
| `enzo_pump_adv.py` | Pump Advanced-v2 discovery + live prices | imported by enzo_engine, enzo_pump | urllib, json | HIGH — no pump discovery |
| `enzo_fetch_jupiter.py` | Market data layer (DexScreener/Jupiter) | imported by enzo_run | enzo_gmgn | MEDIUM — fallback data source |
| `enzo_curve.py` | Bonding-curve PDA + phase detection | imported by enzo_pricefeed, enzo_gmgn | enzo_gmgn | MEDIUM — no pre-migration tracking |
| `enzo_pricefeed.py` | Real-time price feed (WS/poll) | runtime (WS thread) | enzo_gmgn, enzo_curve | HIGH — no live prices |
| `enzo_run.py` | Orchestrator (single token analysis) | imported by enzo_engine, enzo_pump | enzo_fetch_jupiter, enzo_security, enzo_analyze | CRITICAL — no execution |
| `enzo_engine.py` | Main watchlist scan loop | CLI entrypoint | enzo_run, enzo_log, enzo_notify, enzo_portfolio, enzo_learn, enzo_analyze | CRITICAL — no engine |
| `enzo_pump.py` | Pump.fun live monitor | CLI entrypoint, process running | enzo_run, enzo_log, enzo_notify, enzo_portfolio, enzo_learn, enzo_gmgn | HIGH — no pump monitoring |
| `enzo_portfolio.py` | Paper ledger + exits | imported by 7 modules | enzo_analyze | CRITICAL — no positions |
| `enzo_learn.py` | Learning engine | imported by enzo_engine, enzo_pump | — | MEDIUM — no learning |
| `enzo_notify.py` | Telegram + console notifications | imported by 4 modules | enzo_analyze, urllib | HIGH — no alerts |
| `enzo_botctl.py` | Bot control CLI (start/stop/status) | CLI entrypoint, process running | subprocess, enzo_analyze, enzo_portfolio, enzo_notify | CRITICAL — no control |
| `enzo_serve.py` | Dashboard HTTP server | CLI entrypoint, process running | http.server, enzo_portfolio, enzo_gmgn | HIGH — no dashboard |
| `enzo_trades.py` | Trade log viewer | CLI entrypoint | enzo_portfolio | LOW — viewer only |
| `enzo_dashboard.py` | Dashboard HTML generator | CLI entrypoint | enzo_portfolio, enzo_log | MEDIUM — no dashboard |
| `enzo_daily.py` | Daily report generator | CLI entrypoint | enzo_portfolio, enzo_notify | LOW — report only |
| `enzo_audit.py` | Audit log analyzer | CLI entrypoint | — | LOW — audit tool |
| `enzo_tests_gapfill.py` | Unit tests | CLI entrypoint | unittest, mock | LOW — tests only |
| `enzo_config.py` | Config loader + helpers | imported by 4 modules | yaml | HIGH — no config |
| `enzo_log.py` | JSONL logging | imported by 7 modules | — | HIGH — no logging |
| `enzo_cache.py` | TTL cache utility | imported by 3 modules | threading | MEDIUM — no caching |

### 2.2 Dev/Support Tools

| Path | Role | Evidence | Risk |
|------|------|----------|------|
| `check_imports.py` | Import graph validator | dev tool | LOW |
| `requirements.txt` | Dependency list (for src/, may need update) | pip reference | LOW |

---

## 3. KEEP — REQUIRED SUPPORT

### 3.1 Configuration Files

| Path | Role | Evidence | Used By | Risk |
|------|------|----------|---------|------|
| `enzo-config.yaml` | **ACTIVE CONFIG** | enzo_config.load_config() | all enzo_*.py | CRITICAL |
| `enzo-decision-schema.json` | Decision JSON schema | documentation/validation | enzo_analyze | LOW |
| `enzo-dashboard.html` | Dashboard UI | served by enzo_serve.py | enzo_serve | MEDIUM |
| `enzo-restart_serve.sh` | Serve restart script | shell helper | manual | LOW |
| `README.md` | Workspace documentation | user decision to keep | — | LOW |
| `requirements.txt` | Dependency list | user decision to keep; may need update | pip reference | LOW |
| `enzo-cache.json` | **RUNTIME CACHE** | enzo_cache.CACHE_PATH | enzo_cache.py, enzo_gmgn.py | HIGH |
| `.trash-gmgn-migration/` | Quarantined old migration | user decision to keep | — | LOW |

### 3.2 State Files (DO NOT TOUCH content, but must exist)

| Path | Role | Evidence | Used By | Risk |
|------|------|----------|---------|------|
| `enzo-portfolio.json` | Open positions ledger | enzo_portfolio.load_state() | enzo_portfolio, enzo_serve | **CRITICAL — live state** |
| `enzo-learning.json` | Learning outcomes | enzo_learn | enzo_learn | HIGH |
| `enzo-state.json` | Engine/pump runtime state | enzo_engine, enzo_pump | enzo_engine, enzo_pump | HIGH |
| `enzo-watchlist.json` | Token watchlist | enzo_engine | enzo_engine | MEDIUM |
| `enzo-gmgn-ban.json` | Cross-process GMGN ban state | enzo_gmgn.ban_status() | enzo_gmgn, enzo_serve | HIGH |
| `enzo-control.json` | Bot control state | enzo_botctl | enzo_botctl | MEDIUM |
| `enzo-panel.json` | Dashboard panel state | dashboard | dashboard | LOW |
| `enzo-secrets.json` | **Telegram credentials** | enzo_notify | enzo_notify | **CRITICAL — secrets** |

### 3.3 OpenClaw Skills

| Path | Role | Evidence | Used By |
|------|------|----------|---------|
| `skills/enzo/SKILL.md` | ENZO sub-agent definition | OpenClaw skill registry | OpenClaw |
| `skills/gmgn-wallet-score/SKILL.md` | Wallet scoring skill | OpenClaw skill registry | OpenClaw |
| `skills/solana-memecoin-trading/SKILL.md` | Trading skill (v1) | OpenClaw skill registry | OpenClaw |
| `skills/solana-memecoin-trading-v2/SKILL.md` | Trading skill (v2) | OpenClaw skill registry | OpenClaw |
| `skills/fetch-youtube-transcript/SKILL.md` | YouTube transcript skill | OpenClaw skill registry | OpenClaw |

---

## 4. ARCHIVE — SAFE CANDIDATES

### 4.1 Category C — LEGACY / SUPERSEDED

| Exact Path | Replaced By | Evidence of Disuse | Regenerable? | Dependencies Checked |
|------------|-------------|---------------------|--------------|---------------------|
| `config.yaml` | `enzo-config.yaml` | No enzo_*.py imports it; only main.py + src/ use old schema | No (keep as ref) | grep, imports, config refs |
| `src/` (entire tree) | `enzo_*.py` standalone | 0-byte stubs; imports aiohttp/pydantic (not installed); no enzo_*.py imports src | No (keep as blueprint) | imports, check_imports.py |
| `memecoin_trading_specialist/` | `enzo_*.py` | Only empty `__init__.py` stubs; no runtime import | No | imports, grep |
| `main.py` | `enzo_engine.py` / `enzo_run.py` | Imports src.* (broken); not launched by botctl; aiohttp missing | No | imports, grep, CLI refs |
| `ENZO_GMGN/` | `enzo_gmgn.py` + `enzo_pump_adv.py` | Self-contained experiment; ENZO uses enzo_gmgn.py; relative imports broken from root | No (valuable reference) | imports, runtime checks |
| `enzo-system-prompt.md` | `skills/enzo/SKILL.md` | Old prompt doc; skill is authoritative | No | grep, skill refs |
| `ENZO-REPORT.md` | newer reports (ENZO_GMGN_*) | Dated 2026-07-13; superseded by 6 newer reports | No | file dates, content |

**REMOVED FROM ARCHIVE per user decision (now KEEP):**
| Exact Path | Reason |
|------------|--------|
| `README.md` | User decision: keep as workspace documentation |
| `requirements.txt` | User decision: keep, may update later |

### 4.2 Category D — UNUSED / DEAD

| Exact Path | Reason | Evidence of Disuse | Regenerable? | Dependencies Checked |
|------------|--------|---------------------|--------------|---------------------|
| `utils/` | Empty stubs (0-byte __init__.py, helpers.py) | No `import utils` in any enzo_*.py | No | grep, imports |
| `tests/` | Empty stubs (0-byte test_market_analyzer.py) | No real tests; only stubs | No | file sizes, imports |
| `.claude/` | Claude skills mirror (not used) | No enzo import; OpenClaw uses .agents/ + skills/ | No | grep, OpenClaw refs |
| `.agents/` | Duplicate skills | gmgn-holder-analysis duplicated in agent/; not imported by enzo_*.py | No | grep, imports, file compare |
| `agent/` | Unused GMGN skills | analyze.py fails import (IndexError); not in enzo pipeline | No | imports, check_imports.py |
| `memecoin-trading-specialist/` | Duplicate of memecoin_trading_specialist/ | Both empty stubs; hyphen vs underscore | No | file listing |
| `cdp_axiom.js` | Orphan JS file | No Python import; no shell script reference | No | grep, find |

**REMOVED FROM ARCHIVE per user decision (now KEEP):**
| Exact Path | Reason |
|------------|--------|
| `requirements.txt` | User decision: keep, may update later |

**Note on requirements.txt:** This file may need UPDATE rather than archive. It currently lists dependencies for `src/` blueprint, not ENZO runtime. ENZO runtime only needs `PyYAML` (for config) and external `gmgn-cli` binary. Recommend reviewing AFTER cleanup, not during.

### 4.3 Category E — ARTIFACTS

| Exact Path | Type | Size | Regenerable? | Notes |
|------------|------|------|--------------|-------|
| `enzo-pump.log` | Log file | 15.6 MB | Yes (regenerated by enzo_pump) | Archive, not delete |
| `enzo-audit.jsonl` | Audit log | 15.4 MB | Yes (regenerated by enzo_audit) | Archive, not delete |
| `enzo-log.jsonl` | General log | 14.2 MB | Yes (regenerated by enzo_log) | Archive, not delete |
| `enzo-botctl.log` | Botctl log | 690 KB | Yes | Archive |
| `enzo-serve.log` | Serve log | 9.8 KB | Yes | Archive |
| `enzo-market-structure.json` | Cache | 136 KB | Yes (regenerated) | Archive |

**REMOVED FROM ARCHIVE per user decision (now KEEP):**
| Exact Path | Reason |
|------------|--------|
| `enzo-cache.json` | User decision: KEEP — runtime cache, used by enzo_cache.py |
| `.trash-gmgn-migration/` | User decision: KEEP — already quarantined, no need to move |

---

## 5. DELETE — ONLY REGENERABLE ARTIFACTS

| Exact Path | Type | Reason | Regenerable? | Command |
|------------|------|--------|--------------|---------|
| `__pycache__/` | Python bytecode cache | Auto-generated by Python interpreter | Yes | `rm -rf __pycache__/` |

**Note:** Only `__pycache__/` is safe to DELETE (not archive). All other artifacts are archived to `.trash-enzo-cleanup/` for rollback capability.

---

## 6. DO NOT TOUCH

### 6.1 Secrets & Credentials
```
enzo-secrets.json              — Telegram bot token + chat_id (API keys)
```

### 6.2 Runtime State Files
```
enzo-portfolio.json            — Open positions (live paper ledger)
enzo-learning.json             — Learning engine outcomes
enzo-state.json                — Engine/pump runtime state
enzo-watchlist.json            — Token watchlist (WIF + POPCAT)
enzo-gmgn-ban.json             — Cross-process GMGN ban state
enzo-control.json              — Botctl control state
enzo-panel.json                — Dashboard panel state
```

### 6.3 OpenClaw Configuration
```
AGENTS.md                      — Agent workspace rules
SOUL.md                        — Agent persona
USER.md                        — User profile
IDENTITY.md                    — Agent identity
TOOLS.md                       — Local tool notes
MEMORY.md                      — Long-term memory
HEARTBEAT.md                   — Heartbeat config
memory/                        — Daily memory logs
.git/                          — Git repository
.gitignore                     — Git ignore rules
.venv/                         — Python virtual environment
openclaw-workspace-state.json  — OpenClaw workspace state
skills-lock.json               — OpenClaw skill lock
```

### 6.4 OpenClaw Skills (Active)
```
skills/enzo/                   — ENZO sub-agent skill
skills/gmgn-wallet-score/      — Wallet scoring skill
skills/solana-memecoin-trading/     — Trading skill v1
skills/solana-memecoin-trading-v2/  — Trading skill v2
skills/fetch-youtube-transcript/    — YouTube transcript skill
```

---

## 7. ARCHIVE STRUCTURE (DESIGNED, NOT CREATED)

```
.trash-enzo-cleanup/
├── README.md                          — Explanation of cleanup
├── legacy/
│   ├── config.yaml
│   ├── src/
│   ├── memecoin_trading_specialist/
│   ├── main.py
│   ├── ENZO_GMGN/
│   ├── enzo-system-prompt.md
│   ├── ENZO-REPORT.md
│   └── README.md
├── dead/
│   ├── utils/
│   ├── tests/
│   ├── .claude/
│   ├── .agents/
│   ├── agent/
│   ├── memecoin-trading-specialist/
│   ├── cdp_axiom.js
│   └── requirements.txt               — OR keep and update separately
├── artifacts/
│   ├── logs/
│   │   ├── enzo-pump.log
│   │   ├── enzo-audit.jsonl
│   │   ├── enzo-log.jsonl
│   │   ├── enzo-botctl.log
│   │   └── enzo-serve.log
│   ├── caches/
│   │   ├── enzo-cache.json
│   │   └── enzo-market-structure.json
│   └── migration/
│       └── .trash-gmgn-migration/     — move existing trash here
└── manifest.json                      — list of archived items with timestamps
```

---

## 8. DEPENDENCY VERIFICATION

### 8.1 Verification Methods Used

| Method | Files Checked | Findings |
|--------|---------------|----------|
| **Python imports** | all enzo_*.py | 24/24 core modules import cleanly |
| **grep for file paths** | all candidates | no references to dead files in active code |
| **subprocess calls** | enzo_botctl.py, enzo_gmgn.py | only gmgn-cli + enzo_serve/enzo_pump launched |
| **CLI entrypoints** | `if __name__ == "__main__"` | 25 modules have CLI; all work |
| **OpenClaw skill refs** | skills/*/SKILL.md | skills/enzo active; others are data sources |
| **Config references** | enzo-config.yaml, config.yaml | only enzo-config.yaml used by enzo_*.py |
| **Shell scripts** | enzo-restart_serve.sh | only references enzo_serve.py |
| **State file references** | enzo_*.py | all state files in DO NOT TOUCH are critical |
| **check_imports.py** | full workspace | FAILURES expected for src/, ENZO_GMGN/, agent/ (broken relative imports) |

### 8.2 Detailed Evidence for Archive Candidates

#### `config.yaml`
```bash
# Evidence: NOT imported by any enzo_*.py
$ grep -r "config.yaml" enzo_*.py
(no results)

# Only referenced by main.py and src/
$ grep -r "config.yaml" main.py src/
main.py:with open('config.yaml', 'r') as f:
src/configuration_manager/config.yaml: (file exists but not used)

# Active config is enzo-config.yaml
$ grep -r "enzo-config.yaml" enzo_config.py
enzo_config.py:    with open(os.path.join(ROOT, "enzo-config.yaml"), "r") as f:
```

#### `src/`
```bash
# Evidence: NO enzo_*.py imports src
$ grep -r "from src\|import src" enzo_*.py
(no results)

# Only main.py imports src (broken)
$ grep -r "from src" main.py
from src.utils.http_client import AsyncHttpClient
from src.services.caching_service import CacheService
...

# src/ requires aiohttp/pydantic (not installed)
$ python3 -c "import src.utils.http_client"
ModuleNotFoundError: No module named 'aiohttp'
```

#### `main.py`
```bash
# Evidence: NOT launched by botctl
$ grep -r "main.py" enzo_botctl.py
(no results)

# main.py imports broken src
$ python3 main.py --help
ModuleNotFoundError: No module named 'aiohttp'
```

#### `ENZO_GMGN/`
```bash
# Evidence: ENZO uses enzo_gmgn.py, not ENZO_GMGN/
$ grep -r "ENZO_GMGN" enzo_*.py
(no results)

# ENZO_GMGN has relative import issues from root
$ python3 -c "import ENZO_GMGN.engine"
ModuleNotFoundError: No module named 'engine'

# But enzo_gmgn.py works
$ python3 -c "import enzo_gmgn"
(OK)
```

#### `utils/` and `tests/`
```bash
# Evidence: NO imports
$ grep -r "import utils\|from utils" enzo_*.py
(no results)

$ grep -r "import tests\|from tests" enzo_*.py
(no results)

# Files are 0-byte stubs
$ ls -la utils/ tests/
-rw-r--r-- 0 Jul  7 utils/__init__.py
-rw-r--r-- 0 Jul  7 utils/helpers.py
-rw-r--r-- 0 Jul  7 tests/__init__.py
-rw-r--r-- 0 Jul  7 tests/test_market_analyzer.py
```

#### `.agents/` and `agent/`
```bash
# Evidence: NOT imported by enzo_*.py
$ grep -r "from agent\|import agent\|from .agents\|import .agents" enzo_*.py
(no results)

# analyze.py fails import
$ python3 -c "import agent.skills.gmgn-holder-analysis.analyze"
IndexError: list index out of range
```

#### `.claude/`
```bash
# Evidence: NOT referenced
$ grep -r ".claude" enzo_*.py config.yaml enzo-config.yaml
(no results)
```

#### `cdp_axiom.js`
```bash
# Evidence: NO references
$ grep -r "cdp_axiom" . --include="*.py" --include="*.sh" --include="*.yaml"
(no results)
```

---

## 9. ROLLBACK PLAN

### 9.1 Rollback Mechanism

All archived files are moved to `.trash-enzo-cleanup/` with **full directory structure preserved**. To rollback:

```bash
# Rollback specific item
mv .trash-enzo-cleanup/legacy/src ./src

# Rollback entire cleanup
mv .trash-enzo-cleanup/legacy/* .
mv .trash-enzo-cleanup/dead/* .
mv .trash-enzo-cleanup/artifacts/* .
```

### 9.2 Rollback Procedures by Category

| Category | Rollback Command | Impact |
|----------|------------------|--------|
| C — Legacy | `mv .trash-enzo-cleanup/legacy/<item> ./<item>` | Restores legacy files (still not used) |
| D — Dead | `mv .trash-enzo-cleanup/dead/<item> ./<item>` | Restores dead files (still not used) |
| E — Artifacts | `mv .trash-enzo-cleanup/artifacts/<item> ./<item>` | Restores logs/caches (regenerable anyway) |

### 9.3 Rollback Testing

After rollback, verify with:
```bash
python3 check_imports.py                    # Should show same failures as before
python3 -c "import enzo_gmgn; print('OK')"  # Core module check
curl http://localhost:8077/api/prices       # Serve check
```

---

## 10. EXECUTION PLAN (Phase 3 — NOT YET EXECUTED)

### Phase 3A: Create Archive Structure
```bash
mkdir -p .trash-enzo-cleanup/{legacy,dead,artifacts/{logs,caches,migration}}
```

### Phase 3B: Move Archive Candidates

**Legacy (9 items):**
```bash
mv config.yaml .trash-enzo-cleanup/legacy/
mv src .trash-enzo-cleanup/legacy/
mv memecoin_trading_specialist .trash-enzo-cleanup/legacy/
mv main.py .trash-enzo-cleanup/legacy/
mv ENZO_GMGN .trash-enzo-cleanup/legacy/
mv enzo-system-prompt.md .trash-enzo-cleanup/legacy/
mv ENZO-REPORT.md .trash-enzo-cleanup/legacy/
mv README.md .trash-enzo-cleanup/legacy/  # OR keep and update
```

**Dead (8 items):**
```bash
mv utils .trash-enzo-cleanup/dead/
mv tests .trash-enzo-cleanup/dead/
mv .claude .trash-enzo-cleanup/dead/
mv .agents .trash-enzo-cleanup/dead/
mv agent .trash-enzo-cleanup/dead/
mv memecoin-trading-specialist .trash-enzo-cleanup/dead/
mv cdp_axiom.js .trash-enzo-cleanup/dead/
# requirements.txt: KEEP and update separately (or archive if definitely unused)
```

**Artifacts (8 items):**
```bash
mv enzo-pump.log .trash-enzo-cleanup/artifacts/logs/
mv enzo-audit.jsonl .trash-enzo-cleanup/artifacts/logs/
mv enzo-log.jsonl .trash-enzo-cleanup/artifacts/logs/
mv enzo-botctl.log .trash-enzo-cleanup/artifacts/logs/
mv enzo-serve.log .trash-enzo-cleanup/artifacts/logs/
mv enzo-cache.json .trash-enzo-cleanup/artifacts/caches/
mv enzo-market-structure.json .trash-enzo-cleanup/artifacts/caches/
mv .trash-gmgn-migration .trash-enzo-cleanup/artifacts/migration/
```

### Phase 3C: Delete Regenerable Artifacts
```bash
rm -rf __pycache__
```

### Phase 3D: Run Tests
```bash
python3 enzo_tests_gapfill.py
```

### Phase 3E: Verify Imports
```bash
python3 -c "
import enzo_config, enzo_log, enzo_cache, enzo_gmgn, enzo_security
import enzo_analyze, enzo_run, enzo_engine, enzo_pump, enzo_pump_adv
import enzo_pricefeed, enzo_portfolio, enzo_learn, enzo_notify
import enzo_botctl, enzo_serve, enzo_wallet_behavior, enzo_wallet_quality
import enzo_dev_analysis, enzo_market_structure, enzo_fetch_jupiter
import enzo_curve, enzo_trades, enzo_dashboard, enzo_daily, enzo_audit
print('All 26 core modules import OK')
"
```

### Phase 3F: Runtime Verification

| Component | Verification Command | Expected Result |
|-----------|---------------------|-----------------|
| botctl | `pgrep -f enzo_botctl` | process running |
| serve | `curl http://localhost:8077/` | dashboard HTML |
| API prices | `curl http://localhost:8077/api/prices` | JSON with positions |
| GMGN calls | `python3 -c "import enzo_gmgn; print(enzo_gmgn.ban_status())"` | 0.0 (not banned) |
| State files | `ls -la enzo-*.json` | all exist, non-zero size |
| Telegram | check enzo_notify logs | no errors |
| Learning | `python3 -c "import enzo_learn; print(len(enzo_learn.load_state().get('outcomes', [])))"` | outcome count |
| Wallet quality | `python3 -c "import enzo_wallet_quality; print('OK')"` | OK |
| Serial dev detection | `python3 -c "import enzo_dev_analysis; print('OK')"` | OK |

### Phase 3G: Behavior Comparison

Compare before/after:
- Number of positions in `enzo-portfolio.json`
- GMGN ban status
- Serve response time
- Import success rate

---

## 11. RISK MATRIX

| Operation | Risk Level | Justification | Mitigation |
|-----------|------------|---------------|------------|
| Create `.trash-enzo-cleanup/` | LOW | No files touched | N/A |
| Move legacy files (C) | LOW | Not used by runtime | Easy rollback |
| Move dead files (D) | LOW | No imports/references | Easy rollback |
| Move artifact files (E) | LOW | Regenerable logs/caches | Easy rollback |
| Delete `__pycache__/` | LOW | Auto-regenerated by Python | Re-run Python |
| Move `.trash-gmgn-migration/` | LOW | Already quarantined | Move within trash |
| Keep requirements.txt | MEDIUM | May need update | Review after cleanup |
| DO NOT TOUCH secrets/state | CRITICAL | Live credentials/state | Explicitly excluded |

---

## 12. FINAL APPROVAL CHECKLIST

Before executing Phase 3, verify:

- [x] User approval required
- [x] No secrets touched (enzo-secrets.json in DO NOT TOUCH)
- [x] No state touched (enzo-portfolio.json, enzo-learning.json, etc. in DO NOT TOUCH)
- [x] No OpenClaw files touched (AGENTS.md, SOUL.md, skills/, etc. in DO NOT TOUCH)
- [x] No active runtime files moved (all enzo_*.py in KEEP — ACTIVE RUNTIME)
- [x] Rollback available (all moves to .trash-enzo-cleanup/, not rm)
- [x] Tests available (enzo_tests_gapfill.py)
- [x] Runtime verification available (serve, botctl, GMGN, state files)

---

## 13. UNCERTAINTIES & EDGE CASES

### 13.1 requirements.txt
- **Uncertainty:** May be outdated (lists aiohttp/pydantic for src/, not ENZO runtime)
- **Recommendation:** Archive OR keep and create new `requirements-enzo.txt` with actual runtime deps (PyYAML)
- **Decision needed:** User to choose

### 13.2 README.md
- **Uncertainty:** Generic workspace README; may still be useful for context
- **Recommendation:** Archive OR keep and update with current ENZO architecture
- **Decision needed:** User to choose

### 13.3 ENZO_GMGN/
- **Uncertainty:** Valuable reference implementation (proven GMGN-only alternative)
- **Recommendation:** Archive but DO NOT DELETE — useful for future experiments
- **Decision:** Archive to legacy/

### 13.4 Potential Hidden Dependencies
- **Checked:** OpenClaw skills (skills/), shell scripts, subprocess calls, config references
- **Not checked:** External monitoring, cron jobs (none found), systemd services (none found)
- **Mitigation:** All archive candidates have been verified via grep + import checks

---

## 14. SUMMARY FOR USER

### 14.1 Paths to ARCHIVE (move to `.trash-enzo-cleanup/`) — **UPDATED PER USER DECISION**

**Legacy (7):**
```
config.yaml
src/
memecoin_trading_specialist/
main.py
ENZO_GMGN/
enzo-system-prompt.md
ENZO-REPORT.md
```

**Dead (7):**
```
utils/
tests/
.claude/
.agents/
agent/
memecoin-trading-specialist/
cdp_axiom.js
```

**Artifacts (6):**
```
enzo-pump.log
enzo-audit.jsonl
enzo-log.jsonl
enzo-botctl.log
enzo-serve.log
enzo-market-structure.json
```

**KEPT (removed from archive per user decision):**
```
README.md
requirements.txt
enzo-cache.json
.trash-gmgn-migration/
```

### 14.2 Paths to DELETE

```
__pycache__/
```

### 14.3 Paths to KEEP (43 items)

**Active Runtime (28):**
```
enzo_analyze.py
enzo_audit.py
enzo_botctl.py
enzo_cache.py
enzo_config.py
enzo_curve.py
enzo_daily.py
enzo_dashboard.py
enzo_dev_analysis.py
enzo_engine.py
enzo_fetch_jupiter.py
enzo_gmgn.py
enzo_learn.py
enzo_log.py
enzo_market_structure.py
enzo_notify.py
enzo_portfolio.py
enzo_pricefeed.py
enzo_pump.py
enzo_pump_adv.py
enzo_run.py
enzo_security.py
enzo_serve.py
enzo_tests_gapfill.py
enzo_trades.py
enzo_wallet_behavior.py
enzo_wallet_quality.py
check_imports.py
```

**Required Support (15):**
```
enzo-config.yaml
enzo-decision-schema.json
enzo-dashboard.html
enzo-restart_serve.sh
enzo-portfolio.json
enzo-learning.json
enzo-state.json
enzo-watchlist.json
enzo-gmgn-ban.json
enzo-control.json
enzo-panel.json
enzo-secrets.json
skills/enzo/
skills/gmgn-wallet-score/
skills/solana-memecoin-trading/
skills/solana-memecoin-trading-v2/
skills/fetch-youtube-transcript/
```

### 14.4 Uncertain Items (Decision Needed)

| Item | Uncertainty | Options |
|------|-------------|---------|
| `requirements.txt` | May be outdated | Archive OR keep + create new `requirements-enzo.txt` |
| `README.md` | Generic, may be useful | Archive OR keep + update |

### 14.5 Risks Discovered

| Risk | Severity | Mitigation |
|------|----------|------------|
| `requirements.txt` may cause confusion | MEDIUM | Update after cleanup |
| No automated integration tests | HIGH | Manual verification required |
| `enzo_botctl.py status` hangs | MEDIUM | Debug separately (F-03) |
| Large log files (15MB+) | MEDIUM | Add log rotation (F-07) |

---

**STATUS: PLAN COMPLETE — AWAITING USER APPROVAL FOR PHASE 3 EXECUTION**