#!/bin/sh
# rust-debug-fmt installer.
#
#   curl -fsSL https://raw.githubusercontent.com/hsqStephenZhang/rust-debug-fmt/main/install.sh | sh
#
# What it does:
#   1. clones (or updates) the repository into $RDF_DIR   (default ~/.rust-debug-fmt)
#   2. adds a marked block to ~/.gdbinit  if gdb  is installed  (skip with RDF_NO_GDB=1)
#   3. adds a marked block to ~/.lldbinit if lldb is installed  (skip with RDF_NO_LLDB=1)
# Running it again only updates; the init-file blocks are never duplicated.
#
#   sh install.sh --uninstall      removes the init-file blocks and $RDF_DIR
#
# Environment: RDF_DIR, RDF_REPO (git url), RDF_REF (branch/tag, default main),
#              RDF_NO_GDB=1, RDF_NO_LLDB=1, RDF_FORCE_GDB=1, RDF_FORCE_LLDB=1
set -eu

RDF_DIR="${RDF_DIR:-$HOME/.rust-debug-fmt}"
RDF_REPO="${RDF_REPO:-https://github.com/hsqStephenZhang/rust-debug-fmt.git}"
RDF_REF="${RDF_REF:-main}"
BEGIN="# >>> rust-debug-fmt >>>"
END="# <<< rust-debug-fmt <<<"

say()  { printf '%s\n' "$*"; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# remove our marked block from an init file (no-op if absent)
strip_block() {
    f="$1"
    [ -f "$f" ] || return 0
    grep -qF "$BEGIN" "$f" || return 0
    tmp="$f.rdf.tmp"
    awk -v b="$BEGIN" -v e="$END" '
        $0 == b { skip = 1; next }
        $0 == e { skip = 0; next }
        !skip   { print }
    ' "$f" >"$tmp"
    mv "$tmp" "$f"
    # drop the file if we were its only content
    [ -s "$f" ] || rm -f "$f"
}

# append our marked block with the given source line
add_block() {
    f="$1"; line="$2"
    strip_block "$f"
    [ -f "$f" ] && [ -s "$f" ] && [ "$(tail -c1 "$f" | od -An -c | tr -d ' ')" != '\n' ] && printf '\n' >>"$f"
    {
        printf '%s\n' "$BEGIN"
        printf '# rprint / rlocals / rargs: print Rust values via the debuggee'"'"'s own Debug impls\n'
        printf '%s\n' "$line"
        printf '%s\n' "$END"
    } >>"$f"
    say "  updated $f"
}

if [ "${1:-}" = "--uninstall" ]; then
    strip_block "$HOME/.gdbinit";  say "  cleaned ~/.gdbinit"
    strip_block "$HOME/.lldbinit"; say "  cleaned ~/.lldbinit"
    if [ -d "$RDF_DIR" ]; then rm -rf "$RDF_DIR"; say "  removed $RDF_DIR"; fi
    say "rust-debug-fmt uninstalled."
    exit 0
fi

# ---- 1. fetch ------------------------------------------------------------
say "rust-debug-fmt installer"
if [ -d "$RDF_DIR/.git" ]; then
    say "  updating $RDF_DIR"
    git -C "$RDF_DIR" fetch -q origin "$RDF_REF"
    git -C "$RDF_DIR" checkout -q "$RDF_REF" 2>/dev/null || true
    git -C "$RDF_DIR" reset -q --hard "origin/$RDF_REF"
elif [ -f "$RDF_DIR/rust_debug_fmt_gdb.py" ]; then
    say "  using existing checkout at $RDF_DIR (not a git clone, left untouched)"
elif have git; then
    say "  cloning into $RDF_DIR"
    git clone -q --depth 1 --branch "$RDF_REF" "$RDF_REPO" "$RDF_DIR"
elif have curl && have tar; then
    say "  downloading tarball into $RDF_DIR (git not found)"
    tarball_url="$(printf '%s' "$RDF_REPO" | sed 's/\.git$//')/archive/refs/heads/$RDF_REF.tar.gz"
    mkdir -p "$RDF_DIR"
    curl -fsSL "$tarball_url" | tar -xz -C "$RDF_DIR" --strip-components=1
else
    die "need git, or curl + tar"
fi
[ -f "$RDF_DIR/rust_debug_fmt_gdb.py" ] || die "download failed: $RDF_DIR/rust_debug_fmt_gdb.py missing"

# ---- 2. init files -------------------------------------------------------
installed=""
if [ "${RDF_NO_GDB:-}" != 1 ] && { have gdb || [ "${RDF_FORCE_GDB:-}" = 1 ]; }; then
    add_block "$HOME/.gdbinit" "source $RDF_DIR/rust_debug_fmt_gdb.py"
    installed="$installed gdb"
fi
if [ "${RDF_NO_LLDB:-}" != 1 ] && { have lldb || [ "${RDF_FORCE_LLDB:-}" = 1 ]; }; then
    add_block "$HOME/.lldbinit" "command script import $RDF_DIR/rust_debug_fmt_lldb.py"
    installed="$installed lldb"
fi

# ---- 3. report -----------------------------------------------------------
if [ -z "$installed" ]; then
    say ""
    say "  neither gdb nor lldb found in PATH, so no init file was changed."
    say "  The files are in $RDF_DIR; load them by hand with"
    say "      (gdb)  source $RDF_DIR/rust_debug_fmt_gdb.py"
    say "      (lldb) command script import $RDF_DIR/rust_debug_fmt_lldb.py"
    say "  or rerun with RDF_FORCE_GDB=1 / RDF_FORCE_LLDB=1."
    exit 0
fi

say ""
say "Done. Enabled for:$installed"
say ""
say "Try it: build any Rust program with debuginfo (plain 'cargo build'), then"
for d in $installed; do
    case "$d" in
        gdb)  say "    gdb  target/debug/your-bin      ->  break main, run, rlocals   (rprint EXPR, rprint/p EXPR, rargs)";;
        lldb) say "    lldb target/debug/your-bin      ->  b main, run, rlocals       (rprint EXPR, rprint -p EXPR, rargs)";;
    esac
done
say ""
say "A type prints only if the program formats it with {:?} somewhere; see"
say "    $RDF_DIR/README.md  (section 'Making Debug::fmt available')"
say "Uninstall:  sh $RDF_DIR/install.sh --uninstall"
