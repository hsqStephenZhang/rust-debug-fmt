# How it works

This document explains the pipeline, the rustc/std details it relies on, how
the code is split between the debugger-independent core and the gdb / lldb
backends, the debugger behaviour that shaped the design, and how it differs
from BugStalker's implementation of the same idea.

## The idea

A Rust binary built with `cargo build` already contains, for every type `T`
the program formats with `{:?}`, a compiled function

```
<T as core::fmt::Debug>::fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result
```

`Formatter` writes into a `&mut dyn core::fmt::Write`. `String` implements
`Write`. So if the debugger can

1. find the address of that function,
2. build a `String` header, a `<String as Write>` vtable and a `Formatter`
   in the debuggee's memory,
3. call the function with `&value` and `&mut formatter`,
4. read the `String`'s buffer back and free it,

the result is exactly what the program itself would print. No type knowledge
in the debugger, no pretty printers to maintain.

## Architecture

```
rustdebugfmt/core.py           what is Rust        (no gdb / lldb imports)
rustdebugfmt/gdb_backend.py    what is gdb         implements core.Backend, defines the commands
rustdebugfmt/lldb_backend.py   what is lldb        implements core.Backend, defines the commands
rust_debug_fmt_gdb.py          `source` entry point: fixes sys.path, registers commands
rust_debug_fmt_lldb.py         `command script import` entry point, same for lldb
```

`core.debug_format(backend, value_info, pretty)` is the whole call sequence.
It only talks to a `Backend` object through a small interface:

| method | purpose |
|---|---|
| `ptr_size()`, `byteorder()` | target properties |
| `ptr_name(T, depth)`, `array_ptr_names(E, N)`, `slice_names(E)` | how this debugger spells `&T`, `&[E; N]`, `&[E]` |
| `functions_named(suffix)` | every function named `…::suffix`, with parameter type names (as `FmtFn`, lazily resolved) |
| `function_addr(names)` | address of a symbol by canonical demangled name |
| `formatter_layout(fn)`, `string_layout()` | field offsets of `core::fmt::Formatter` (from `fn`'s second parameter) and `alloc::string::String`, as `TypeLayout` |
| `read_memory`, `write_memory` | inferior memory |
| `scratch(size)` | context manager yielding writable scratch memory in the debuggee |
| `call_environment()` | context manager active around the calls |
| `call(fn, addr, args, ret_size)` | the inferior call; `args` are `Arg.int`, `Arg.fat_at` (two words at an address, by value) or `Arg.native` |

The backend also converts its own value object into a `ValueInfo` (type name,
size, kind, address or raw bytes, optimized-out flag, array element type). The
core never sees a `gdb.Value` or `SBValue`.

Everything Rust-specific lives in the core: canonical symbol names,
`normalize_demangled()`, the `Formatter` flag bits, the `String` header,
vtable assembly, the ranking of candidate `fmt` functions, and the scalar
fallback.

## Pipeline

### 1. Finding the monomorphized `fmt`

The debugger lists every function named `fmt` **with its parameter types**.
The one for `T` has first parameter `&T`, which the debugger spells as
`*mut T` (gdb) or `T *` (lldb). `str(value.type)` on the variable uses the same
spelling, so a string comparison is enough.

The name alone does not say which trait: gdb names impls from the DWARF
structure (`demo::{impl#3}::fmt`), lldb shows the demangled linkage name but
for legacy-mangled binaries with the `$LT$`/`$u20$` escapes still in place.
`core.normalize_demangled()` brings gdb's `demangle -l rust` output, LLVM's v0
demangler output and Itanium-demangled legacy names to one canonical form,
e.g. `<demo::Config as core::fmt::Debug>::fmt`, and the trait check is a
substring test for `core::fmt::Debug`.

Candidates are ranked, best first:

| first parameter | how the value is passed |
|---|---|
| `&T` | address of the value |
| `T` | the value itself, by value (fat pointers such as `&str`, or a pointer) |
| `&&T` | `<&T as Debug>::fmt`: the address is written to a slot and the slot's address is passed |
| `&[E]` for a `[E; N]` array | `<[E] as Debug>::fmt`: a `{ptr, len}` fat pointer is assembled and passed by value |

Among equally ranked candidates a `<&T …>` impl beats a `<*const T …>` /
`<*mut T …>` one, because the debuggers spell all three pointer kinds the same
and the reference impl (prints the pointee) is the useful one.

If nothing matches and the type is a primitive scalar, the value is formatted
locally. `<u64 as Debug>::fmt` and friends live in libcore and are dropped by
the linker unless something uses them, so this is the common case for scalars.

**gdb specifics.** `info functions -q -n ::fmt` prints one line per function
with a full signature; the listing is parsed (respecting nested `<>`), and for
a matching candidate `gdb.lookup_static_symbols(name)` gives the `gdb.Symbol`
whose `.linkage_name` is demangled with the `demangle` command. Symbol lookups
are lazy because only a handful of the thousands of `fmt` functions ever match.

**lldb specifics.** `target.FindGlobalFunctions(regex, 0, eMatchTypeRegex)`
with `(^|::)fmt(<.*>)?(::h[0-9a-f]{16})?$` (the hash suffix is what legacy
names carry). Each `SBSymbolContext` yields an `SBFunction`, sometimes only
after `ResolveSymbolContextForAddress`; its `GetType().GetFunctionArgumentTypes()`
gives the parameter spellings. lldb has no Rust plugin, so primitives are
spelled the C way (`unsigned int`, `unsigned short (*)[3]` for `&[u16; 3]`);
a small C-to-Rust table is used only to build `&[E]` slice names.

### 2. The `<String as core::fmt::Write>` vtable

A Rust vtable is `[drop_in_place, size, align, method…]` with methods in trait
declaration order: `write_str`, `write_char`, `write_fmt`. The entries are
resolved by canonical demangled name, with a fallback that looks for a function
of that name whose first parameter is `&String`:

| slot | symbol |
|---|---|
| drop_in_place | `core::ptr::drop_in_place<alloc::string::String>` (rustc < 1.97) or `core::ptr::drop_glue<alloc::string::String>` |
| size / align | from the DWARF `String` type, fallback 3 pointers / pointer size |
| write_str | `<alloc::string::String as core::fmt::Write>::write_str` |
| write_char | `<alloc::string::String as core::fmt::Write>::write_char` |
| write_fmt | `<alloc::string::String as core::fmt::Write>::write_fmt` or `core::fmt::Write::write_fmt<alloc::string::String>` |

**Why not use the real vtable?** rustc does emit a DWARF variable
`<alloc::string::String as core::fmt::Write>::{vtable}`, and gdb finds it. But
with legacy mangling the vtable global is an anonymous LLVM constant, the
DWARF location does not resolve, and gdb reports the objfile base address for
it (0x555555554000). Calling through it jumps into the ELF header. Hand-building
the vtable is the only reliable option.

### 3. `Formatter` and `String` layout from DWARF

Every derived `Debug::fmt` takes `&mut core::fmt::Formatter`, so the user's
binary always carries the full DWARF description of `Formatter`. The backend
turns the second parameter's pointee type into a `TypeLayout` (name, size,
fields with offsets, nested), and the core fills it by field name:

| rustc | `Formatter` fields | what is written |
|---|---|---|
| 1.87+ | `buf: &mut dyn Write {pointer, vtable}`, `options: FormattingOptions {flags: u32, width: u16, precision: u16}` | `flags = ' ' \| ALIGN_UNKNOWN(3<<29) \| ALWAYS_SET(1<<31) [\| ALTERNATE(1<<23)]`, width/precision 0 |
| 1.85 – 1.87 | `options: {flags: u32, fill: char, align: Option<Alignment>, width: Option<usize>, precision: Option<usize>}`, `buf` | `flags = 0 [\| 1<<2 for alternate]`, `fill = ' '`, `align = 3` (None niche), width/precision zero bytes (= None) |
| 1.81 – 1.85 | `flags, fill, align: Alignment, width, precision` directly in `Formatter`, plus `buf` | same values, `align = 3` is `Alignment::Unknown` |

Only the *meaning of the bits* is hardcoded; offsets and sizes come from
DWARF. The layout generation is detected by whether a `fill` field exists.
Alternate (`{:#?}`) sets the `ALTERNATE` bit in the respective encoding.

`String` is `Vec<u8>` is `RawVec + len`; the leaf offsets of `cap`, `pointer`
and `len` are found by walking the type recursively, with a `(0, 8, 16)`
fallback. `String::new()` is written as `cap = 0, ptr = 1 (dangling, align of
u8), len = 0`. gdb gets the type from a `fmt`/`drop_in_place` parameter (its
`lookup_type("alloc::string::String")` returns a *namespace*), lldb from
`target.FindFirstType`.

### 4. Scratch memory

Everything (String header, vtable, Formatter, an indirection slot, and a copy
of the value if it has no address) goes into a scratch area in the debuggee.

- **lldb:** `SBProcess.AllocateMemory()` / `DeallocateMemory()`; if that fails,
  the stack pointer register is moved down and memory below the old `sp` is
  used.
- **gdb:** a few hundred bytes below the **innermost** frame's stack pointer.
  `$sp` is set to the base of that area before the call so gdb's dummy frame
  and the callee's frames land below it, and restored afterwards. No `malloc`
  symbol needed, no dependence on the allocator's state.

### 5. The call

- **gdb:** `gdb.Symbol.value()(arg0, arg1)` performs the inferior call using
  the DWARF prototype, so gdb does the ABI work (register assignment,
  struct-by-value classification for fat pointers, return value). Functions
  without a prototype are called through a C cast (`((unsigned char(*)(unsigned
  long,…))addr)(…)`) with the language temporarily set to C. For the duration
  `unwind-on-signal on` (a crash inside `fmt` unwinds back) and
  `scheduler-locking on` (only the current thread runs, mirroring BugStalker)
  are set and later restored.
- **lldb:** `SBFrame.EvaluateExpression` of the same C cast, with
  `SBExpressionOptions`: language C, `SetUnwindOnError(True)`,
  `SetIgnoreBreakpoints(True)`, `SetTryAllThreads(False)`, a timeout. lldb has
  no Rust expression evaluator, but a C function-pointer cast needs none. A
  fat pointer passed by value becomes two integer arguments, which is how both
  the x86-64 SysV and the AArch64 ABIs pass a two-word struct.

The `fmt::Result` return value is inspected (`Err` is reported together with
any partial output the impl managed to write).

### 6. Read back and clean up

`cap`, `ptr`, `len` are read from the header (`len <= cap` is checked), `len`
bytes are read from `ptr` and decoded as UTF-8. If `cap > 0` the heap buffer
is released by calling `drop_in_place::<String>` on the header.

## Automatic mode

`set rfmt-auto on` (gdb) / `rfmt-set auto on` (lldb) plugs the same pipeline
into the debugger's own value display.

- **gdb:** a pretty-printer lookup function is inserted at index 0 of
  `gdb.pretty_printers` *and* of every objfile's `pretty_printers` list (also
  for objfiles loaded later, via `new_objfile`). Objfile printers are consulted
  before global ones and rust-gdb registers there, so being first in those lists
  is what makes us win. The lookup claims a value only if the process is
  stopped, the type is a struct / union / enum / array, and `choose_debug_fmt`
  finds a candidate (cached per type name); everything else returns `None` and
  gdb continues down the chain. `to_string()` runs `debug_format`, or
  `format_string(raw=True)` if that fails.
- **lldb:** a Python summary provider registered with `type summary add -x
  '^.*$'` in the category `rust-debug-fmt`; enabling / disabling the category
  is the switch. Returning `""` makes lldb fall back to its normal display
  (returning `None` would print the word "None"), so declining is invisible.
  Pointers, builtins and unsupported aggregates decline.
- **Category precedence (lldb):** the Rust toolchain's formatters
  (`lldb_commands`, loaded by `rust-lldb` and by CodeLLDB when
  `sourceLanguages` contains `rust`) register a catch-all summary and
  synthetic provider in the `Rust` category, and lldb consults the most
  recently *enabled* category first, an empty summary from a higher category
  still counts as handled. So `rfmt-set auto on` also installs a target
  stop-hook running `type category enable rust-debug-fmt`, which moves our
  category back in front at every stop regardless of load order. Without a
  target (e.g. in `initCommands` or `~/.lldbinit`) the hook cannot be created
  yet, hence the recommendation to run `rfmt-set auto on` in
  `postRunCommands`.
- **Reentrancy:** a `debug_format` in flight can make the debugger print
  values (frame lines when the callee stops, fallbacks calling
  `GetSummary()`). A guard flag makes the printer / summary decline while one
  of our calls is running, so there is never a nested inferior call.
- **Formatting:** `{:?}` by default. gdb: `set rfmt-auto-pretty on|off|auto`
  (`auto` follows `set print pretty`; note gdb-dashboard turns `print pretty`
  on and then squashes newlines, which is why the default is `off`). lldb:
  `rfmt-set pretty on`.

## Differences from BugStalker

| | BugStalker (`src/debugger/call/fmt.rs`) | this project |
|---|---|---|
| finding `fmt` | linkage-name templates plus `DW_AT_name` patterns, separate code paths for structs, enums, arrays, `&str` | one rule: signature `(&T, &mut Formatter)` + demangled linkage name contains `core::fmt::Debug` |
| `Formatter` layout | three hardcoded struct definitions selected by rustc version | field offsets from DWARF, only flag semantics hardcoded |
| `String` layout | hardcoded `cap, ptr, len` | from DWARF, hardcoded fallback |
| vtable `write_fmt` | borrows `write_fmt<Adapter<StdoutLock>>` | uses `write_fmt<String>` itself |
| temporary memory | injects an `mmap` syscall | lldb: `AllocateMemory`; gdb: stack below `$sp` |
| the call | own x86-64 trampoline (`call rax; int3`), manual register setup | the debugger's inferior call, any architecture it supports |
| crash handling | expects `SIGTRAP`, panics otherwise | `unwind-on-signal` / `SetUnwindOnError` |
| scalars | unsupported | formatted locally |
| return value | ignored | `Err` reported with partial output |
| values without address | unsupported | copied to scratch |
| debuggers | its own | gdb and lldb, shared core |

## macOS

gdb is not a practical option on current macOS (no Apple Silicon support,
code-signing hoops), which is the main reason the lldb backend exists. Nothing
in the pipeline is Linux specific: Mach-O symbols demangle the same way, DWARF
in the `.o` files / dSYM is what lldb reads anyway, `AllocateMemory` and
expression evaluation work, and the two-word fat pointer convention holds on
AArch64. The macOS CI job runs the full lldb test suite on `macos-latest`
(arm64).

## Debugger facts worth knowing (learned the hard way)

gdb:

- `gdb.lookup_type("core::fmt::Formatter")` returns a *namespace* type in Rust
  programs (methods live under that name), not the struct. Take struct types
  from function parameter types or variables instead.
- `info functions REGEX` matches Rust names unreliably with `$` anchors; match
  loosely and filter in Python.
- `info functions -n` (debug symbols only) prints signatures; without `-n`
  ELF-only symbols appear as `0xADDR  name` and are the way to reach std
  functions that have no parameter DWARF.
- `info address <demangled name>` works unquoted for ELF symbols and returns
  the entry address. Quoting with `'...'` fails in the Rust expression parser
  (it is a char literal there).
- The same DWARF name may exist twice, once with parameters and once as a
  parameter-less declaration; check `len(sym.type.fields())`.
- `gdb.Value.bytes` (gdb ≥ 14) gives the raw bytes of a register-resident
  value; it is what makes copying address-less values possible.
- `gdb.Value(str)` becomes a C `char[]`, which the Rust printer shows as a
  number array. `$rfmt()` is therefore meant for `printf`/`dprintf`, not for
  `print`.
- `set $sp` acts on the *selected* frame; select the newest frame first, and
  reselect the user's frame by level afterwards (frame objects are invalidated
  by an inferior call).
- A gdb error aborts the rest of a `-x` batch script; in `tests/session.gdb`
  the deliberately failing command is last.

lldb:

- Class-based commands (`command script add -c pkg.mod.Class`) are resolved in
  lldb's *own* interpreter namespace. Importing the module from another Python
  module is not enough; run `script import pkg.mod` through `HandleCommand`
  first.
- `cmd/x` is lldb's gdb-format option syntax and is rejected for script
  commands ("doesn't support the --gdb-format option"), hence `rprint -p`.
- `SBSymbolContext.GetFunction()` from `FindGlobalFunctions` is often invalid;
  `target.ResolveSymbolContextForAddress(symbol.GetStartAddress(),
  eSymbolContextFunction).GetFunction()` gets the `SBFunction` with its type.
- With legacy mangling `SBFunction.GetName()` is the Itanium demangling of a
  Rust symbol: `_$LT$demo..Config$u20$as$u20$core..fmt..Debug$GT$::fmt::h1234…`.
  `core.normalize_demangled()` undoes the escapes and strips the hash.
- Since rustc 1.97 the drop function is `core::ptr::drop_glue::<T>` (with the
  turbofish in v0 demangling); the core accepts both spellings.
- `SBValue.GetLoadAddress()` is `LLDB_INVALID_ADDRESS` for register values and
  zero-sized types; `GetData().uint8s` still gives the bytes.
- `SBExpressionOptions.SetLanguage(eLanguageTypeC)` makes expression
  evaluation work in a Rust frame even though lldb prints the "no plugin for
  the language rust" warning.

## Troubleshooting

| message | meaning / fix |
|---|---|
| ``no `<T as core::fmt::Debug>::fmt` in the binary`` | rustc never emitted it. Format a `T` with `{:?}` somewhere, or add the `keep_debug_impls` hook from the README. |
| `… (found only: <T as core::fmt::Display>::fmt)` | the type has `Display` but no `Debug` instantiation. |
| `value is optimized out` | build with less optimization or inspect at a different line. |
| `cannot build <String as core::fmt::Write> vtable` | the binary is stripped or std symbols are missing; check `info address <alloc::string::String as core::fmt::Write>::write_str` (gdb) or `image lookup -n` (lldb). |
| `Debug::fmt failed: … signaled …` / `… SIGSEGV …` | the impl crashed; the debugger already unwound. Turn on verbose logging and check the chosen symbol and layout; please report it with the rustc version. |
| call hangs | probably a lock held by a stopped thread or the allocator; interrupt, then try turning the scheduler lock off. |
| lldb: `'rprint' is not a valid command` | the module could not be imported; run `command script import` by hand and read the error. |
