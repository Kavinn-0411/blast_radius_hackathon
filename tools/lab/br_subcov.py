"""Auto-started (via .pth) in every Python process when BR_TRACE_DIR is set:
records sqlglot line coverage and dumps it on exit, tagged with BR_TEST."""
import os

_dir = os.environ.get("BR_TRACE_DIR")
if _dir and os.environ.get("BR_TEST"):
    import atexit
    import json

    import coverage

    _cov = coverage.Coverage(data_file=None, include=["/sqlglot/sqlglot/*"], config_file=False)
    _cov.start()
    import sys as _sys
    _sys.path.insert(0, "/tools")
    try:
        import br_classes as _brc
        _brc.start()
    except Exception:
        _brc = None

    def _dump():
        try:
            _cov.stop()
            d = _cov.get_data()
            lines = {f[len("/sqlglot/"):]: sorted(d.lines(f)) for f in d.measured_files() if d.lines(f)}
            if _brc is not None:
                lines["__classes__"] = _brc.snapshot()
            p = os.path.join(_dir, f"{os.environ['BR_TEST']}-{os.getpid()}.json")
            with open(p, "w") as fh:
                json.dump(lines, fh)
        except Exception:
            pass

    atexit.register(_dump)
    try:  # multiprocessing children may skip atexit; hook their finalizers too
        from multiprocessing import util as _u
        _u.Finalize(None, _dump, exitpriority=-100)
    except Exception:
        pass
