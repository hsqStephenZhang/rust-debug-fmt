# rust-debug-fmt entry point for lldb.
#
#   (lldb) command script import /path/to/rust_debug_fmt_lldb.py
#
# or once and for all:
#   echo 'command script import /path/to/rust_debug_fmt_lldb.py' >> ~/.lldbinit
#
# Adds the commands rprint, rlocals, rargs and rfmt-set.
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)


def __lldb_init_module(debugger, internal_dict):
    import rustdebugfmt.lldb_backend as backend

    backend.register(debugger)
