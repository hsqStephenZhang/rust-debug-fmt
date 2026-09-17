"""
rustdebugfmt.gdb_backend -- gdb implementation of the Backend interface plus
the `rprint`, `rlocals`, `rargs` commands and the `$rfmt()` function.

Loaded through `rust_debug_fmt_gdb.py` (which fixes up sys.path); do not
import this module outside of gdb.
"""

import contextlib
import re

import gdb

from . import core
from .core import Arg, Field, FmtFn, RfmtError, TypeLayout, ValueInfo

# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


class _BoolParam(gdb.Parameter):
    def __init__(self, name, default, doc):
        self.set_doc = "Set " + doc
        self.show_doc = "Show " + doc
        self.__doc__ = doc
        super(_BoolParam, self).__init__(name, gdb.COMMAND_DATA, gdb.PARAM_BOOLEAN)
        self.value = default

    def get_set_string(self):
        return ""

    def get_show_string(self, svalue):
        return "%s is %s" % (self.__doc__, svalue)


VERBOSE = _BoolParam("rfmt-verbose", False, "whether rust-debug-fmt logs its work")
SCHED_LOCK = _BoolParam(
    "rfmt-scheduler-lock", True, "whether rust-debug-fmt runs only the current thread during the call"
)


# --------------------------------------------------------------------------
# gdb helpers
# --------------------------------------------------------------------------

_INFO_ADDR_RE = re.compile(r"(?:is at|at address) (0x[0-9a-fA-F]+)")


def _msym_addr(name):
    """Address of an ELF symbol by demangled name (`info address`), or None."""
    try:
        out = gdb.execute("info address %s" % name, to_string=True)
    except gdb.error:
        return None
    m = _INFO_ADDR_RE.search(out)
    return int(m.group(1), 16) if m else None


def _demangle(linkage):
    if not linkage:
        return ""
    try:
        out = gdb.execute("demangle -l rust %s" % linkage, to_string=True).strip()
    except gdb.error:
        return linkage
    return linkage if out.startswith("Can't demangle") else out


def _function_symbols(name):
    """All DWARF function symbols with this qualified name."""
    syms = []
    try:
        syms.extend(gdb.lookup_static_symbols(name))
    except (gdb.error, AttributeError):
        try:
            s = gdb.lookup_static_symbol(name)
            if s is not None:
                syms.append(s)
        except gdb.error:
            pass
    try:
        s = gdb.lookup_global_symbol(name)
        if s is not None:
            syms.append(s)
    except gdb.error:
        pass
    out = []
    for s in syms:
        try:
            if s.type is not None and s.type.strip_typedefs().code == gdb.TYPE_CODE_FUNC:
                out.append(s)
        except gdb.error:
            pass
    return out


def _sym_addr(sym):
    try:
        return int(sym.value().address)
    except gdb.error:
        return 0


def _split_top(s, seps=","):
    """Split on `seps` outside of <>, (), [] (Rust generics, fn types)."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(s):
        ch = s[i]
        if ch == "-" and i + 1 < len(s) and s[i + 1] == ">":
            cur.append("->")
            i += 2
            continue
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if depth == 0 and ch in seps:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
        i += 1
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_signature(sig):
    """'NAME(PARAMS) -> RET;' -> (NAME, [PARAMS])."""
    depth, i = 0, 0
    while i < len(sig):
        ch = sig[i]
        if ch == "-" and i + 1 < len(sig) and sig[i + 1] == ">":
            i += 2
            continue
        if ch in "<[":
            depth += 1
        elif ch in ">]":
            depth -= 1
        elif ch == "(" and depth == 0:
            break
        i += 1
    else:
        return None
    name = sig[:i].strip()
    j, d = i, 0
    while j < len(sig):
        if sig[j] == "(":
            d += 1
        elif sig[j] == ")":
            d -= 1
            if d == 0:
                break
        j += 1
    params = sig[i + 1 : j].strip()
    return name, (_split_top(params) if params else [])


_INFO_FN_LINE_RE = re.compile(r"^(?:\d+:)?\s*(?:static\s+)?fn\s+(.*)$")


def _list_functions(regex):
    """Parse `info functions -q -n REGEX` into [(qualified name, [param types])]."""
    try:
        out = gdb.execute("info functions -q -n %s" % regex, to_string=True)
    except gdb.error as e:
        raise RfmtError("info functions failed: %s" % e)
    seen = set()
    result = []
    for line in out.splitlines():
        m = _INFO_FN_LINE_RE.match(line)
        if not m:
            continue
        parsed = _parse_signature(m.group(1))
        if parsed is None:
            continue
        key = (parsed[0], tuple(parsed[1]))
        if key in seen:
            continue
        seen.add(key)
        result.append(parsed)
    return result


def _layout_of(gdb_type, depth=0):
    try:
        t = gdb_type.strip_typedefs()
        if t.code not in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
            return None
        fields = []
        for f in t.fields():
            if f.name is None or getattr(f, "is_base_class", False):
                continue
            ft = f.type.strip_typedefs()
            sub = _layout_of(ft, depth + 1) if depth < 8 else None
            fields.append(Field(f.name, f.bitpos // 8, ft.sizeof, sub))
        return TypeLayout(str(t), t.sizeof, fields)
    except gdb.error:
        return None


def _call_c(addr, ret_ctype, ints):
    """Call an address without debug info through a C cast."""
    lang = gdb.parameter("language")
    gdb.execute("set language c", to_string=True)
    try:
        proto = ",".join("unsigned long" for _ in ints) or "void"
        call = ",".join("%#xUL" % a for a in ints)
        return gdb.parse_and_eval("((%s(*)(%s))%#x)(%s)" % (ret_ctype, proto, addr, call))
    finally:
        gdb.execute("set language %s" % lang, to_string=True)


# --------------------------------------------------------------------------
# the backend
# --------------------------------------------------------------------------


class GdbBackend(core.Backend):
    name = "gdb"

    def __init__(self):
        self.invalidate()

    def invalidate(self, _event=None):
        self._functions = {}
        self._string_layout = None
        self._demangled = {}

    # -- environment --
    def ptr_size(self):
        try:
            return gdb.lookup_type("usize").sizeof
        except gdb.error:
            return 8

    def byteorder(self):
        try:
            return "big" if "big endian" in gdb.execute("show endian", to_string=True) else "little"
        except gdb.error:
            return "little"

    def log(self, msg):
        if VERBOSE.value:
            gdb.write("[rfmt] %s\n" % msg)

    # -- type name spelling --
    def ptr_name(self, type_name, depth=1):
        return "*mut " * depth + type_name

    def slice_names(self, elem):
        return ["&[%s]" % elem, "&mut [%s]" % elem, "*mut [%s]" % elem]

    # -- symbols --
    def _demangle_sym(self, sym):
        ln = sym.linkage_name
        if ln not in self._demangled:
            self._demangled[ln] = _demangle(ln)
        return self._demangled[ln]

    def functions_named(self, suffix):
        if suffix not in self._functions:
            pat = re.compile(r"(^|::)%s(<.*>)?$" % re.escape(suffix))
            fns = []
            for name, params in _list_functions("::" + suffix):
                if not pat.search(name):
                    continue
                fns.append(FmtFn(name, params, resolver=self._resolve))
            self.log("indexed %d functions named %s" % (len(fns), suffix))
            self._functions[suffix] = fns
        return self._functions[suffix]

    def _resolve(self, fn):
        # the same name may also exist as a parameter-less declaration
        for sym in _function_symbols(fn.name):
            if len(sym.type.strip_typedefs().fields()) != len(fn.params):
                continue
            addr = _sym_addr(sym)
            if not addr:
                continue
            fn.resolve(demangled=self._demangle_sym(sym), addr=addr, handle=sym)
            return

    def function_addr(self, names):
        for n in names:
            syms = [s for s in _function_symbols(n) if _sym_addr(s)]
            if syms:
                typed = [s for s in syms if s.type.strip_typedefs().fields()]
                return _sym_addr((typed or syms)[0])
            addr = _msym_addr(n)
            if addr:
                return addr
        return 0

    def _typed_symbol(self, names):
        for n in names:
            for s in _function_symbols(n):
                if _sym_addr(s) and s.type.strip_typedefs().fields():
                    return s
        return None

    def formatter_layout(self, fn):
        sym = fn.handle
        if sym is None:
            return None
        try:
            return _layout_of(sym.type.strip_typedefs().fields()[1].type.target())
        except (gdb.error, IndexError):
            return None

    def string_layout(self):
        if self._string_layout is None:
            layout = None
            want = self.ptr_name(core.STRING_TYPE)
            for fn in self.functions_named("fmt"):
                if fn.params and fn.params[0] == want and fn.handle is not None:
                    layout = _layout_of(fn.handle.type.strip_typedefs().fields()[0].type.target())
                    break
            if layout is None:
                sym = self._typed_symbol(core.DROP_STRING_NAMES)
                if sym is not None:
                    layout = _layout_of(sym.type.strip_typedefs().fields()[0].type.target())
            self._string_layout = layout if layout is not None else False
        return self._string_layout or None

    # -- inferior --
    def read_memory(self, addr, n):
        try:
            return gdb.selected_inferior().read_memory(addr, n).tobytes()
        except gdb.MemoryError as e:
            raise RfmtError("cannot read %d bytes at %#x: %s" % (n, addr, e))

    def write_memory(self, addr, data):
        try:
            gdb.selected_inferior().write_memory(addr, bytes(data))
        except gdb.MemoryError as e:
            raise RfmtError("cannot write %d bytes at %#x: %s" % (len(data), addr, e))

    @contextlib.contextmanager
    def scratch(self, size):
        """Below the innermost frame's stack pointer; `$sp` is moved underneath
        for the duration so the inferior call frames land below it too."""
        level = gdb.selected_frame().level()
        newest = gdb.newest_frame()
        sp0 = int(newest.read_register("sp"))
        base = (sp0 - size - 512) & ~0x3F
        newest.select()
        gdb.execute("set $sp = %#x" % base, to_string=True)
        try:
            yield base
        finally:
            try:
                gdb.newest_frame().select()
                gdb.execute("set $sp = %#x" % sp0, to_string=True)
            finally:
                try:
                    gdb.execute("select-frame %d" % level, to_string=True)
                except gdb.error:
                    pass

    @contextlib.contextmanager
    def call_environment(self):
        saved = {}
        for param, val in (("unwind-on-signal", "on"), ("scheduler-locking", "on" if SCHED_LOCK.value else None)):
            if val is None:
                continue
            try:
                old = gdb.parameter(param)
                if isinstance(old, bool):
                    old = "on" if old else "off"
                gdb.execute("set %s %s" % (param, val), to_string=True)
                saved[param] = old
            except gdb.error as e:
                self.log("could not set %s: %s" % (param, e))
        try:
            yield
        finally:
            for param, old in saved.items():
                try:
                    gdb.execute("set %s %s" % (param, old), to_string=True)
                except gdb.error:
                    pass

    def call(self, fn, addr, args, ret_size):
        sym = fn.handle if fn is not None else None
        if sym is None:
            # e.g. drop_in_place::<String>: use its DWARF prototype when we have one
            cand = self._typed_symbol(core.DROP_STRING_NAMES)
            if cand is not None and _sym_addr(cand) == addr:
                sym = cand
        try:
            if sym is not None:
                ptypes = [f.type for f in sym.type.strip_typedefs().fields()]
                if len(ptypes) == len(args):
                    vals = []
                    for a, t in zip(args, ptypes):
                        if a.kind == Arg.INT:
                            vals.append(gdb.Value(a.value).cast(t))
                        elif a.kind == Arg.FAT_AT:
                            vals.append(gdb.Value(a.value).cast(t.pointer()).dereference())
                        else:
                            vals.append(a.value)
                    res = sym.value()(*vals)
                    if ret_size:
                        try:
                            return core.from_bytes(res.bytes[:ret_size], self.byteorder())
                        except (gdb.error, AttributeError):
                            return None
                    return None
            # untyped: C cast, fat pointers become two integer arguments
            ints = []
            ps = self.ptr_size()
            for a in args:
                if a.kind == Arg.INT:
                    ints.append(a.value)
                elif a.kind == Arg.FAT_AT:
                    raw = self.read_memory(a.value, 2 * ps)
                    ints.append(core.from_bytes(raw[:ps], self.byteorder()))
                    ints.append(core.from_bytes(raw[ps:], self.byteorder()))
                else:
                    raise RfmtError("cannot pass this value by value without debug info")
            res = _call_c(addr, "unsigned char" if ret_size else "void", ints)
            return int(res) if ret_size else None
        except gdb.error as e:
            raise RfmtError(str(e))

    # -- values --
    def value_info(self, value):
        try:
            if value.is_optimized_out:
                return ValueInfo("?", 0, optimized_out=True, native=value)
            t = value.type.strip_typedefs()
            if t.code in (gdb.TYPE_CODE_REF, gdb.TYPE_CODE_RVALUE_REF):
                value = value.referenced_value()
                t = value.type.strip_typedefs()
            tname = str(t)
            kind, scalar, elem, n = "other", None, None, None
            if t.code == gdb.TYPE_CODE_BOOL:
                kind, scalar = "bool", bool(int(value))
            elif t.code == gdb.TYPE_CODE_CHAR:
                kind, scalar = "char", int(value)
            elif t.code == gdb.TYPE_CODE_INT:
                kind, scalar = "int", int(value)
            elif t.code == gdb.TYPE_CODE_FLT:
                kind, scalar = "float", float(value)
            elif t.code == gdb.TYPE_CODE_ARRAY:
                kind = "array"
                elem = str(t.target().strip_typedefs())
                lo, hi = t.range()
                n = hi - lo + 1
            elif t.code == gdb.TYPE_CODE_STRUCT and t.sizeof == 0 and tname == "()":
                kind, scalar = "void", 0
            addr = int(value.address) if value.address is not None else None
            raw = None
            if addr is None:
                try:
                    raw = bytes(value.bytes)
                except (gdb.error, AttributeError):
                    raw = None
            return ValueInfo(
                tname, t.sizeof, kind, addr, raw, False, elem, n, scalar, native=value
            )
        except gdb.error as e:
            raise RfmtError(str(e))


BACKEND = GdbBackend()

for _ev in ("new_objfile", "clear_objfiles", "free_objfile", "exited"):
    try:
        getattr(gdb.events, _ev).connect(BACKEND.invalidate)
    except AttributeError:
        pass


def debug_format(value, pretty=False):
    """`{:?}` of a gdb.Value as a str (raises RfmtError)."""
    return core.debug_format(BACKEND, BACKEND.value_info(value), pretty)


def _native_text(val):
    try:
        return val.format_string(max_elements=16, max_depth=2)
    except (gdb.error, TypeError):
        try:
            return str(val)
        except gdb.error as e:
            return "<%s>" % e


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def _gdb_error(fn):
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except RfmtError as e:
            raise gdb.GdbError(str(e))

    return wrapper


class RPrint(gdb.Command):
    """Print Rust values using the debuggee's own Debug impl.

rprint[/p] EXPR [EXPR ...]

Calls `<T as core::fmt::Debug>::fmt` inside the debuggee for every EXPR and
prints the resulting text. `/p` uses the alternate (pretty, `{:#?}`) form."""

    def __init__(self):
        super(RPrint, self).__init__("rprint", gdb.COMMAND_DATA, gdb.COMPLETE_EXPRESSION)

    @_gdb_error
    def invoke(self, arg, from_tty):
        pretty, rest = core.parse_slash(arg)
        exprs = gdb.string_to_argv(rest)
        if not exprs:
            raise gdb.GdbError("usage: rprint[/p] EXPR [EXPR ...]")
        for e in exprs:
            try:
                val = gdb.parse_and_eval(e)
            except gdb.error as ex:
                raise gdb.GdbError("%s: %s" % (e, ex))
            gdb.write("%s = %s\n" % (e, debug_format(val, pretty)))


class _FrameVarsCommand(gdb.Command):
    want_args = False
    _command_name = ""

    @_gdb_error
    def invoke(self, arg, from_tty):
        pretty, rest = core.parse_slash(arg)
        if rest:
            raise gdb.GdbError("usage: %s [/p]" % self._command_name)
        try:
            frame = gdb.selected_frame()
            block = frame.block()
        except RuntimeError as e:
            raise gdb.GdbError(str(e))
        seen = set()
        printed = 0
        while block is not None:
            for sym in block:
                ok = sym.is_argument if self.want_args else (sym.is_variable and not sym.is_argument)
                if not ok or sym.name in seen:
                    continue
                seen.add(sym.name)
                printed += 1
                try:
                    val = frame.read_var(sym, block)
                except (gdb.error, ValueError) as e:
                    gdb.write("%s = <error: %s>\n" % (sym.name, e))
                    continue
                try:
                    info = BACKEND.value_info(val)
                except RfmtError as e:
                    gdb.write("%s = %s    (fallback: %s)\n" % (sym.name, _native_text(val), e))
                    continue
                gdb.write(core.render_variable(BACKEND, sym.name, info, _native_text(val), pretty) + "\n")
            if block.function is not None:
                break
            block = block.superblock
        if printed == 0:
            gdb.write("No %s.\n" % ("arguments" if self.want_args else "locals"))


class RLocals(_FrameVarsCommand):
    """Print every local of the selected frame using the debuggee's Debug impls.

rlocals [/p]

Variables whose Debug::fmt is not available fall back to gdb's own printer."""

    _command_name = "rlocals"
    want_args = False

    def __init__(self):
        super(RLocals, self).__init__("rlocals", gdb.COMMAND_DATA)


class RArgs(_FrameVarsCommand):
    """Print every argument of the selected frame using the debuggee's Debug impls.

rargs [/p]"""

    _command_name = "rargs"
    want_args = True

    def __init__(self):
        super(RArgs, self).__init__("rargs", gdb.COMMAND_DATA)


class RfmtFunction(gdb.Function):
    """$rfmt(VALUE [, PRETTY]) -> the `{:?}` text of VALUE (use with printf/dprintf)."""

    def __init__(self):
        super(RfmtFunction, self).__init__("rfmt")

    @_gdb_error
    def invoke(self, value, pretty=None):
        return gdb.Value(debug_format(value, bool(int(pretty)) if pretty is not None else False))


_registered = False


def register():
    global _registered
    if _registered:
        return
    _registered = True
    RPrint()
    RLocals()
    RArgs()
    RfmtFunction()
