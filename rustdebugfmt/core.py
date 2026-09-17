"""
rustdebugfmt.core -- the debugger-independent part of rust-debug-fmt.

Everything here knows about Rust (symbol names, `Formatter` / `String`
layouts, the `<String as Write>` vtable, how to pick the right `fmt`
monomorphization) but nothing about gdb or lldb. The debugger specific parts
implement the `Backend` interface below; see `gdb_backend.py` and
`lldb_backend.py`.
"""

import re

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

STRING_TYPE = "alloc::string::String"
FORMATTER_TYPE = "core::fmt::Formatter"

# rustc < 1.97 emits drop_in_place, later versions drop_glue (the v0
# demangler spells it `drop_glue::<T>`; normalize_demangled() removes `::<`).
DROP_STRING_NAMES = (
    "core::ptr::drop_in_place<alloc::string::String>",
    "core::ptr::drop_glue<alloc::string::String>",
)
STRING_WRITE_STR = "<alloc::string::String as core::fmt::Write>::write_str"
STRING_WRITE_CHAR = "<alloc::string::String as core::fmt::Write>::write_char"
STRING_WRITE_FMT_NAMES = (
    "<alloc::string::String as core::fmt::Write>::write_fmt",
    "core::fmt::Write::write_fmt<alloc::string::String>",
)

# core::fmt::FormattingOptions::flags, rustc >= 1.87
ALTERNATE_FLAG = 1 << 23
ALIGN_UNKNOWN = 3 << 29
ALWAYS_SET = 1 << 31
# legacy `flags: u32` (rustc < 1.87): FlagV1::Alternate
LEGACY_ALTERNATE = 1 << 2

MAX_COPY = 64 * 1024


class RfmtError(Exception):
    """User-facing failure; backends turn it into their command error."""


# --------------------------------------------------------------------------
# symbol names
# --------------------------------------------------------------------------

_LEGACY_MAP = {
    "$SP$": "@",
    "$BP$": "*",
    "$RF$": "&",
    "$LT$": "<",
    "$GT$": ">",
    "$LP$": "(",
    "$RP$": ")",
    "$C$": ",",
}
_HASH_RE = re.compile(r"::h[0-9a-f]{16}$")
_UNICODE_RE = re.compile(r"\$u([0-9a-fA-F]{2,6})\$")
_UNDERSCORE_RE = re.compile(r"(^|::)_(?=[<\[{(*&])")


def normalize_demangled(name):
    """Bring a demangled Rust symbol into one canonical spelling.

    Handles three inputs: gdb's `demangle -l rust` output, LLVM's v0 demangler
    output (`drop_glue::<T>`), and Itanium-demangled *legacy* Rust names where
    the `$LT$`/`$u20$`/`..` escapes are still present (what lldb shows).
    """
    if not name:
        return ""
    name = _HASH_RE.sub("", name)
    if "$" in name:
        name = name.replace("..", "::")
        for k, v in _LEGACY_MAP.items():
            name = name.replace(k, v)
        name = _UNICODE_RE.sub(lambda m: chr(int(m.group(1), 16)), name)
        name = _UNDERSCORE_RE.sub(r"\1", name)
        name = _HASH_RE.sub("", name)
    return name.replace("::<", "<")


_FMT_NAME_RE = re.compile(r"(^|::)fmt(<.*>)?$")


def looks_like_fmt(name):
    return bool(_FMT_NAME_RE.search(name))


def is_debug_impl(demangled):
    return "core::fmt::Debug" in demangled


# --------------------------------------------------------------------------
# data carried between backend and core
# --------------------------------------------------------------------------


class FmtFn(object):
    """A function the debugger knows, with enough to call it.

    Backends may create it cheaply from a name listing and hand in a
    `resolver(fn)` that fills in `demangled`, `addr` and `handle` on first use
    (symbol lookups are expensive and most candidates never match).
    """

    def __init__(self, name, params, resolver=None, demangled=None, addr=0, handle=None):
        self.name = name  # the debugger's own qualified name
        self.params = list(params)  # parameter type names, debugger spelling
        self._resolver = resolver
        self._resolved = resolver is None
        self._demangled = normalize_demangled(demangled) if demangled else None
        self._addr = addr or 0
        self._handle = handle

    def resolve(self, demangled=None, addr=None, handle=None):
        """Called by the backend's resolver to fill in the expensive parts."""
        if demangled:
            self._demangled = normalize_demangled(demangled)
        if addr is not None:
            self._addr = addr
        if handle is not None:
            self._handle = handle

    def _ensure(self):
        if not self._resolved:
            self._resolved = True
            self._resolver(self)

    @property
    def demangled(self):
        self._ensure()
        return self._demangled or normalize_demangled(self.name)

    @property
    def addr(self):
        self._ensure()
        return self._addr

    @property
    def handle(self):
        self._ensure()
        return self._handle

    def __repr__(self):
        return "FmtFn(%r, %r)" % (self.name, self.params)


class Field(object):
    def __init__(self, name, offset, size, layout=None):
        self.name = name
        self.offset = offset
        self.size = size
        self.layout = layout  # TypeLayout for aggregates, else None


class TypeLayout(object):
    """Just enough of a struct type: name, size, named fields with offsets."""

    def __init__(self, name, size, fields):
        self.name = name
        self.size = size
        self.fields = fields  # list of Field

    def field(self, name):
        for f in self.fields:
            if f.name == name:
                return f
        return None

    def leaf_offsets(self, max_depth=8):
        """{field name: absolute offset} for every named field, outermost first wins."""
        out = {}

        def walk(layout, base, depth):
            for f in layout.fields:
                if f.name is None:
                    continue
                out.setdefault(f.name, base + f.offset)
                if f.layout is not None and depth < max_depth and f.layout.size > 0:
                    walk(f.layout, base + f.offset, depth + 1)

        walk(self, 0, 0)
        return out


class ValueInfo(object):
    """What the core needs to know about a variable, debugger independent."""

    KINDS = ("int", "uint", "float", "bool", "char", "array", "void", "other")

    def __init__(
        self,
        type_name,
        size,
        kind="other",
        address=None,
        raw=None,
        optimized_out=False,
        elem_type_name=None,
        array_len=None,
        scalar=None,
        native=None,
    ):
        self.type_name = type_name
        self.size = size
        self.kind = kind
        self.address = address  # int or None (register / constant)
        self.raw = raw  # bytes when address is None and the value is known
        self.optimized_out = optimized_out
        self.elem_type_name = elem_type_name
        self.array_len = array_len
        self.scalar = scalar  # python int / float / bool for scalar kinds
        self.native = native  # backend's own value object


class Arg(object):
    """One argument for an inferior call."""

    INT = "int"  # an integer / pointer in one register
    FAT_AT = "fat_at"  # a two word fat pointer stored at this address, passed by value
    NATIVE = "native"  # the backend's own value object, passed by value

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    @classmethod
    def int(cls, v):
        return cls(cls.INT, int(v))

    @classmethod
    def fat_at(cls, addr):
        return cls(cls.FAT_AT, int(addr))

    @classmethod
    def native(cls, v):
        return cls(cls.NATIVE, v)


# --------------------------------------------------------------------------
# backend interface
# --------------------------------------------------------------------------


class Backend(object):
    """What a debugger has to provide. Documented here, implemented per debugger."""

    name = "backend"

    # -- environment --
    def ptr_size(self):
        raise NotImplementedError

    def byteorder(self):
        return "little"

    def log(self, msg):
        pass

    # -- type name spelling --
    def ptr_name(self, type_name, depth=1):
        """How this debugger spells a pointer to `type_name` (`*mut T` / `T *`)."""
        raise NotImplementedError

    def array_ptr_names(self, elem_type_name, n):
        """Spellings of a pointer to `[E; N]`."""
        return []

    def slice_names(self, elem_type_name):
        """Spellings of `&[E]` for an array element type `E`."""
        return []

    # -- symbols --
    def functions_named(self, suffix):
        """All functions whose (normalized) name ends with `suffix`, as FmtFn."""
        raise NotImplementedError

    def function_addr(self, demangled_names):
        """Address of the first symbol matching one of the canonical names, else 0."""
        raise NotImplementedError

    def formatter_layout(self, fn):
        """TypeLayout of core::fmt::Formatter taken from fn's second parameter, or None."""
        return None

    def string_layout(self):
        """TypeLayout of alloc::string::String, or None."""
        return None

    # -- inferior --
    def read_memory(self, addr, n):
        raise NotImplementedError

    def write_memory(self, addr, data):
        raise NotImplementedError

    def scratch(self, size):
        """Context manager yielding the address of `size` writable bytes.

        Must also make sure the debugger performs inferior calls *below* that
        area if it lives on the stack.
        """
        raise NotImplementedError

    def call_environment(self):
        """Context manager active around the inferior calls (unwind on crash etc.)."""
        raise NotImplementedError

    def call(self, fn, addr, args, ret_size):
        """Call `fn` (FmtFn, may be None) at `addr` with `args`; return the first
        `ret_size` bytes of the return value as int, or None if unknown."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def to_bytes(val, size, order="little"):
    return (int(val) & ((1 << (8 * size)) - 1)).to_bytes(size, order)


def from_bytes(raw, order="little"):
    return int.from_bytes(bytes(raw), order)


# --------------------------------------------------------------------------
# choosing the fmt function
# --------------------------------------------------------------------------


def choose_debug_fmt(backend, info):
    """Return (FmtFn, mode) for the `Debug::fmt` of the value described by `info`.

    mode:  'ptr'    fn(&T, &mut Formatter)         pass the value's address
           'value'  fn(T, &mut Formatter)          pass the value itself (fat pointers)
           'ptrptr' fn(&&T, &mut Formatter)        `<&T as Debug>::fmt`, one more indirection
           'slice'  fn(&[E], &mut Formatter)       `<[E] as Debug>::fmt` for an array
    """
    tname = info.type_name
    wanted = {
        backend.ptr_name(tname): ("ptr", 0),
        tname: ("value", 1),
        backend.ptr_name(tname, 2): ("ptrptr", 2),
    }
    if info.kind == "array" and info.elem_type_name is not None:
        for s in backend.array_ptr_names(info.elem_type_name, info.array_len):
            wanted.setdefault(s, ("ptr", 0))
        for s in backend.slice_names(info.elem_type_name):
            wanted.setdefault(s, ("slice", 3))

    best = None
    rejected = []
    for fn in backend.functions_named("fmt"):
        if len(fn.params) != 2 or FORMATTER_TYPE not in fn.params[1]:
            continue
        if not looks_like_fmt(fn.demangled) and not looks_like_fmt(fn.name):
            continue
        hit = wanted.get(fn.params[0])
        if hit is None:
            continue
        mode, rank = hit
        if not is_debug_impl(fn.demangled):
            rejected.append(fn.demangled)
            continue
        if not fn.addr:
            continue
        # `&T`, `*const T` and `*mut T` may all be spelled the same by the
        # debugger; prefer the reference impl (prints the pointee).
        key = (rank, fn.demangled.startswith("<*"))
        if best is None or key < best[1]:
            backend.log("candidate %s @ %#x (%s)" % (fn.demangled, fn.addr, mode))
            best = ((fn, mode), key)
    if best is None:
        if rejected:
            raise RfmtError(
                "no `<%s as core::fmt::Debug>::fmt` in the binary (found only: %s)"
                % (tname, ", ".join(sorted(set(rejected))[:3]))
            )
        raise RfmtError(
            "no `<%s as core::fmt::Debug>::fmt` in the binary; the program has to "
            "format this type with {:?} somewhere for rustc to emit it" % tname
        )
    return best[0]


# --------------------------------------------------------------------------
# String
# --------------------------------------------------------------------------


def string_layout(layout, ptr_size):
    """(cap_off, ptr_off, len_off, size) of String from its TypeLayout (or fallback)."""
    fallback = (0, ptr_size, 2 * ptr_size, 3 * ptr_size)
    if layout is None:
        return fallback
    leaves = layout.leaf_offsets()
    cap = leaves.get("cap")
    ptr = leaves.get("pointer", leaves.get("ptr"))
    ln = leaves.get("len")
    if None in (cap, ptr, ln):
        return fallback
    return cap, ptr, ln, layout.size or fallback[3]


def string_new_bytes(slayout, ptr_size, order):
    cap_off, ptr_off, len_off, size = slayout
    hdr = bytearray(size)
    hdr[cap_off : cap_off + ptr_size] = to_bytes(0, ptr_size, order)
    hdr[ptr_off : ptr_off + ptr_size] = to_bytes(1, ptr_size, order)  # dangling, align 1
    hdr[len_off : len_off + ptr_size] = to_bytes(0, ptr_size, order)
    return bytes(hdr)


def parse_string_header(raw, slayout, ptr_size, order):
    cap_off, ptr_off, len_off, _size = slayout
    cap = from_bytes(raw[cap_off : cap_off + ptr_size], order)
    ptr = from_bytes(raw[ptr_off : ptr_off + ptr_size], order)
    ln = from_bytes(raw[len_off : len_off + ptr_size], order)
    return cap, ptr, ln


# --------------------------------------------------------------------------
# <String as core::fmt::Write> vtable
# --------------------------------------------------------------------------


class StringVTable(object):
    def __init__(self, drop, write_str, write_char, write_fmt):
        self.drop = drop
        self.write_str = write_str
        self.write_char = write_char
        self.write_fmt = write_fmt

    def as_bytes(self, string_size, string_align, ptr_size, order):
        return b"".join(
            to_bytes(x, ptr_size, order)
            for x in (self.drop, string_size, string_align, self.write_str, self.write_char, self.write_fmt)
        )


def resolve_string_vtable(backend):
    string_ptr = backend.ptr_name(STRING_TYPE)

    def by_signature(suffix):
        for fn in backend.functions_named(suffix):
            if fn.params and fn.params[0] == string_ptr and fn.addr:
                return fn.addr
        return 0

    drop = backend.function_addr(DROP_STRING_NAMES) or by_signature("drop_in_place") or by_signature("drop_glue")
    write_str = backend.function_addr([STRING_WRITE_STR]) or by_signature("write_str")
    write_char = backend.function_addr([STRING_WRITE_CHAR]) or by_signature("write_char")
    write_fmt = backend.function_addr(STRING_WRITE_FMT_NAMES) or by_signature("write_fmt")
    vt = StringVTable(drop, write_str, write_char, write_fmt)
    backend.log(
        "String vtable: drop=%#x write_str=%#x write_char=%#x write_fmt=%#x"
        % (drop, write_str, write_char, write_fmt)
    )
    if not (drop and write_str and write_char):
        raise RfmtError(
            "cannot build <String as core::fmt::Write> vtable (drop=%#x write_str=%#x "
            "write_char=%#x); is the binary stripped?" % (drop, write_str, write_char)
        )
    return vt


# --------------------------------------------------------------------------
# Formatter
# --------------------------------------------------------------------------


def build_formatter(layout, string_hdr, vtable, pretty, ptr_size, order):
    """Bytes of a `core::fmt::Formatter` whose sink is the String at `string_hdr`."""
    ps = ptr_size
    if layout is None or layout.size == 0 or not layout.fields:
        # rustc >= 1.87: buf {pointer, vtable}, options {flags u32, width u16, precision u16}
        buf = bytearray(2 * ps + 8)
        buf[0:ps] = to_bytes(string_hdr, ps, order)
        buf[ps : 2 * ps] = to_bytes(vtable, ps, order)
        flags = ord(" ") | ALIGN_UNKNOWN | ALWAYS_SET | (ALTERNATE_FLAG if pretty else 0)
        buf[2 * ps : 2 * ps + 4] = to_bytes(flags, 4, order)
        return bytes(buf)

    buf = bytearray(layout.size)

    def put(off, val, size):
        buf[off : off + size] = to_bytes(val, size, order)

    bf = layout.field("buf")
    if bf is None:
        raise RfmtError("core::fmt::Formatter has no `buf` field, unsupported rustc")
    sub = bf.layout
    if sub is not None and sub.field("pointer") is not None and sub.field("vtable") is not None:
        put(bf.offset + sub.field("pointer").offset, string_hdr, ps)
        put(bf.offset + sub.field("vtable").offset, vtable, ps)
    else:
        put(bf.offset, string_hdr, ps)
        put(bf.offset + ps, vtable, ps)

    of = layout.field("options")
    if of is not None and of.layout is not None:
        opt_off, ofields = of.offset, of.layout.fields
    else:
        # rustc 1.81..1.85: the option fields live directly in Formatter
        opt_off, ofields = 0, [f for f in layout.fields if f.name != "buf"]
    legacy = any(f.name == "fill" for f in ofields)
    for f in ofields:
        off = opt_off + f.offset
        if f.name == "flags":
            if legacy:
                val = LEGACY_ALTERNATE if pretty else 0
            else:
                val = ord(" ") | ALIGN_UNKNOWN | ALWAYS_SET | (ALTERNATE_FLAG if pretty else 0)
            put(off, val, f.size)
        elif f.name == "fill":
            put(off, ord(" "), f.size)
        elif f.name == "align":
            # rt::Alignment::Unknown == 3, and Option<Alignment>::None uses niche 3
            put(off, 3, f.size)
        # width / precision: zero bytes == None / not set
    return bytes(buf)


# --------------------------------------------------------------------------
# scalars without calling into the debuggee
# --------------------------------------------------------------------------


def scalar_fallback(info):
    """`{:?}` of a primitive, or None if `info` is not one we can do locally."""
    v = info.scalar
    if v is None:
        return None
    try:
        if info.kind == "bool":
            return "true" if v else "false"
        if info.kind == "char":
            ch = chr(int(v))
            esc = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\\": "\\\\", "'": "\\'", "\0": "\\0"}
            return "'%s'" % esc.get(ch, ch)
        if info.kind in ("int", "uint"):
            return str(int(v))
        if info.kind == "float":
            f = float(v)
            if f != f:
                return "NaN"
            if f in (float("inf"), float("-inf")):
                return "inf" if f > 0 else "-inf"
            r = repr(f)
            return r.replace("e+", "e") if "e" in r else r
        if info.kind == "void":
            return "()"
    except (ValueError, OverflowError):
        return None
    return None


# --------------------------------------------------------------------------
# the whole thing
# --------------------------------------------------------------------------


def debug_format(backend, info, pretty=False):
    """Return the `{:?}` (or `{:#?}`) rendering of the value described by `info`."""
    if info.optimized_out:
        raise RfmtError("value is optimized out")
    if info.kind == "void":
        return "()"

    try:
        fn, mode = choose_debug_fmt(backend, info)
    except RfmtError:
        text = scalar_fallback(info)
        if text is None:
            raise
        backend.log("no fmt for %s, formatted the scalar locally" % info.type_name)
        return text

    ps = backend.ptr_size()
    order = backend.byteorder()
    formatter_layout = backend.formatter_layout(fn)
    vtable = resolve_string_vtable(backend)
    slayout = string_layout(backend.string_layout(), ps)
    string_size = slayout[3]

    need_copy = info.address is None
    if need_copy:
        if info.raw is None:
            raise RfmtError("value has no address and cannot be copied")
        if len(info.raw) > MAX_COPY:
            raise RfmtError("value has no address and is too large to copy (%d bytes)" % len(info.raw))
    copy_size = len(info.raw) if need_copy else 0

    with backend.scratch(256 + copy_size) as base:
        string_hdr = base
        vtable_at = base + 64
        formatter_at = base + 128
        slot_at = base + 192
        copy_at = base + 256

        backend.write_memory(string_hdr, string_new_bytes(slayout, ps, order))
        backend.write_memory(vtable_at, vtable.as_bytes(string_size, ps, ps, order))
        backend.write_memory(
            formatter_at, build_formatter(formatter_layout, string_hdr, vtable_at, pretty, ps, order)
        )

        if need_copy:
            backend.write_memory(copy_at, bytes(info.raw))
            self_addr = copy_at
            backend.log("value has no address, copied %d bytes to %#x" % (copy_size, copy_at))
        else:
            self_addr = info.address

        if mode == "ptr":
            self_arg = Arg.int(self_addr)
        elif mode == "ptrptr":
            backend.write_memory(slot_at, to_bytes(self_addr, ps, order))
            self_arg = Arg.int(slot_at)
        elif mode == "value":
            if need_copy or info.native is None:
                raw = info.raw if need_copy else backend.read_memory(self_addr, info.size)
                backend.write_memory(slot_at, bytes(raw))
                self_arg = Arg.fat_at(slot_at)
            else:
                self_arg = Arg.native(info.native)
        elif mode == "slice":
            backend.write_memory(slot_at, to_bytes(self_addr, ps, order) + to_bytes(info.array_len, ps, order))
            self_arg = Arg.fat_at(slot_at)
        else:
            raise RfmtError("internal: unknown mode %r" % mode)

        backend.log(
            "calling %s (%s) self@%#x formatter=%#x" % (fn.demangled, mode, self_addr, formatter_at)
        )

        text = None
        err = None
        with backend.call_environment():
            try:
                res = backend.call(fn, fn.addr, [self_arg, Arg.int(formatter_at)], 1)
                if res:
                    err = "Debug::fmt returned Err"
            except RfmtError as e:
                err = "Debug::fmt failed: %s" % e

            cap = 0
            try:
                raw = backend.read_memory(string_hdr, string_size)
                cap, ptr, ln = parse_string_header(raw, slayout, ps, order)
                if ln > cap:
                    raise RfmtError("corrupted String header after call (len %d > cap %d)" % (ln, cap))
                text = backend.read_memory(ptr, ln).decode("utf-8", "replace") if ln else ""
            finally:
                if cap:
                    try:
                        backend.call(None, vtable.drop, [Arg.int(string_hdr)], 0)
                    except RfmtError as e:
                        backend.log("drop_in_place::<String> failed, leaking %d bytes: %s" % (cap, e))

    if err:
        raise RfmtError("%s%s" % (err, (" (partial output: %r)" % text) if text else ""))
    return text


def render_variable(backend, name, info, native_text, pretty):
    """One `name = value` line for rlocals/rargs, falling back to the debugger's text."""
    try:
        return "%s = %s" % (name, debug_format(backend, info, pretty))
    except RfmtError as e:
        backend.log("%s: %s" % (name, e))
        return "%s = %s    (fallback: %s)" % (name, native_text, e)


def parse_slash(arg):
    """'/p rest' or '-p rest' -> (pretty, 'rest'); raises RfmtError on unknown modifiers.

    lldb reserves the `cmd/x` syntax for its own gdb-format option, so the
    `-p` / `--pretty` spelling is accepted everywhere too.
    """
    arg = arg.strip()
    pretty = False
    m = re.match(r"(?:-p|--pretty)(?:\s+|$)(.*)$", arg, re.S)
    if m:
        return True, m.group(1)
    if arg.startswith("/"):
        m = re.match(r"/(\w*)\s*(.*)$", arg, re.S)
        flags, arg = m.group(1), m.group(2)
        for ch in flags:
            if ch == "p":
                pretty = True
            else:
                raise RfmtError("unknown modifier /%s (only /p is supported)" % ch)
    return pretty, arg
