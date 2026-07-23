#!/bin/bash
# predeploy.sh — P5.19 (iss_a7a07ee3): the gate every deploy runs BEFORE going
# live (home restarts AND the ETFminer deploy). Three legs, all must pass:
#   1. engine test suite  (BKT/gate/scheduling/placement/auth + packver + relearn)
#   2. pack validation    (schema gate over every live topic pack)
#   3. app boot smoke     (imports resolve; FastAPI app constructs)
# Exit 0 = deployable. Nonzero = the first failing leg's output tells you why.
set -uo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

echo "── predeploy gate ──────────────────────────────────────────"
echo "[1/3] engine tests"
"$PY" -m pytest tests/ -q || { echo "✗ GATE FAILED: engine tests"; exit 1; }

echo "[2/3] pack validation ($(ls topics/*.json | grep -vc _roadmap) packs)"
fail=0
for f in topics/*.json; do
  case "$f" in */_roadmap.json) continue;; esac
  "$PY" validate_pack.py "$f" > /dev/null 2>&1 || { echo "✗ $f"; fail=1; }
done
[ "$fail" -eq 0 ] || { echo "✗ GATE FAILED: pack validation"; exit 1; }

echo "[3/3] app boot smoke"
LEARN_DB=/tmp/learn_predeploy_smoke.db "$PY" -c "
import app
assert app.app.title == 'Learn'
assert len(app.load_topics()) >= 1
print('   boot OK —', len(app.load_topics()), 'topics load')" \
  || { echo "✗ GATE FAILED: boot smoke"; exit 1; }
rm -f /tmp/learn_predeploy_smoke.db

echo "── ✓ GATE PASSED — deployable ──────────────────────────────"
