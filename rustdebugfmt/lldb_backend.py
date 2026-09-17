"""
rustdebugfmt.lldb_backend -- lldb implementation of the Backend interface plus
the `rprint`, `rlocals`, `rargs` and `rfmt-set` commands.

Loaded through `rust_debug_fmt_lldb.py` (`command script import`); do not
import this module outside of lldb.

lldb has no Rust language plugin, so it spells Rust types the C way:
`rfmt_test::Point *` for `&Point`, `unsigned short[3]` for `[u16; 3]`,
`unsigned int` for `u32`. Variables and function parameters come out of the
same type system, so comparing those spellings works; only arrays/slices need
a small C-to-Rust table for the element type.
"""

import contextlib
import re
import shlex
import struct

import lldb

from . import core
from .core import Arg, Field, FmtFn, RfmtError, TypeLayout, ValueInfo

SETTINGS = {"verbose": False, "scheduler-lock": True, "timeout": 30, "auto": False}
CATEGORY = "rust-debug-fmt"

# lldb's C spelling of Rust primitives -> possible Rust names (for `&[E]` lookups)
_C_TO_RUST = {
    "unsigned char": ["u8"],
    "signed char": ["i8"],
    "char": ["i8", "u8"],
    "unsigned short": ["u16"],
    "short": ["i16"],
    "unsigned int": ["u32"],
    "int": ["i32"],
    "unsigned long": ["u64", "usize"],
    "long": ["i64", "isize"],
    "unsigned long long": ["u64", "usize"],
    "long long": ["i64", "isize"],
    "unsigned __int128": ["u128"],
    "__int128": ["i128"],
    "float": ["f32"],
    "double": ["f64"],
    "bool": ["bool"],
    "char32_t": ["char"],
}

_HASH_SUFFIX = r"(::h[0-9a-f]{16})?$"


def _layout_of(t, depth=0):
    n = t.GetNumberOfFields()
    if n == 0:
        return None
    fields = []
    for k in range(n):
        m = t.GetFieldAtIndex(k)
        name = m.GetName()
        if not name:
            continue
        mt = m.GetType()
        sub = _layout_of(mt, depth + 1) if depth < 8 and mt.GetNumberOfFields() else None
        fields.append(Field(name, m.GetOffsetInBytes(), mt.GetByteSize(), sub))
    return TypeLayout(t.GetName(), t.GetByteSize(), fields)


class LldbBackend(core.Backend):
    name = "lldb"

    def __init__(self, debugger):
        self.debugger = debugger
        self._functions = {}
        self._symbol_index = None
        self._string_layout = None
        self._cache_key = None
        self.auto_cache = {}

    # -- lldb objects --
    def _target(self):
        t = self.debugger.GetSelectedTarget()
        if not t.IsValid():
            raise RfmtError("no target")
        return t

    def _process(self):
        p = self._target().GetProcess()
        if not p.IsValid() or p.GetState() != lldb.eStateStopped:
            raise RfmtError("process is not stopped")
        return p

    def _frame0(self):
        thread = self._process().GetSelectedThread()
        return thread.GetFrameAtIndex(0)

    def _check_cache(self):
        p = self._target().GetProcess()
        key = (self._target().GetTriple(), p.GetProcessID() if p.IsValid() else 0, self._target().GetNumModules())
        if key != self._cache_key:
            self._cache_key = key
            self._functions = {}
            self._symbol_index = None
            self._string_layout = None
            self.auto_cache = {}

    # -- environment --
    def ptr_size(self):
        return self._target().GetAddressByteSize()

    def byteorder(self):
        return "big" if self._target().GetByteOrder() == lldb.eByteOrderBig else "little"

    def log(self, msg):
        if SETTINGS["verbose"]:
            print("[rfmt] %s" % msg)

    # -- type name spelling --
    def ptr_name(self, type_name, depth=1):
        # `T *`, `T **`; a pointer to a pointer type is spelled `T **`, not `T * *`
        sep = "" if type_name.endswith("*") else " "
        return type_name + sep + "*" * depth

    def array_ptr_names(self, elem, n):
        return ["%s (*)[%d]" % (elem, n)]

    def slice_names(self, elem):
        out = []
        for r in _C_TO_RUST.get(elem, [elem]):
            out += ["&[%s]" % r, "&mut [%s]" % r]
        return out

    # -- symbols --
    def _function_of(self, sc):
        f = sc.GetFunction()
        if f.IsValid():
            return f
        sym = sc.GetSymbol()
        if not sym.IsValid():
            return f
        return self._target().ResolveSymbolContextForAddress(
            sym.GetStartAddress(), lldb.eSymbolContextFunction
        ).GetFunction()

    def functions_named(self, suffix):
        self._check_cache()
        if suffix not in self._functions:
            target = self._target()
            regex = r"(^|::)%s(<.*>)?%s" % (re.escape(suffix), _HASH_SUFFIX)
            scl = target.FindGlobalFunctions(regex, 0, lldb.eMatchTypeRegex)
            seen = set()
            fns = []
            for i in range(scl.GetSize()):
                sc = scl.GetContextAtIndex(i)
                f = self._function_of(sc)
                if not f.IsValid():
                    continue
                addr = f.GetStartAddress().GetLoadAddress(target)
                if addr in seen or addr == lldb.LLDB_INVALID_ADDRESS:
                    continue
                seen.add(addr)
                ft = f.GetType()
                argtypes = ft.GetFunctionArgumentTypes()
                params = [argtypes.GetTypeAtIndex(j).GetName() for j in range(argtypes.GetSize())]
                name = f.GetName() or sc.GetSymbol().GetName() or f.GetMangledName() or ""
                fns.append(FmtFn(name, params, demangled=name, addr=addr, handle=f))
            self.log("indexed %d functions named %s" % (len(fns), suffix))
            self._functions[suffix] = fns
        return self._functions[suffix]

    def _symbols(self):
        """{normalized demangled name: load address} over all modules (built once)."""
        if self._symbol_index is None:
            target = self._target()
            idx = {}
            for mi in range(target.GetNumModules()):
                mod = target.GetModuleAtIndex(mi)
                for sym in mod:
                    if sym.GetType() != lldb.eSymbolTypeCode:
                        continue
                    n = sym.GetName()
                    if not n:
                        continue
                    addr = sym.GetStartAddress().GetLoadAddress(target)
                    if addr == lldb.LLDB_INVALID_ADDRESS:
                        continue
                    idx.setdefault(core.normalize_demangled(n), addr)
            self.log("indexed %d code symbols" % len(idx))
            self._symbol_index = idx
        return self._symbol_index

    def function_addr(self, names):
        self._check_cache()
        target = self._target()
        for n in names:
            fl = target.FindFunctions(n)
            for i in range(fl.GetSize()):
                sym = fl.GetContextAtIndex(i).GetSymbol()
                if sym.IsValid():
                    addr = sym.GetStartAddress().GetLoadAddress(target)
                    if addr != lldb.LLDB_INVALID_ADDRESS:
                        return addr
        for n in names:
            addr = self._symbols().get(n)
            if addr:
                return addr
        return 0

    def formatter_layout(self, fn):
        f = fn.handle
        if f is None or not f.IsValid():
            return None
        args = f.GetType().GetFunctionArgumentTypes()
        if args.GetSize() < 2:
            return None
        return _layout_of(args.GetTypeAtIndex(1).GetPointeeType())

    def string_layout(self):
        self._check_cache()
        if self._string_layout is None:
            t = self._target().FindFirstType(core.STRING_TYPE)
            layout = _layout_of(t) if t.IsValid() else None
            self._string_layout = layout if layout is not None else False
        return self._string_layout or None

    # -- inferior --
    def read_memory(self, addr, n):
        if n == 0:
            return b""
        err = lldb.SBError()
        data = self._process().ReadMemory(addr, n, err)
        if err.Fail() or data is None:
            raise RfmtError("cannot read %d bytes at %#x: %s" % (n, addr, err.GetCString()))
        return bytes(data)

    def write_memory(self, addr, data):
        if not data:
            return
        err = lldb.SBError()
        self._process().WriteMemory(addr, bytes(data), err)
        if err.Fail():
            raise RfmtError("cannot write %d bytes at %#x: %s" % (len(data), addr, err.GetCString()))

    @contextlib.contextmanager
    def scratch(self, size):
        """Prefer memory allocated by lldb in the debuggee; fall back to the stack."""
        process = self._process()
        err = lldb.SBError()
        base = process.AllocateMemory(size + 64, lldb.ePermissionsReadable | lldb.ePermissionsWritable, err)
        if err.Success() and base != lldb.LLDB_INVALID_ADDRESS:
            try:
                yield base
            finally:
                process.DeallocateMemory(base)
            return
        self.log("AllocateMemory failed (%s), using the stack" % err.GetCString())
        frame0 = self._frame0()
        reg = frame0.FindRegister("sp")
        sp0 = reg.GetValueAsUnsigned()
        base = (sp0 - size - 512) & ~0x3F
        if not reg.SetValueFromCString("%#x" % base):
            raise RfmtError("cannot move the stack pointer")
        try:
            yield base
        finally:
            self._frame0().FindRegister("sp").SetValueFromCString("%#x" % sp0)

    @contextlib.contextmanager
    def call_environment(self):
        yield  # everything is handled through SBExpressionOptions in call()

    def call(self, fn, addr, args, ret_size):
        ps = self.ptr_size()
        order = self.byteorder()
        ints = []
        for a in args:
            if a.kind == Arg.INT:
                ints.append(a.value)
            elif a.kind == Arg.FAT_AT:
                raw = self.read_memory(a.value, 2 * ps)
                ints.append(core.from_bytes(raw[:ps], order))
                ints.append(core.from_bytes(raw[ps:], order))
            else:  # NATIVE: an SBValue passed by value (a pointer or a fat pointer)
                raw = bytes(a.value.GetData().uint8s)
                if len(raw) == ps:
                    ints.append(core.from_bytes(raw, order))
                elif len(raw) == 2 * ps:
                    ints.append(core.from_bytes(raw[:ps], order))
                    ints.append(core.from_bytes(raw[ps:], order))
                else:
                    raise RfmtError("cannot pass a %d byte value by value" % len(raw))
        proto = ",".join("unsigned long" for _ in ints) or "void"
        ret = "unsigned char" if ret_size else "void"
        expr = "((%s(*)(%s))%#xUL)(%s)" % (ret, proto, addr, ",".join("%#xUL" % v for v in ints))

        opts = lldb.SBExpressionOptions()
        opts.SetLanguage(lldb.eLanguageTypeC)
        opts.SetUnwindOnError(True)
        opts.SetIgnoreBreakpoints(True)
        opts.SetTryAllThreads(not SETTINGS["scheduler-lock"])
        opts.SetTimeoutInMicroSeconds(int(SETTINGS["timeout"] * 1_000_000))
        frame = self._frame0()
        res = frame.EvaluateExpression(expr, opts)
        if res.GetError().Fail():
            raise RfmtError(res.GetError().GetCString() or "expression failed")
        return res.GetValueAsUnsigned() if ret_size else None

    # -- values --
    def value_info(self, v):
        if not v.IsValid():
            raise RfmtError("invalid value")
        t = v.GetType()
        if t.IsReferenceType():
            v = v.Dereference()
            t = v.GetType()
        err = v.GetError()
        if err.Fail() and "optimized out" in (err.GetCString() or "").lower():
            return ValueInfo(t.GetName() or "?", 0, optimized_out=True, native=v)
        tname = t.GetName() or "?"
        size = t.GetByteSize()
        kind, scalar, elem, n = "other", None, None, None
        tc = t.GetTypeClass()
        if tc == lldb.eTypeClassArray:
            kind = "array"
            elem = t.GetArrayElementType().GetName()
            n = v.GetNumChildren()
        elif tc == lldb.eTypeClassBuiltin:
            bt = t.GetBasicType()
            if bt == lldb.eBasicTypeBool:
                kind, scalar = "bool", bool(v.GetValueAsUnsigned())
            elif bt in (lldb.eBasicTypeChar32, lldb.eBasicTypeChar16):
                kind, scalar = "char", v.GetValueAsUnsigned()
            elif bt in (lldb.eBasicTypeFloat, lldb.eBasicTypeDouble, lldb.eBasicTypeLongDouble, lldb.eBasicTypeHalf):
                kind = "float"
                raw = bytes(v.GetData().uint8s)
                if len(raw) == 4:
                    scalar = struct.unpack("<f" if self.byteorder() == "little" else ">f", raw)[0]
                elif len(raw) == 8:
                    scalar = struct.unpack("<d" if self.byteorder() == "little" else ">d", raw)[0]
            elif bt == lldb.eBasicTypeVoid:
                kind, scalar = "void", 0
            elif size in (1, 2, 4, 8, 16):
                unsigned = tname.startswith("unsigned") or tname.startswith("u")
                raw = bytes(v.GetData().uint8s)
                if raw:
                    scalar = int.from_bytes(raw, self.byteorder(), signed=not unsigned)
                    kind = "uint" if unsigned else "int"
        addr = v.GetLoadAddress()
        if addr == lldb.LLDB_INVALID_ADDRESS:
            addr = None
        raw = None
        if addr is None and size:
            raw = bytes(v.GetData().uint8s)
            if len(raw) != size:
                raw = None
        optimized_out = addr is None and raw is None and size > 0 and kind != "void"
        return ValueInfo(tname, size, kind, addr, raw, optimized_out, elem, n, scalar, native=v)


_BACKENDS = {}


def backend_for(debugger):
    key = debugger.GetID()
    if key not in _BACKENDS:
        _BACKENDS[key] = LldbBackend(debugger)
    return _BACKENDS[key]


def debug_format(debugger, value, pretty=False):
    """`{:?}` of an SBValue as a str (raises RfmtError)."""
    b = backend_for(debugger)
    return core.debug_format(b, b.value_info(value), pretty)


def _native_text(v):
    with GUARD:  # GetSummary() would otherwise re-enter our own summary
        return v.GetSummary() or v.GetValue() or ("<%s>" % (v.GetType().GetName() or "?"))


# --------------------------------------------------------------------------
# automatic mode: a type summary that hands aggregates to Debug::fmt
# --------------------------------------------------------------------------

GUARD = core.ReentrancyGuard()
_AGGREGATES = (lldb.eTypeClassStruct, lldb.eTypeClassClass, lldb.eTypeClassUnion, lldb.eTypeClassArray, lldb.eTypeClassEnumeration)


def auto_summary(valobj, internal_dict):
    """lldb summary provider, registered for every type.

    Returning "" makes lldb fall back to its normal display (value, children),
    so declining is cheap and invisible; returning None would print "None".
    """
    if not SETTINGS["auto"] or GUARD.active:
        return ""
    try:
        t = valobj.GetType()
        if t.IsReferenceType():
            t = t.GetDereferencedType()
        if t.GetTypeClass() not in _AGGREGATES:
            return ""
        process = valobj.GetProcess()
        if not process.IsValid() or process.GetState() != lldb.eStateStopped:
            return ""
        b = backend_for(valobj.GetTarget().GetDebugger())
        with GUARD:
            info = b.value_info(valobj)
            if not core.auto_supported(b, info, b.auto_cache):
                return ""
            return core.debug_format(b, info, SETTINGS.get("pretty", False))
    except RfmtError as e:
        backend_for(valobj.GetTarget().GetDebugger()).log("auto: %s" % e)
        return ""


_STOP_HOOKS = {}  # target id -> stop-hook id


def _set_auto(debugger, on, result=None):
    """Enable/disable automatic mode.

    Other formatter categories (the Rust toolchain's, loaded by rust-lldb and
    CodeLLDB) contain catch-all summaries, and lldb consults the most recently
    *enabled* category first. So besides enabling ours now, a stop-hook
    re-enables it at every stop, which keeps it in front no matter who loads
    after us. The hook needs a target; without one we can only warn.
    """
    SETTINGS["auto"] = on
    target = debugger.GetSelectedTarget()
    if on:
        debugger.HandleCommand(
            "type summary add -w %s -x '^.*$' --python-function %s.auto_summary" % (CATEGORY, __name__)
        )
        debugger.HandleCommand("type category enable %s" % CATEGORY)
        if target.IsValid():
            tid = target.GetUniqueID() if hasattr(target, "GetUniqueID") else str(target)
            if tid not in _STOP_HOOKS:
                res = lldb.SBCommandReturnObject()
                debugger.GetCommandInterpreter().HandleCommand(
                    "target stop-hook add -o 'type category enable %s'" % CATEGORY, res
                )
                m = re.search(r"#(\d+)", res.GetOutput() or "")
                _STOP_HOOKS[tid] = int(m.group(1)) if m else None
        elif result is not None:
            result.AppendMessage(
                "rfmt: no target yet. Formatters loaded later (e.g. the Rust toolchain's) will take "
                "precedence until you run `rfmt-set auto on` again with a target (CodeLLDB: put it in "
                "postRunCommands)."
            )
    else:
        debugger.HandleCommand("type category disable %s" % CATEGORY)
        if target.IsValid():
            tid = target.GetUniqueID() if hasattr(target, "GetUniqueID") else str(target)
            hook = _STOP_HOOKS.pop(tid, None)
            if hook is not None:
                debugger.HandleCommand("target stop-hook delete %d" % hook)


def _frame(exe_ctx, debugger):
    frame = exe_ctx.GetFrame()
    if not frame.IsValid():
        frame = debugger.GetSelectedTarget().GetProcess().GetSelectedThread().GetSelectedFrame()
    if not frame.IsValid():
        raise RfmtError("no frame selected (is the process stopped?)")
    return frame


def _lookup(frame, expr):
    v = frame.GetValueForVariablePath(expr)
    if v.IsValid() and v.GetError().Success():
        return v
    v2 = frame.FindVariable(expr)
    if v2.IsValid() and v2.GetError().Success():
        return v2
    opts = lldb.SBExpressionOptions()
    opts.SetLanguage(lldb.eLanguageTypeC)
    v3 = frame.EvaluateExpression(expr, opts)
    if v3.IsValid() and v3.GetError().Success():
        return v3
    raise RfmtError("%s: %s" % (expr, (v.GetError().GetCString() or v3.GetError().GetCString() or "not found")))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


class _Command(object):
    usage = ""

    def __init__(self, debugger, internal_dict):
        pass

    def get_short_help(self):
        return self.__doc__.strip().splitlines()[0]

    def get_long_help(self):
        return self.__doc__.strip()

    def __call__(self, debugger, command, exe_ctx, result):
        try:
            self.run(debugger, command, exe_ctx, result)
        except RfmtError as e:
            result.SetError(str(e))


class RPrint(_Command):
    """Print Rust values using the debuggee's own Debug impl.

rprint[/p] EXPR [EXPR ...]

Calls `<T as core::fmt::Debug>::fmt` inside the debuggee for every EXPR and
prints the resulting text. `/p` uses the alternate (pretty, `{:#?}`) form.
EXPR is a variable path (a, a.b, *p, arr[2]) or a C expression."""

    def run(self, debugger, command, exe_ctx, result):
        pretty, rest = core.parse_slash(command)
        exprs = shlex.split(rest)
        if not exprs:
            raise RfmtError("usage: rprint[/p] EXPR [EXPR ...]")
        frame = _frame(exe_ctx, debugger)
        for e in exprs:
            val = _lookup(frame, e)
            result.AppendMessage("%s = %s" % (e, debug_format(debugger, val, pretty)))


class _FrameVars(_Command):
    want_args = False
    name = ""

    def run(self, debugger, command, exe_ctx, result):
        pretty, rest = core.parse_slash(command)
        if rest:
            raise RfmtError("usage: %s [/p]" % self.name)
        frame = _frame(exe_ctx, debugger)
        b = backend_for(debugger)
        vals = frame.GetVariables(self.want_args, not self.want_args, False, True)
        seen = set()
        printed = 0
        for v in vals:
            name = v.GetName()
            if not name or name in seen:
                continue
            seen.add(name)
            printed += 1
            try:
                info = b.value_info(v)
            except RfmtError as e:
                result.AppendMessage("%s = %s    (fallback: %s)" % (name, _native_text(v), e))
                continue
            result.AppendMessage(core.render_variable(b, name, info, _native_text(v), pretty))
        if printed == 0:
            result.AppendMessage("No %s." % ("arguments" if self.want_args else "locals"))


class RLocals(_FrameVars):
    """Print every local of the selected frame using the debuggee's Debug impls.

rlocals [/p]

Variables whose Debug::fmt is not available fall back to lldb's own summary."""

    want_args = False
    name = "rlocals"


class RArgs(_FrameVars):
    """Print every argument of the selected frame using the debuggee's Debug impls.

rargs [/p]"""

    want_args = True
    name = "rargs"


class RfmtSet(_Command):
    """Change rust-debug-fmt settings.

rfmt-set auto on|off             make `p`, `frame variable`, IDE variable views use Debug::fmt
                                 for every aggregate that has one (default off)
rfmt-set pretty on|off           use {:#?} in automatic mode (default off)
rfmt-set verbose on|off          log symbol resolution and calls
rfmt-set scheduler-lock on|off   run only the current thread during the call (default on)
rfmt-set timeout SECONDS         expression timeout (default 30)
rfmt-set                         show current settings"""

    def run(self, debugger, command, exe_ctx, result):
        parts = command.split()
        if not parts:
            for k, v in SETTINGS.items():
                result.AppendMessage("%s = %s" % (k, v))
            return
        if len(parts) != 2 or parts[0] not in SETTINGS and parts[0] != "pretty":
            raise RfmtError(self.__doc__.strip())
        key, val = parts
        if key == "timeout":
            SETTINGS[key] = float(val)
            return
        if val not in ("on", "off", "true", "false", "1", "0"):
            raise RfmtError("expected on|off")
        flag = val in ("on", "true", "1")
        if key == "auto":
            _set_auto(debugger, flag, result)
        else:
            SETTINGS[key] = flag


def register(debugger):
    mod = __name__
    # lldb resolves the class path in its own interpreter namespace, so the
    # module has to be imported there, not just in ours.
    debugger.HandleCommand("script import %s" % mod.split(".")[0])
    debugger.HandleCommand("script import %s" % mod)
    for cls, name in ((RPrint, "rprint"), (RLocals, "rlocals"), (RArgs, "rargs"), (RfmtSet, "rfmt-set")):
        debugger.HandleCommand("command script add -c %s.%s %s" % (mod, cls.__name__, name))
