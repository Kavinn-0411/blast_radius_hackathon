"""Record which sqlglot classes a process instantiates (via sys.monitoring CALL).

start() begins recording; snapshot() returns sorted "module:qualname" strings for
every class in the MRO of each instantiated sqlglot class.
"""

import sys

TOOL = 4
_seen: set = set()


def _on_call(code, offset, func, arg0):
    if isinstance(func, type):
        _seen.add(func)
    return None


def start() -> None:
    mon = sys.monitoring
    try:
        mon.use_tool_id(TOOL, "br_classes")
    except ValueError:
        pass
    mon.register_callback(TOOL, mon.events.CALL, _on_call)
    mon.set_events(TOOL, mon.events.CALL)


def stop() -> None:
    try:
        sys.monitoring.set_events(TOOL, 0)
    except Exception:
        pass


def snapshot() -> list:
    out = set()
    for cls in list(_seen):
        try:
            mro = cls.__mro__
        except Exception:
            continue
        for k in mro:
            mod = getattr(k, "__module__", "") or ""
            if mod.startswith("sqlglot"):
                out.add(f"{mod}:{getattr(k, '__qualname__', k.__name__)}")
    return sorted(out)
