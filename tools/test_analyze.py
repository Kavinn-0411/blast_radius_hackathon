"""Offline checks of patch parsing / analysis on unusual diff shapes."""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("BR_SRC", str(ROOT / "work/sqlglot"))

from analyze import Analyzer  # noqa: E402
from candidates import Map  # noqa: E402

tests = json.load(open(ROOT / "sample/tests.json"))["tests"]
m = Map(json.load(open(ROOT / "app/map.json")))
for t in tests:
    m.test_file_of[t["number"]] = (t["file"], t["id"])
an = Analyzer(m, [t["number"] for t in tests])
SRC = Path(os.environ["BR_SRC"])


def lines(path):
    return (SRC / path).read_text(encoding="utf-8").split("\n")


def check(name, patch, expect):
    try:
        r = an.analyze(patch)
        ok = expect(r)
        print(f"{'ok  ' if ok else 'FAIL'} {name}: tests={len(r.tests)} broken={r.import_break} "
              f"files={list(r.files)} notes={r.notes}")
    except Exception as e:
        print(f"CRASH {name}: {e!r}")


trie = lines("sqlglot/trie.py")
# 1. new file (untracked module)
check("new module", """diff --git a/sqlglot/newmod.py b/sqlglot/newmod.py
new file mode 100644
index 0000000..e69de29
--- /dev/null
+++ b/sqlglot/newmod.py
@@ -0,0 +1,2 @@
+X = 1
+Y = 2
""", lambda r: "sqlglot/newmod.py" in r.files)

# 2. deleted module
body = "".join("-" + l + "\n" for l in trie[:-1])
check("deleted module", f"""diff --git a/sqlglot/trie.py b/sqlglot/trie.py
deleted file mode 100644
index 1111111..0000000
--- a/sqlglot/trie.py
+++ /dev/null
@@ -1,{len(trie) - 1} +0,0 @@
{body}""", lambda r: r.files.get("sqlglot/trie.py", 1) is None and len(r.tests) > 1000)

# 3. insertion at top of file (comment) -> noop
check("comment at line 1", f"""diff --git a/sqlglot/trie.py b/sqlglot/trie.py
index 1111111..2222222 100644
--- a/sqlglot/trie.py
+++ b/sqlglot/trie.py
@@ -0,0 +1 @@
+# header comment
""", lambda r: not r.tests)

# 4. syntax error introduced
check("syntax error", f"""diff --git a/sqlglot/trie.py b/sqlglot/trie.py
index 1111111..2222222 100644
--- a/sqlglot/trie.py
+++ b/sqlglot/trie.py
@@ -1,3 +1,3 @@
-{trie[0]}
+{trie[0]} )(
 {trie[1]}
 {trie[2]}
""", lambda r: r.import_break)

# 5. coding cookie comment -> must not be a noop
check("coding cookie", f"""diff --git a/sqlglot/trie.py b/sqlglot/trie.py
index 1111111..2222222 100644
--- a/sqlglot/trie.py
+++ b/sqlglot/trie.py
@@ -0,0 +1 @@
+# -*- coding: latin-1 -*-
""", lambda r: r.tests or r.import_break)

# 6. "# comment" inserted inside a multi-line string
gen = lines("sqlglot/generator.py")
idx = next(i for i, l in enumerate(gen) if l.strip().startswith('"""') and not l.strip().endswith('"""', 3))
check("comment inside docstring", f"""diff --git a/sqlglot/generator.py b/sqlglot/generator.py
index 1111111..2222222 100644
--- a/sqlglot/generator.py
+++ b/sqlglot/generator.py
@@ -{idx + 1},2 +{idx + 1},3 @@
 {gen[idx]}
+    # not really a comment
 {gen[idx + 1]}
""", lambda r: "noop:sqlglot/generator.py" not in r.notes)

# 7. no newline at end of file marker
check("no newline at EOF", f"""diff --git a/sqlglot/trie.py b/sqlglot/trie.py
index 1111111..2222222 100644
--- a/sqlglot/trie.py
+++ b/sqlglot/trie.py
@@ -{len(trie) - 1},1 +{len(trie) - 1},2 @@
 {trie[-2]}
+# trailing
\\ No newline at end of file
""", lambda r: True)

# 8. rename of a test fixture
check("rename fixture", """diff --git a/tests/fixtures/optimizer/simplify.sql b/tests/fixtures/optimizer/simplify2.sql
similarity index 100%
rename from tests/fixtures/optimizer/simplify.sql
rename to tests/fixtures/optimizer/simplify2.sql
""", lambda r: True)

# 9. README / docs change
check("readme", """diff --git a/README.md b/README.md
index 1111111..2222222 100644
--- a/README.md
+++ b/README.md
@@ -1,1 +1,2 @@
 # SQLGlot
+hello
""", lambda r: True)

# 10. edit to a test method in a test file
tp = lines("tests/test_parser.py")
k = next(i for i, l in enumerate(tp) if "def test_union" in l)
check("test method edit", f"""diff --git a/tests/test_parser.py b/tests/test_parser.py
index 1111111..2222222 100644
--- a/tests/test_parser.py
+++ b/tests/test_parser.py
@@ -{k + 2},1 +{k + 2},1 @@
-{tp[k + 1]}
+{tp[k + 1]}  ;assert False
""", lambda r: 0 < len(r.tests) < 5)
