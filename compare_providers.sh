#!/usr/bin/env bash
# Full protocol: baseline + --instrument-tools per provider, then the 4
# comparisons that matter (baseline anthropic vs openai, instrumented
# anthropic vs openai, and the before/after contrast for each provider).
#
# 4 real API calls in total (2 providers x 2 modes) - not 2.
#
# Prerequisites: .venv active, .env loaded (ANTHROPIC_API_KEY / OPENAI_API_KEY).
#
# Usage:
#   ./compare_providers.sh          # full protocol
#   ./compare_providers.sh --tree   # + print the span tree on each run
#
# Do not pass --dump or --instrument-tools as arguments: the script handles
# them itself to produce the filenames the comparisons expect.
#
# All output is also written to full_report.txt (overwritten on each run), so
# it can be re-read or shared without copying everything by hand.

set -euo pipefail

OUTFILE="full_report.txt"

preflight() {
    if ! command -v python >/dev/null 2>&1; then
        echo "python not found. Activate the virtualenv first:" >&2
        echo "  source .venv/bin/activate" >&2
        exit 1
    fi
    if ! python -c "import opentelemetry.sdk" >/dev/null 2>&1; then
        echo "opentelemetry SDK not importable by '$(command -v python)'." >&2
        echo "Activate the virtualenv and install dependencies:" >&2
        echo "  source .venv/bin/activate && pip install -r requirements.txt" >&2
        exit 1
    fi
    if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        echo "ANTHROPIC_API_KEY not set. Load your .env first:" >&2
        echo "  set -a && source .env && set +a" >&2
        exit 1
    fi
    if [ -z "${OPENAI_API_KEY:-}" ]; then
        echo "OPENAI_API_KEY not set. Load your .env first:" >&2
        echo "  set -a && source .env && set +a" >&2
        exit 1
    fi
}

run_protocol() {
    echo "=== 1/4: anthropic (baseline) ==="
    python agent_bench.py --provider anthropic --dump anthropic.json.new "$@" || return 1

    echo
    echo "=== 2/4: anthropic (--instrument-tools) ==="
    python agent_bench.py --provider anthropic --instrument-tools --dump anthropic_instr.json.new "$@" || return 1

    echo
    echo "=== 3/4: openai (baseline) ==="
    python agent_bench.py --provider openai --dump openai.json.new "$@" || return 1

    echo
    echo "=== 4/4: openai (--instrument-tools) ==="
    python agent_bench.py --provider openai --instrument-tools --dump openai_instr.json.new "$@" || return 1

    echo
    echo "=== Comparison 1/4: anthropic vs openai (baseline) ==="
    python compare.py anthropic.json.new openai.json.new || return 1

    echo
    echo "=== Comparison 2/4: anthropic vs openai (--instrument-tools) ==="
    python compare.py anthropic_instr.json.new openai_instr.json.new || return 1

    echo
    echo "=== Comparison 3/4: anthropic before/after --instrument-tools ==="
    python compare.py anthropic.json.new anthropic_instr.json.new || return 1

    echo
    echo "=== Comparison 4/4: openai before/after --instrument-tools ==="
    python compare.py openai.json.new openai_instr.json.new || return 1
}

# Nothing is published until the whole protocol succeeds: a run that dies
# halfway must not leave half-new, half-old evidence behind.
preflight

PARTIAL="${OUTFILE}.partial"
set +e
run_protocol "$@" 2>&1 | tee "$PARTIAL"
status="${PIPESTATUS[0]}"
set -e

if [ "$status" -eq 0 ]; then
    for f in anthropic anthropic_instr openai openai_instr; do
        mv "${f}.json.new" "${f}.json"
    done
    mv "$PARTIAL" "$OUTFILE"
    echo
    echo "Full report written to ${OUTFILE}"
else
    echo >&2
    echo "Run failed (exit ${status}). Committed dumps and ${OUTFILE} left untouched." >&2
    echo "Partial output kept in ${PARTIAL}, partial dumps in *.json.new" >&2
fi

exit "$status"
