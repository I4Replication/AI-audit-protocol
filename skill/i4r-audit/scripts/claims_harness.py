#!/usr/bin/env python3
"""Which standalone harness an audit's published claims script carries (2.7.11).

make_claims_script.sh gives an R claims script the verification notebook's setup (2.7.10). 2.7.11 adds what a
replicator's machine needs on top of it: the script switches a non-UTF-8 R session to UTF-8 before anything else,
says which R it needs, names the programs outside R its checks run (pdftotext and the other Poppler tools, python3
and the modules it imports, macOS's textutil) with the versions the audit recorded and checks them up front, its
header describes the folder layout the code reads, and a manual check that names the audit's own working files says
they are not published.

The harness is frozen per audit, in the policy snapshot leaf ``publication.claims_script_harness``:

- absent: ``LEGACY_HARNESS``. Every audit initialised before 2.7.11 froze no leaf, and its claims script is rebuilt
  byte for byte as 2.7.10 built it: a rebuild never changes what generate_finding_artifacts.py's projection
  manifest binds, and a published script is never changed. There is NO fallback to the packaged policy.
- ``SHIPPED_HARNESS``: the 2.7.11 additions above.

Stdlib only, like claim_change_rules.py: the drift check loads it by path. The post-delta receipt's CODE_PATHS files
never import it.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Mapping

LEGACY_HARNESS = "notebook_setup_1"
SHIPPED_HARNESS = "portable_1"
LIVE_HARNESSES: tuple[str, ...] = (LEGACY_HARNESS, SHIPPED_HARNESS)
LEAF = ("publication", "claims_script_harness")

# The standard library's top-level module names (the union of sys.stdlib_module_names in Python 3.11 to 3.15, plus
# the 3.9/3.10 modules since removed). The portable_1 harness leaves these out of the Python modules a claims script
# declares. It is a fixed list, never the building Python's own: the builder runs under whatever python3 is first on
# PATH (3.9 has no sys.stdlib_module_names at all), and the script's bytes must not depend on it. Part of portable_1:
# never edit it; a different list is a new harness value.
STDLIB_MODULES = frozenset("""
__future__ _abc _aix_support _android_support _apple_support _ast _ast_unparse _asyncio _bisect _blake2 _bootlocale
_bootsubprocess _bz2 _codecs _codecs_cn _codecs_hk _codecs_iso2022 _codecs_jp _codecs_kr _codecs_tw _collections
_collections_abc _colorize _compat_pickle _compression _contextvars _crypt _csv _ctypes _curses _curses_panel
_datetime _dbm _decimal _elementtree _frozen_importlib _frozen_importlib_external _functools _gdbm _hashlib _heapq
_hmac _imp _interpchannels _interpqueues _interpreters _io _ios_support _json _locale _lsprof _lzma _markupbase
_math_integer _md5 _msi _multibytecodec _multiprocessing _opcode _opcode_metadata _operator _osx_support _overlapped
_pickle _posixshmem _posixsubprocess _py_abc _py_warnings _pydatetime _pydecimal _pyio _pylong _pyrepl _queue
_random _remote_debugging _scproxy _sha1 _sha2 _sha256 _sha3 _sha512 _signal _sitebuiltins _socket _sqlite3 _sre
_ssl _stat _statistics _string _strptime _struct _suggestions _symtable _sysconfig _thread _threading_local _tkinter
_tokenize _tracemalloc _types _typing _uuid _warnings _weakref _weakrefset _winapi _wmi _zoneinfo _zstd abc aifc
annotationlib antigravity argparse array ast asynchat asyncio asyncore atexit audioop base64 bdb binascii binhex
bisect builtins bz2 cProfile calendar cgi cgitb chunk cmath cmd code codecs codeop collections colorsys compileall
compression concurrent configparser contextlib contextvars copy copyreg crypt csv ctypes curses dataclasses datetime
dbm decimal difflib dis distutils doctest email encodings ensurepip enum errno faulthandler fcntl filecmp fileinput
fnmatch formatter fractions ftplib functools gc genericpath getopt getpass gettext glob graphlib grp gzip hashlib
heapq hmac html http idlelib imaplib imghdr imp importlib inspect io ipaddress itertools json keyword lib2to3
linecache locale logging lzma mailbox mailcap marshal math mimetypes mmap modulefinder msilib msvcrt multiprocessing
netrc nis nntplib nt ntpath nturl2path numbers opcode operator optparse os ossaudiodev parser pathlib pdb pickle
pickletools pipes pkgutil platform plistlib poplib posix posixpath pprint profile profiling pstats pty pwd
py_compile pyclbr pydoc pydoc_data pyexpat queue quopri random re readline reprlib resource rlcompleter runpy sched
secrets select selectors shelve shlex shutil signal site smtpd smtplib sndhdr socket socketserver spwd sqlite3
sre_compile sre_constants sre_parse ssl stat statistics string stringprep struct subprocess sunau symbol symtable
sys sysconfig syslog tabnanny tarfile telnetlib tempfile termios textwrap this threading time timeit tkinter token
tokenize tomllib trace traceback tracemalloc tty turtle turtledemo types typing unicodedata unittest urllib uu uuid
venv warnings wave weakref webbrowser winreg winsound wsgiref xdrlib xml xmlrpc zipapp zipfile zipimport zlib
zoneinfo
""".split())


class HarnessError(ValueError):
    pass


def harness_of(policy: Mapping[str, Any] | None) -> str:
    """The harness a policy freezes: LEGACY_HARNESS without the leaf; refuse a value that is not a frozen live one."""
    node: Any = policy
    for key in LEAF:
        if not isinstance(node, Mapping) or key not in node:
            return LEGACY_HARNESS
        node = node[key]
    if node not in LIVE_HARNESSES or node == LEGACY_HARNESS:
        # LEGACY_HARNESS is implied by absence and never frozen, so a frozen "notebook_setup_1" is refused too
        raise HarnessError(f"{'.'.join(LEAF)} is {node!r}, not a packaged claims-script harness "
                           f"({SHIPPED_HARNESS!r}, or no leaf for the 2.7.10 harness)")
    return node


def frozen_harness(root: pathlib.Path) -> str:
    """The audit's harness, read ONLY from its own snapshot: LEGACY_HARNESS when there is none or it froze no leaf."""
    path = root / "pipeline-policy.snapshot.json"
    if not path.exists():
        return LEGACY_HARNESS
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarnessError(f"cannot read the audit policy snapshot {path}: {exc}") from exc
    return harness_of(snapshot.get("policy") if isinstance(snapshot, Mapping) else None)


if __name__ == "__main__":
    import sys
    try:
        print(frozen_harness(pathlib.Path(sys.argv[1])))
    except (IndexError, HarnessError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(2)
