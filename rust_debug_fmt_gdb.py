# rust-debug-fmt entry point for gdb.
#
#   (gdb) source /path/to/rust_debug_fmt_gdb.py
#
# or once and for all:  echo 'source /path/to/rust_debug_fmt_gdb.py' >> ~/.gdbinit
#
# Adds the commands rprint, rlocals, rargs and the convenience function $rfmt().
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

import rustdebugfmt.gdb_backend as _backend  # noqa: E402

_backend.register()
