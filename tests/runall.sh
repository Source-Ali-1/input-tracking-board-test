#!/usr/bin/env bash
# Runs every simulation: the real apps-script/Code.gs against a mock sheet, synced with a local
# test board. Prints one line per test group and the total. Takes about 12 minutes.
T="$(cd "$(dirname "$0")" && pwd)"
cd "$T" && export PYTHONIOENCODING=utf-8
PY="${PYTHON:-}"
if [ -z "$PY" ]; then # the first Python that really runs (Windows has a do-nothing "python3" stub)
  for c in python3 python py; do "$c" -c "import sys" >/dev/null 2>&1 && { PY="$c"; break; }; done
fi
python() { command "$PY" "$@"; }
export -f python 2>/dev/null; export PY

run() { # name, command, DATE_LABELS
  mkdir -p .run; : > .run/board.log
  DATE_LABELS=$3 HEADER_ARRAY=$HEADER_ARRAY bash restart.sh
  out=$(eval "$2" 2>&1)
  p=$(echo "$out" | grep -c "^PASS"); f=$(echo "$out" | grep -c "^FAIL ")
  if [ "$f" = 0 ] && [ "$p" -gt 0 ]; then
    printf "%-32s passes         (%s checks)\n" "$1" "$p"
  else
    printf "%-32s DOESN'T PASS   (%s of %s checks failed)\n" "$1" "$f" "$((p+f))"
    echo "$out" | grep "^FAIL \|Traceback\|Error" | sed 's/^/      /'
  fi
  TP=$((TP+p)); TF=$((TF+f))
}

TP=0; TF=0
run "Main scenarios"                 "python scenarios.py restart.sh" ""
run "Main scenarios with dates"      "python scenarios.py restart.sh" "1"
run "Edge cases"                     "python edges.py" ""
run "Stale board page"               "python stale.py" ""
run "Late trigger race"              "python race.py" ""
run "Rows added by hand"             "python newrow.py" ""
run "Person added in sheet"          "python newperson.py" ""
run "Speed and missed pushes"        "python latency.py" ""
run "Edit without trigger"           "python untracked.py | sed 's/^board value reached sheet: True/PASS board->sheet/; s/^untracked sheet edit reached board via catch-up: True | sheet still 33: True/PASS untracked sheet edit caught up/'" ""
HEADER_ARRAY=1 run "Week total / Win-Loss formulas" "python formulas.py" ""
run "Headings vs inputs"             "python headings.py" ""
run "Week added by hand"             "python manualweek.py proper | grep -q \"'Mon 10/26'\" && echo PASS proper-format week kept, no duplicate" ""
run "Week added by hand, odd format" "python manualweek.py odd | grep -q \"'Monday 26 Oct'\" && echo PASS odd-format week not overwritten" ""
bash restart.sh stopall
echo "TOTAL: $TP passed, $TF failed"
[ "$TF" = 0 ]
