# Blast Radius selector

Chooses which sqlglot tests to run for each patch (sqlglot pinned at `eb11aec`) and splits them over
up to 8 workers. Cost per commit = busiest worker + 2 s/worker, +300 s if any failing test is left
out, plus the container's CPU. See [SCORING.md](SCORING.md).

## How it works

**Offline, once, from the pinned commit only** (`tools/`, output committed as `app/map.json`):

- `tools/lab/trace_tests.py` runs every test in its own forked process. It records the sqlglot lines
  the test executes (coverage, including the spawned process-pool workers used by the
  optimizer/executor tests), the repo files it opens (fixtures), and the sqlglot classes it
  instantiates (with their MRO).
- `tools/build_map.py` turns that into function-level test sets (a changed line maps to every test
  that ran any part of its enclosing function, which survives short-circuit `and`/`or` flips),
  class-usage sets for class-body edits, fixture readers, statement spans, and test spans.
- `tools/lab/cost_tests.py` measures each cheap test's CPU under the selector's own harness.

**At run time, per commit** (`app/`):

1. `analyze.py`: apply the patch in memory. For Python files, only the edited statement is parsed,
   old vs new. If the ASTs are equal (comments, formatting) the edit is a no-op. A syntax error means
   the module's importers fail.
2. `candidates.py`: map the edits to candidate tests (function → class users → module family).
   Fixture edits map to the tests that read the file; test-file edits map to the affected tests.
3. `policy.py`: giant tests (≥ 1 s) are never run by us. Cheap candidates are run inside the
   container (`verify.py`) on a `/tmp` symlink overlay of the patched tree, forked from a warm
   pytest, with process pools replaced by an inline, pickle-faithful executor. Only the tests that
   actually fail are selected. A giant is dropped only when cheap tests that ran the same changed
   function were run and all passed.
4. `pack.py`: LPT packing, choosing the worker count that minimises busiest load + 2 s × workers.

Every failure path falls back to selecting more, never less: analysis errors select every test,
check-run timeouts treat the checked tests as failing, and a batch-wide CPU budget plus deadline
switch to blind selection.

## Develop

```sh
bash sample.sh                       # build + run under scorer limits + score (no make on Windows)
SET=work/mut1 bash sample.sh         # same, on another labeled commit set
python tools/simulate.py sample      # offline policy simulation (perfect check runs)
python tools/test_analyze.py         # unusual patch shapes
```

Lab image (`tools/lab/Dockerfile`): the scorer's base digest, `requirements.lock`, sqlglot at the
pinned commit, and coverage. Used for tracing, cost measurement, and labeling synthetic mutants
(`tools/lab/mutants.py`, `tools/lab/label.py`). Mutants are only used to evaluate the selector
locally; they are never shipped or looked up.
