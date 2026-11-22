"""Run candidate tests on the patched tree inside the container.

The parent (main.py) has pytest imported but never sqlglot. Each job forks a
child that builds a symlink overlay of the pinned checkout in /tmp with the
patched files written for real, imports the patched sqlglot, and runs only the
wanted tests. Results come back through a JSON file in /tmp.
"""

import json
import os
import shutil
import signal
import sys
import time

SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
TMP = os.environ.get("BR_TMP", "/tmp/br")
TEST_TIMEOUT_S = 15  # our cheap tests take <= ~1.5 s CPU each


def build_overlay(dst: str, files: dict) -> None:
    tree: dict = {}
    for path, content in files.items():
        node = tree
        parts = path.split("/")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                return  # file/dir clash: give up on this path
        node[parts[-1]] = content

    def build(d: str, s: str, sub: dict) -> None:
        os.mkdir(d)
        names = set(os.listdir(s)) if os.path.isdir(s) else set()
        for name in names | set(sub):
            sp, dp = os.path.join(s, name), os.path.join(d, name)
            if name in sub:
                node = sub[name]
                if isinstance(node, dict):
                    build(dp, sp, node)
                elif node is not None:
                    with open(dp, "w", encoding="utf-8", newline="") as fh:
                        fh.write(node)
            else:
                os.symlink(sp, dp)

    build(dst, SRC, tree)


class _Collector:
    def __init__(self, wanted: set, cpu_cap: float = 1e9):
        self.wanted = wanted
        self.found: set = set()
        self.failed: set = set()
        self.bad_files: set = set()
        self.done: set = set()  # tests whose teardown finished
        self.cpu_cap = cpu_cap
        self.session = None

    def pytest_sessionstart(self, session):
        self.session = session

    def pytest_collection_modifyitems(self, session, config, items):
        keep, drop = [], []
        for it in items:
            (keep if it.nodeid in self.wanted else drop).append(it)
        if drop:
            config.hook.pytest_deselected(items=drop)
        items[:] = keep
        self.found = {it.nodeid for it in keep}

    def pytest_collectreport(self, report):
        if report.failed:
            self.bad_files.add(report.nodeid.split("::")[0])

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed.add(report.nodeid)
        if report.when == "teardown":
            self.done.add(report.nodeid)
            if self.session is None:
                return
            # over budget: stop between tests; whatever did not run counts as failing
            if time.process_time() > self.cpu_cap:
                self.session.shouldstop = "cpu cap"
            # nearly everything failing so far (a broad break): the rest would
            # almost surely fail too, and failing tests are the slow ones
            nf = len(self.failed & self.done)
            if nf >= 5 and nf >= 0.8 * len(self.done):
                self.session.shouldstop = "mostly failing"


def _inline_pools() -> None:
    """Run ProcessPoolExecutor work in-process.

    test_optimizer/test_executor push work through spawn pools; each spawned
    worker re-imports pandas/duckdb/sqlglot (~1 s CPU each). Running inline keeps
    the observable behaviour that matters: arguments and results still make a
    pickle round trip (serde mutations break exactly there), and exceptions
    surface through the future as before.
    """
    import concurrent.futures as cf
    import pickle

    class InlineExecutor(cf.Executor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None,
                     initargs=(), **kwargs):
            if initializer is not None:
                initializer(*pickle.loads(pickle.dumps(initargs)))

        def submit(self, fn, /, *args, **kwargs):
            fut = cf.Future()
            try:
                fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
                fut.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
            except BaseException as e:  # noqa
                fut.set_exception(e)
            return fut

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

    cf.ProcessPoolExecutor = InlineExecutor
    try:
        import concurrent.futures.process as cfp
        cfp.ProcessPoolExecutor = InlineExecutor
    except Exception:
        pass
    # anything still spawning real pools should not size them by host cores
    os.process_cpu_count = lambda: 2
    os.cpu_count = lambda: 2


def preload() -> None:
    """Heavy third-party imports the tests need, done once before forking."""
    for name in ("numpy", "pandas", "duckdb", "pandas.testing", "unittest", "unittest.mock"):
        try:
            __import__(name)
        except Exception:
            pass


def _child(job_dir: str, files: dict, wanted: list[str], out: str, cpu_cap: float) -> None:
    os.setsid()
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    import pytest

    build_overlay(job_dir, files)
    os.chdir(job_dir)
    sys.path.insert(0, job_dir)
    os.environ["PYTHONPATH"] = job_dir
    os.environ["SKIP_INTEGRATION"] = "0"
    _inline_pools()
    col = _Collector(set(wanted), cpu_cap)
    test_files = sorted({w.split("::")[0] for w in wanted})
    rc = pytest.main(
        ["-q", "-p", "no:cacheprovider", "-o", "addopts=", "--no-header", "-W", "ignore",
         # pass/fail is all we need: formatting deep-recursion tracebacks for
         # every failing subtest can cost tens of seconds
         "--tb=no", "--show-capture=no", "-p", "no:logging",
         # a hung test is a failing test (the labels count timeouts as failures)
         "--timeout", str(TEST_TIMEOUT_S)]
        + test_files,
        plugins=[col],
    )
    failed = set(col.failed)
    for w in wanted:
        f = w.split("::")[0]
        # not collected, not finished (cpu cap / interrupted), or its file broke
        if w not in col.done or f in col.bad_files:
            failed.add(w)
    with open(out + ".part", "w") as fh:
        json.dump({"rc": int(rc), "failed": sorted(failed)}, fh)
    os.rename(out + ".part", out)


class Job:
    def __init__(self, key: str, files: dict, wanted: list[str], timeout: float,
                 cpu_cap: float = 1e9):
        self.key = key
        self.dir = os.path.join(TMP, key)
        self.out = os.path.join(TMP, key + ".json")
        self.wanted = wanted
        self.timeout = timeout
        sys.stdout.flush()
        sys.stderr.flush()
        self.pid = os.fork()
        if self.pid == 0:
            try:
                _child(self.dir, files, wanted, self.out, cpu_cap)
            except BaseException as e:  # noqa
                try:
                    with open(self.out + ".err", "w") as fh:
                        fh.write(repr(e))
                except Exception:
                    pass
            finally:
                os._exit(0)
        self.start = time.monotonic()
        self.result: set | None = None
        self.cpu = -1.0

    def poll(self) -> bool:
        """True when finished (result set: failed nodeids, all wanted on error)."""
        pid, _, ru = os.wait4(self.pid, os.WNOHANG)
        if pid:
            self.cpu = ru.ru_utime + ru.ru_stime
        if pid == 0:
            if time.monotonic() - self.start < self.timeout:
                return False
            self.kill()
            print(f"[verify] {self.key} timed out", file=sys.stderr)
        self._finish()
        return True

    def kill(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass

    def _finish(self) -> None:
        try:
            os.killpg(self.pid, signal.SIGKILL)  # stray pool workers
        except OSError:
            pass
        try:
            with open(self.out) as fh:
                self.result = set(json.load(fh)["failed"])
        except Exception:
            err = ""
            try:
                err = open(self.out + ".err").read()
            except Exception:
                pass
            print(f"[verify] {self.key} no result {err}", file=sys.stderr)
            self.result = set(self.wanted)  # unknown -> assume all fail
        shutil.rmtree(self.dir, ignore_errors=True)
        for p in (self.out, self.out + ".err"):
            try:
                os.remove(p)
            except OSError:
                pass
