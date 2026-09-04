#!/usr/bin/env python3
"""Regression guard: the generated dashboard must contain VALID JavaScript.

Why this file exists
--------------------
The whole dashboard is emitted from ONE non-raw Python f-string in
enzo/ui/dashboard.py (the huge html_content f-string that starts with the
DOCTYPE line). Inside a non-raw f-string, Python expands every single-backslash
escape BEFORE the text is emitted. So writing a backslash-n inside a JS string
literal produces a REAL newline in the output:

    var tip = 'some text.<REAL NEWLINE>' + ...

which is a JavaScript SyntaxError. A SyntaxError anywhere in a <script> block
prevents the ENTIRE block from executing, so:

  * no polling runs        -> the Real-Time Activity Stream renders nothing
  * no handlers are bound  -> every button on the dashboard is dead
  * the static HTML still paints, so the page *looks* alive

That exact failure shipped once (the min_trade_usd floor tooltip). Nothing
caught it because no test ever validated the emitted JS. These checks do.

Two independent guards:
  1. SOURCE guard (always runs, no dependencies): scan the f-string template for
     single-backslash escape sequences that Python would expand.
  2. OUTPUT guard (runs when node is available): `node --check` the extracted
     <script> blocks — the authoritative syntax verdict.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✔ {name}")
    else:
        FAIL += 1
        print(f"  ✖ {name}" + (f" — {detail}" if detail else ""))
    return ok


# ── Guard 1: the template source must not contain expandable escapes ─────────
TQ = '"' * 3  # built, never written literally: this file's own docstring would end


def template_region(src: str):
    """Return the body of the big html_content f-string template."""
    opener = "html_content" + r"\s*=\s*f" + TQ
    m = re.search(opener, src)
    if not m:
        return None
    start = m.end()
    end = src.find(TQ, start)
    if end == -1:
        return None
    return src[start:end]


def test_source_has_no_expandable_escapes():
    print("\n[1] SOURCE guard — f-string template escapes")
    path = os.path.join(REPO, "enzo", "ui", "dashboard.py")
    src = open(path, encoding="utf-8").read()

    region = template_region(src)
    if not check("dashboard template located", region is not None,
                 "could not find the html_content f-string — guard is blind"):
        return

    # A single backslash before n/t/r that is NOT itself doubled. In the raw
    # file bytes, a correctly-escaped JS newline reads as two backslashes + n,
    # so the negative lookbehind on a backslash finds the dangerous form only.
    bad = []
    for m in re.finditer(r'(?<!\\)\\([ntr])', region):
        line_no = src[:src.find(region) + m.start()].count("\n") + 1
        snippet = region[max(0, m.start() - 45):m.start() + 25].replace("\n", "\\n")
        bad.append(f"L{line_no}: ...{snippet}...")

    check(
        "no Python-expandable \\n / \\t / \\r inside the JS template",
        not bad,
        "these become REAL control characters in the emitted JS and break it:\n      "
        + "\n      ".join(bad[:8]),
    )


# ── Guard 2: the emitted JS must actually parse ──────────────────────────────
def test_generated_js_parses():
    print("\n[2] OUTPUT guard — emitted <script> blocks")
    node = shutil.which("node")
    if not node:
        print("  ⚠ node not on PATH — skipping the authoritative syntax check.")
        print("    (install Node.js to enable it; the SOURCE guard still runs)")
        return

    from enzo.ui import dashboard as D

    with tempfile.TemporaryDirectory() as td:
        os.environ["ENZO_HOME"] = td  # never touch the real data directory
        try:
            res = D.generate_safe()
        except Exception as e:
            check("dashboard generated", False, f"{type(e).__name__}: {e}")
            return
        finally:
            os.environ.pop("ENZO_HOME", None)

    if not check("dashboard generated", bool(res.get("ok")), str(res.get("error"))):
        return
    html = open(res["path"], encoding="utf-8").read()
    check("dashboard HTML is non-trivial", len(html) > 10_000, f"{len(html)} bytes")

    blocks = [b for b in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S) if b.strip()]
    if not check("at least one <script> block found", bool(blocks), "nothing to validate"):
        return

    for i, block in enumerate(blocks):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(block)
            tmp = fh.name
        try:
            proc = subprocess.run([node, "--check", tmp], capture_output=True, text=True, timeout=60)
            err = (proc.stderr or "").strip()
            # Surface the offending line, which is the whole point of the guard.
            first = next((l for l in err.splitlines() if l.strip() and not l.startswith("node:")), "")
            check(f"<script> block {i} parses as valid JavaScript ({len(block)} chars)",
                  proc.returncode == 0, first[:300])
        finally:
            os.unlink(tmp)


# ── Guard 3: the interactive pieces the operator actually relies on ──────────
def test_activity_stream_wiring():
    print("\n[3] WIRING guard — activity stream + buttons")
    from enzo.ui import dashboard as D

    with tempfile.TemporaryDirectory() as td:
        os.environ["ENZO_HOME"] = td
        try:
            res = D.generate_safe()
        finally:
            os.environ.pop("ENZO_HOME", None)
    if not res.get("ok"):
        check("dashboard available for wiring check", False, str(res.get("error")))
        return
    html = open(res["path"], encoding="utf-8").read()

    check("container #activityFeedContainer exists", 'id="activityFeedContainer"' in html)
    check("JS targets #activityFeedContainer", "getElementById('activityFeedContainer')" in html)
    check("JS fetches /api/activity", "'/api/activity'" in html or '"/api/activity"' in html)
    check("JS renders activities", "renderActivities" in html)

    # Every id the script looks up should exist in the markup, otherwise the
    # handler silently no-ops (the `if (!el) return;` pattern) and the button
    # looks dead with no error anywhere.
    referenced = set(re.findall(r"getElementById\(['\"]([A-Za-z0-9_\-]+)['\"]\)", html))
    present = set(re.findall(r'id="([A-Za-z0-9_\-]+)"', html))

    # Ids that are LEGITIMATELY absent from the default markup:
    #   serverFault — a server-side banner injected only when the *previous*
    #                 render failed; the JS checks for it precisely to decide
    #                 whether to clear the client-side banner. Its absence on a
    #                 healthy page is the normal case, not a wiring bug.
    # Runtime-created ids would also belong here if any are added later.
    OPTIONAL_IDS = {"serverFault"}

    missing = sorted(referenced - present - OPTIONAL_IDS)
    check(
        "no getElementById target is missing from the markup",
        not missing,
        "these handlers would silently no-op: " + ", ".join(missing[:12]),
    )
    check("optional/conditional ids were the only absent ones",
          (referenced - present) <= OPTIONAL_IDS,
          "unexpected absent ids: " + ", ".join(sorted(referenced - present))[:200])



def test_axis_keys_match_backend():
    """The activity stream once printed Wallet: 0 / Dev: 0 for EVERY token.

    Cause: analyze.py writes the six axis scores under wallet_behavior /
    dev_behavior, while the dashboard JS read data.axes.wallet / data.axes.dev.
    `undefined || 0` is 0, so every card showed two dead-looking zeros and the
    operator could not tell a bad wallet from a missing key. This cross-checks
    the two files so the contract can never drift again.
    """
    print("\n=== المحاور: اللوحة تقرأ ما يكتبه المحلّل بالضبط ===")
    with open(os.path.join(REPO, "enzo", "analyzers", "analyze.py"), encoding="utf-8") as f:
        an = f.read()
    axes_block = re.search(r"axes\s*=\s*\{(.*?)\n    \}", an, re.S)
    backend = set(re.findall(r'"([a-z_]+)":', axes_block.group(1))) if axes_block else set()

    with open(os.path.join(REPO, "enzo", "ui", "dashboard.py"), encoding="utf-8") as f:
        dash = f.read()
    reads = set(re.findall(r"ax\('([a-z_]+)'\)", dash))
    reads |= set(re.findall(r"data\.axes\.([a-zA-Z_]+)", dash))

    check("المحلّل يكتب المحاور الستة", backend == {"security", "wallet_behavior",
          "dev_behavior", "momentum", "market_structure", "liquidity"}, str(sorted(backend)))
    missing = reads - backend
    check("كل مفتاح تقرأه اللوحة يكتبه المحلّل فعلاً", not missing, str(sorted(missing)))
    unread = backend - reads
    check("كل محور يكتبه المحلّل تعرضه اللوحة", not unread, str(sorted(unread)))
    check("لا بقايا قراءة نقطية قديمة (axes.wallet / axes.dev)",
          re.search(r"data\.axes\.(wallet|dev)\b", dash) is None)
    check("محور بلا بيانات يظهر n/a لا صفراً كاذباً", "'n/a'" in dash)

if __name__ == "__main__":
    print("=" * 68)
    print("  Dashboard JavaScript regression guard")
    print("=" * 68)
    for fn in (test_source_has_no_expandable_escapes,
               test_generated_js_parses,
               test_activity_stream_wiring,
               test_axis_keys_match_backend):
        try:
            fn()
        except Exception as e:
            check(f"{fn.__name__} did not raise", False, f"{type(e).__name__}: {e}")
    print("\n" + "=" * 68)
    print(f"  RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 68)
    sys.exit(1 if FAIL else 0)
