#!/usr/bin/env bash
# Regression test: build tests/rfmt_test, drive gdb through tests/session.gdb
# and check that every line of tests/expected.txt (plus expected.<debugger>.txt)
# shows up in the output.
#
#   tests/run.sh                 # gdb, current toolchain
#   DEBUGGER=lldb tests/run.sh   # lldb
#   tests/run.sh +1.84           # a specific rustup toolchain
#   RUSTFLAGS="-C symbol-mangling-version=v0" tests/run.sh
#   PROFILE=opt1 tests/run.sh    # optimized build (some values get optimized out)
set -euo pipefail
cd "$(dirname "$0")"

TOOLCHAIN="${1:-}"                       # e.g. +1.84, passed straight to cargo
PROFILE="${PROFILE:-dev}"
DEBUGGER="${DEBUGGER:-gdb}"              # gdb | lldb
TARGET_DIR="rfmt_test/target${TOOLCHAIN#+}${RUSTFLAGS:+-custom}"
OUT="$(mktemp)"
trap 'rm -f "$OUT"' EXIT

echo "== building rfmt_test ($(cargo ${TOOLCHAIN} --version), profile $PROFILE)"
cargo ${TOOLCHAIN} build -q --manifest-path rfmt_test/Cargo.toml --profile "$PROFILE" --target-dir "$TARGET_DIR" 2>&1 | grep -v '^warning' || true
case "$PROFILE" in dev) BIN="$TARGET_DIR/debug/rfmt_test" ;; *) BIN="$TARGET_DIR/$PROFILE/rfmt_test" ;; esac
[ -x "$BIN" ] || { echo "binary not found: $BIN"; exit 1; }

echo "== running $DEBUGGER"
case "$DEBUGGER" in
    gdb)  gdb -nx -q -batch -x session.gdb "$BIN" >"$OUT" 2>&1 || true ;;
    lldb) lldb --no-lldbinit --batch --source session.lldb -- "$BIN" >"$OUT" 2>&1 || true ;;
    *)    echo "unknown DEBUGGER=$DEBUGGER"; exit 2 ;;
esac

fail=0
while IFS= read -r line; do
    [ -z "$line" ] && continue
    if grep -qF -- "$line" "$OUT"; then
        printf '  ok    %s\n' "$line"
    else
        printf '  MISSING %s\n' "$line"
        fail=1
    fi
done < <(cat expected.txt "expected.$DEBUGGER.txt")

if grep -q "Traceback\|Python Exception" "$OUT"; then
    echo "  python exception in output"
    fail=1
fi

if [ "$fail" -ne 0 ]; then
    echo; echo "== FAILED, full $DEBUGGER output:"; cat "$OUT"; exit 1
fi
echo "== all expected lines present"
