# rust-debug-fmt

[![CI](https://github.com/hsqStephenZhang/rust-debug-fmt/actions/workflows/ci.yml/badge.svg)](https://github.com/hsqStephenZhang/rust-debug-fmt/actions/workflows/ci.yml)

Print Rust values in **gdb** and **lldb** exactly as `{:?}` / `{:#?}` would, by
calling the debuggee's own `core::fmt::Debug::fmt`.

```
(gdb) rlocals                      (lldb) rlocals
config = Config { name: "svc", retries: 3, tags: ["a", "b"] }
state  = Running { pid: 42, cfg: Config { name: "", retries: 0, tags: [] } }
scores = {"x": 1.5}
maybe  = None
```

A debugger's own Rust printing stops at the struct layout
(`alloc::string::String {vec: alloc::vec::Vec<u8, alloc::alloc::Global> {buf:
...`), and lldb without a Rust plugin shows even less. This extension instead
finds the monomorphized `<T as Debug>::fmt` that rustc already compiled into
your binary, runs it inside the debuggee with a `String` as the sink, reads the
text back and frees it. The output is therefore always what your program itself
would print: custom `Debug` impls, `HashMap`s, `Option`, enums, nested generics,
third-party types, everything.

The technique is borrowed from [BugStalker](https://github.com/godzie44/BugStalker)'s
`vard` / `argd` commands; this is a port of the idea to the Python APIs of gdb
and lldb, with the Rust knowledge shared between both.

## Demo

here is a clear comparison between gdb's default output and our output, where the default output even overflowed my terminal:

![demo](./assets/demo.png)

## Quick start

One line, then every `gdb` / `lldb` session on the machine has the commands:

```sh
curl -fsSL https://raw.githubusercontent.com/hsqStephenZhang/rust-debug-fmt/main/install.sh | sh
```

The installer clones into `~/.rust-debug-fmt`, detects which of gdb / lldb
you have, and adds a marked block to `~/.gdbinit` / `~/.lldbinit`. Run it
again to update; `sh ~/.rust-debug-fmt/install.sh --uninstall` removes
everything. Then, in any Rust project:

```sh
cargo build
gdb target/debug/your-bin          # or: lldb target/debug/your-bin
(gdb) break your_crate::main
(gdb) run
(gdb) rlocals                      # every local, as {:?}
(gdb) rprint some_var.field        # one expression
```

Nothing to add to the project. If a type is reported as missing its
`Debug::fmt`, see [Making `Debug::fmt` available](#making-debugfmt-available).

<details>
<summary>Manual install</summary>

Pure Python, no dependencies beyond the debugger's bundled interpreter.

```sh
git clone https://github.com/hsqStephenZhang/rust-debug-fmt ~/rust-debug-fmt

# gdb (Linux)
echo 'source ~/rust-debug-fmt/rust_debug_fmt_gdb.py' >> ~/.gdbinit

# lldb (Linux, macOS)
echo 'command script import ~/rust-debug-fmt/rust_debug_fmt_lldb.py' >> ~/.lldbinit
```

Both `gdb` / `rust-gdb` and `lldb` / `rust-lldb` pick the init files up. To
load for one session only, run the `source` / `command script import` line
inside the debugger.
</details>

| | requirement | tested |
|---|---|---|
| gdb | gdb ≥ 13 with Python 3 | gdb 15 (Ubuntu 24.04), gdb 17 |
| lldb | lldb ≥ 14 with Python 3 | lldb 18 (Ubuntu), 22 (Arch), Xcode lldb on macOS arm64 |
| rustc | ≥ 1.81, debuginfo on (default `cargo build`) | 1.84, 1.86, 1.89, stable, nightly; legacy and v0 mangling |
| platform | Linux x86-64, macOS arm64 | both in CI |

**macOS:** gdb does not work on modern macOS (no Apple Silicon support, code
signing), so use the lldb backend there. It is what the macOS CI job runs.

## Commands

| command | gdb | lldb | what it does |
|---|---|---|---|
| `rprint EXPR [EXPR ...]` | ✓ | ✓ | `{:?}` of each expression |
| `rprint/p EXPR`, `rprint -p EXPR` | ✓ | `-p` only | `{:#?}` (pretty, multi-line); lldb reserves the `/x` syntax |
| `rlocals [-p]` | ✓ | ✓ | every local of the selected frame (BugStalker's `vard locals`) |
| `rargs [-p]` | ✓ | ✓ | every argument of the selected frame (`argd all`) |
| `$rfmt(EXPR [, 1])` | ✓ | – | convenience function returning the text; for `printf`, `dprintf`, breakpoint conditions |
| `set rfmt-auto on` | ✓ | `rfmt-set auto on` | automatic mode: `print`/`frame variable`/dashboards use Debug::fmt (see below) |
| `set rfmt-verbose on` | ✓ | `rfmt-set verbose on` | log which symbol was picked and what was called |
| `set rfmt-scheduler-lock off` | ✓ | `rfmt-set scheduler-lock off` | let other threads run during the call (default: only the current thread) |
| | | `rfmt-set timeout 60` | expression timeout in seconds (lldb, default 30) |

Expressions are the debugger's own: Rust syntax in gdb; in lldb a variable path
(`a`, `a.b`, `*p`, `arr[2]`) or a C expression. `rlocals` / `rargs` fall back to
the debugger's native printer for a variable whose `Debug::fmt` is not
available and say why.

```
(gdb) rprint cfg.tags map[0] *boxed
(gdb) printf "state = %s\n", $rfmt(state)
(gdb) dprintf worker.rs:88, "job = %s\n", $rfmt(job)

(lldb) rprint -p state
(lldb) rargs
```

## Automatic mode: `print`, `frame variable`, dashboards, IDEs

Off by default. Turn it on and the debugger's *own* commands show Debug output
for every aggregate that has a `Debug::fmt`, while scalars, pointers and types
without one keep their native display:

```
(gdb) set rfmt-auto on                 (lldb) rfmt-set auto on
(gdb) print state                      (lldb) v state
$1 = Running { pid: 42, cfg: Config { name: "", retries: 0, tags: [] } }
(gdb) info locals                      (lldb) frame variable
```

This covers gdb-dashboard's Variables/Expressions panels, `display`, `finish`'s
"Value returned", lldb's `p` / `v` / `frame variable`, and IDE variable views
that sit on top of them (VS Code with CodeLLDB or the native lldb adapter).
In gdb it is a pretty printer installed ahead of the rust-gdb ones; in lldb a
type summary in the category `rust-debug-fmt`. Put the `set` line in your
init file to have it always.

| setting | gdb | lldb |
|---|---|---|
| enable | `set rfmt-auto on` | `rfmt-set auto on` |
| `{:#?}` instead of `{:?}` | `set rfmt-auto-pretty on` (`auto` follows `set print pretty`) | `rfmt-set pretty on` |

Keep in mind what it costs: every value shown runs `Debug::fmt` inside your
process, so a dashboard that refreshes all locals on each `step` performs one
inferior call per local. The reentrancy guard makes sure our own calls never
trigger the printer recursively, and anything that fails falls back to the
native display.

### VS Code with CodeLLDB

CodeLLDB bundles its own lldb + Python; nothing else to install. Add to
`launch.json` (or, for every session, to `settings.json` under
`lldb.launch.initCommands` / `lldb.launch.postRunCommands`):

```jsonc
{
  "type": "lldb",
  "request": "launch",
  "name": "my-bin",
  "cargo": { "args": ["build", "--bin=my-bin"] },
  "sourceLanguages": ["rust"],
  "initCommands": ["command script import ~/.rust-debug-fmt/rust_debug_fmt_lldb.py"],
  "postRunCommands": ["rfmt-set auto on"]
}
```

Now the Variables panel, Watch, hover and the Debug Console (`rprint x`,
`v x`) all show Debug output. `rfmt-set auto on` goes into `postRunCommands`
on purpose: CodeLLDB loads the Rust toolchain's formatters when it creates the
target, and lldb gives precedence to the most recently enabled formatter
category. A stop-hook installed by `rfmt-set auto on` re-asserts our
precedence at every stop, so the order only matters for the very first stop.
`examples/demo/.vscode/launch.json` is a complete example.

## Making `Debug::fmt` available

rustc only monomorphizes what the program uses, and the linker drops unused
std code. So `<T as Debug>::fmt` exists in the binary only if the program
formats a `T` with `{:?}` somewhere. If it does not you get:

```
no `<demo::Thing as core::fmt::Debug>::fmt` in the binary; the program has to
format this type with {:?} somewhere for rustc to emit it
```

Fix it without printing anything: take the function pointer once, in a
debug-only hook. Type-only, no values needed.

```rust
#[cfg(debug_assertions)]
fn keep_debug_impls() {
    use std::fmt::{Debug, Formatter, Result};
    macro_rules! keep {
        ($($t:ty),*) => { $( std::hint::black_box(
            <$t as Debug>::fmt as fn(&$t, &mut Formatter) -> Result); )* }
    }
    keep!(Thing, Vec<Thing>, Option<Config>, HashMap<String, f64>);
}

fn main() {
    #[cfg(debug_assertions)]
    keep_debug_impls();
    // ...
}
```

`examples/demo` shows this end to end. Primitive scalars (integers, floats,
`bool`, `char`, `()`) never need this: their `Debug` impls are usually not
linked in, so the extension formats them locally.

## Limitations

- **Values that are optimized out** cannot be printed. Values that live in
  registers are copied into the debuggee first, so they work.
- **Raw pointers print the pointee.** Both debuggers spell `&T`, `*const T`
  and `*mut T` the same way, so the reference impl is preferred; a real `{:?}`
  of a raw pointer would print the address.
- **It runs real code in your process.** `Debug::fmt` allocates through the
  global allocator and may take locks. If you are stopped inside the allocator
  (or the type's `Debug` impl needs a lock another, now-stopped, thread holds)
  the call can deadlock; interrupt it and the debugger unwinds. Same caveat as
  BugStalker's `vard`, or `call` / `expr` in general.
- **Side effects of `Debug` impls happen.** Almost all are pure, but a `Debug`
  impl that logs or mutates would do so.
- **lldb spells types the C way** (`demo::Config *`, `unsigned int`) in its
  messages, because it has no Rust language plugin. The printed values are
  unaffected: they come from your program.
- The first command in a session indexes every `fmt` function once. On a
  100 MB debug binary with ~8000 `fmt` functions that takes about 3 s in gdb;
  afterwards it is cached.

## How it works, in one paragraph

Every function named `fmt` is listed with its DWARF signature; the one for `T`
has the shape `(&T, &mut core::fmt::Formatter)`. Its demangled linkage name
tells `Debug` from `Display`. A `String::new()` header, a hand-built
`<String as core::fmt::Write>` vtable and a `core::fmt::Formatter` are written
to scratch memory in the debuggee (memory lldb allocates, or the area below the
stack pointer in gdb); `Formatter` and `String` field offsets come from DWARF,
so one code path covers rustc 1.81 to current. The debugger then performs an
inferior call with crash unwinding enabled, `cap`/`ptr`/`len` are read back,
the bytes decoded, and `drop_in_place::<String>` frees the heap buffer.
Details, including the debugger quirks discovered along the way, are in
[docs/how-it-works.md](docs/how-it-works.md).

## Layout

```
rust_debug_fmt_gdb.py       entry point for gdb   (source ...)
rust_debug_fmt_lldb.py      entry point for lldb  (command script import ...)
rustdebugfmt/core.py        debugger-independent: symbol names, layouts, vtable,
                            candidate selection, the call sequence
rustdebugfmt/gdb_backend.py gdb Backend + commands
rustdebugfmt/lldb_backend.py lldb Backend + commands
tests/                      regression program, gdb/lldb sessions, expected output
examples/demo/              minimal project showing the keep_debug_impls hook
```

## Tests

```sh
tests/run.sh                        # gdb, current toolchain
DEBUGGER=lldb tests/run.sh          # lldb
tests/run.sh +1.84                  # any rustup toolchain
RUSTFLAGS="-C symbol-mangling-version=v0" tests/run.sh
PROFILE=opt1 tests/run.sh           # optimized build; lines for optimized-out values are expected to be MISSING
```

`tests/rfmt_test` covers structs, generics, enums, `Option`, `Result`, `Vec`,
`HashMap`, `Rc`, `Arc<Mutex<_>>`, tuples, arrays (direct and via `[T]`), `u128`,
`char`, `&str`, `String`, `Box`, references, raw pointers, a `Display`-only
type, a type without `Debug`, a `Debug` impl that returns `Err`, and a second
thread running while the call happens. CI runs it on Linux (gdb and lldb,
stable / 1.84 / nightly, legacy and v0 mangling) and on macOS (lldb).

## Credits

The approach is BugStalker's (`src/debugger/call/fmt.rs` there); this project
re-implements it on top of the inferior-call machinery of gdb and lldb.
