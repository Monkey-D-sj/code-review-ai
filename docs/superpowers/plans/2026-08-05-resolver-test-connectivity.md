# Resolver Test-Connectivity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the parser/resolver so that tests that statically reference a changed symbol (via src-layout imports, relative imports, package `__init__` re-exports, or constructors) actually get a resolved call edge — raising SWE-bench recall@10 from 0.20 to ≥ 0.5.

**Architecture:** Four targeted fixes, two in `parser.py` (strip `src/` from module qnames; correct relative-import resolution) and two in `resolver.py` (follow package re-export chains; add constructor→`__init__` edges). The benchmark harness (`benchmark.py`/`run_swebench_suite.py`) and the manifest are untouched — `run_benchmark` re-runs on the cached 30-case suite to measure the parser improvements alone.

**Tech Stack:** Python 3.14, tree-sitter, SQLite, pytest (`uv run pytest`).

## Global Constraints

- Qualified names always go through `code_review_ai.qname` — never build/split by hand (`qname.join`, `qname.short`). Spec ref: `docs/superpowers/specs/2026-08-05-resolver-test-connectivity-design.md`.
- Edge `resolution` stays the trust signal: only `resolved` edges participate in flow traversal and benchmark candidates.
- Do **not** modify `benchmark._candidate_files`, `impact.get_impact`, the manifest `benchmarks/swe-bench-verified-30.json`, or `scripts/run_swebench_suite.py`.
- Acceptance: `uv run pytest` all green; rerun benchmark → `macro_test_file_recall_at_k >= 0.5` and precision not dropping sharply below the baseline 0.0789.
- Fixture repo (`tests/fixtures/repo`) has no `src/` layout and no relative imports — existing tests pin absolute-import behavior only.
- Project convention: function bodies ≤ 50 lines, no single-letter loop variables.

---

### Task 1: Strip `src/` from module qnames

**Files:**
- Modify: `code_review_ai/parser.py:234-239` (`_module_qname`)
- Test: `tests/test_parser.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_module_qname(file_path, repo_root) -> str` now drops a leading `src/` path component, so module qnames are python-visible names (`src/mypkg/service.py` → `mypkg.service`). `parse_file` signature unchanged.

- [ ] **Step 1: Write the failing test** (append to `tests/test_parser.py`)

```python
def test_module_qname_strips_src_layout(tmp_path):
    pkg = tmp_path / "src" / "mypkg"
    pkg.mkdir(parents=True)
    mod = pkg / "service.py"
    mod.write_text("def login():\n    return True\n", encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    assert pf.module_qname == "mypkg.service"
    assert Q("mypkg.service", "login") in {n.qualified_name for n in pf.nodes}

    init = pkg / "__init__.py"
    init.write_text("", encoding="utf-8")
    pfi = parse_file(str(init), str(tmp_path))
    assert pfi.module_qname == "mypkg"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_parser.py::test_module_qname_strips_src_layout -v`
Expected: FAIL with `assert pf.module_qname == "mypkg.service"` → actual `src.mypkg.service`.

- [ ] **Step 3: Write minimal implementation** (replace `_module_qname`)

```python
def _module_qname(file_path: str, repo_root: str) -> str:
    rel = Path(file_path).resolve().relative_to(Path(repo_root).resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_parser.py::test_module_qname_strips_src_layout -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_parser.py code_review_ai/parser.py
git commit -m "fix(parser): strip src/ layout root from module qnames"
```

---

### Task 2: Correct relative-import resolution

**Files:**
- Modify: `code_review_ai/parser.py:359` (`parse_file` call site), `code_review_ai/parser.py:448-492` (`_extract_imports`, `_extract_imports_python`)
- Test: `tests/test_parser.py`

**Interfaces:**
- Consumes: Task 1 — module qnames have no `src.` prefix, so `__init__.py` relative bases are correct.
- Produces: `_extract_imports(root, module_qname, lang, lang_name, file_path)` — new `file_path` param; `_extract_imports_python(root, module_qname, lang, file_path)`; relative imports now yield absolute module names (`pkg.sub`), and `__init__.py` uses **itself** as the relative base (ordinary modules use their parent package).

- [ ] **Step 1: Write the failing test** (append to `tests/test_parser.py`)

```python
def test_relative_import_resolves_absolute_module(tmp_path):
    mod = tmp_path / "a" / "b" / "c.py"
    mod.parent.mkdir(parents=True)
    mod.write_text("from .m import y\n", encoding="utf-8")
    pf = parse_file(str(mod), str(tmp_path))
    imp = {i.local_name: i for i in pf.imports}
    assert imp["y"].module == "a.b.m"


def test_relative_import_in_init_uses_package_base(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    init = pkg / "__init__.py"
    init.write_text("from .sub import Thing\n", encoding="utf-8")
    pf = parse_file(str(init), str(tmp_path))
    imp = {i.local_name: i for i in pf.imports}
    assert imp["Thing"].module == "pkg.sub"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parser.py::test_relative_import_resolves_absolute_module tests/test_parser.py::test_relative_import_in_init_uses_package_base -v`
Expected: FAIL with `imp["y"].module == "a.b.m"` → actual `.m` (module names carry the leading dot).

- [ ] **Step 3: Write minimal implementation**

In `parse_file`, pass `file_path` through:

```python
    pf.imports = _extract_imports(root, module_qname, lang, lang_name, file_path)
```

Change `_extract_imports` signature and dispatch:

```python
def _extract_imports(root, module_qname, lang, lang_name: str,
                     file_path: str) -> list[ImportEntry]:
    if lang_name == "python":
        return _extract_imports_python(root, module_qname, lang, file_path)
    return _extract_imports_esm(root, lang)
```

Change `_extract_imports_python` — package base and the relative-import block:

```python
def _extract_imports_python(root, module_qname, lang,
                            file_path: str) -> list[ImportEntry]:
    entries: list[ImportEntry] = []
    parts = module_qname.split(".") if module_qname else []
    is_init = Path(file_path).name == "__init__.py"
    pkg = parts if is_init else (parts[:-1] if parts else [])
    for node in root.children:
        if node.type not in lang["import_nodes"]:
            continue
        if node.type == "import_statement":
            # unchanged: import m / import m as x / import m.sub
            for child in node.children:
                if child.type == "dotted_name":
                    mod = child.text.decode("utf-8")
                    local = mod.split(".")[0]
                    entries.append(ImportEntry(local, mod, None, False))
                elif child.type == "aliased_import":
                    name = child.child_by_field_name("name").text.decode("utf-8")
                    alias = child.child_by_field_name("alias").text.decode("utf-8")
                    entries.append(ImportEntry(alias, name, None, False))
        elif node.type == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            sub = _dotted(mod_node)
            if mod_node is not None and mod_node.type == "relative_import":
                # tree-sitter nests the leading dots in relative_import; module_name
                # text is like ".sessions" / "..m" / "."
                leading = len(sub) - len(sub.lstrip("."))
                rest = sub[leading:]
                up = leading - 1
                base = pkg[: len(pkg) - up] if up <= len(pkg) else []
                module = ".".join(base + ([rest] if rest else []))
            else:
                module = sub  # absolute import
            for c in node.children:
                if c.type == "dotted_name" and (mod_node is None or c.start_byte != mod_node.start_byte):
                    name = c.text.decode("utf-8")
                    entries.append(ImportEntry(name, module, name, False))
                elif c.type == "aliased_import":
                    name = c.child_by_field_name("name").text.decode("utf-8")
                    alias = c.child_by_field_name("alias").text.decode("utf-8")
                    entries.append(ImportEntry(alias, module, name, False))
                elif c.type == "wildcard_import":
                    entries.append(ImportEntry("*", module, None, True))
    return entries
```

> Note: `import_statement` block shown unchanged for completeness; if your editor diff shows it as a re-indent only, that is expected — the semantic change is the `pkg`/`relative_import` handling above it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parser.py -v`
Expected: all PASS, including pre-existing `test_parse_extracts_calls_and_imports` (fixture repo has no relative imports).

- [ ] **Step 5: Commit**

```bash
git add tests/test_parser.py code_review_ai/parser.py
git commit -m "fix(parser): resolve relative imports against the real package base"
```

---

### Task 3: Follow package `__init__` re-export chains

**Files:**
- Modify: `code_review_ai/resolver.py:47-55` (`resolve_calls`), `code_review_ai/resolver.py:58-86` (`_resolve_one`), add `_resolve_reexport` after `_resolve_one`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: Task 2 — `__init__.py` import bindings are now correct absolute modules.
- Produces: `resolve_calls(parsed_files, existing_qnames) -> list[Edge]` — **signature unchanged** (the global import map is built internally); `_resolve_one(c, local, imports, existing, all_import_maps) -> Edge`; new module-level `_resolve_reexport(current, name, all_import_maps, existing, seen=None) -> str | None` (returns the real qname, or `None`).

- [ ] **Step 1: Write the failing test** (append to `tests/test_resolver.py`)

```python
def test_reexport_through_package_init(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .impl import Session\n", encoding="utf-8")
    (pkg / "impl.py").write_text("class Session:\n    pass\n", encoding="utf-8")
    consumer = tmp_path / "consumer.py"
    consumer.write_text(
        "from pkg import Session\n"
        "import pkg as p\n"
        "a = Session()\n"
        "b = p.Session()\n",
        encoding="utf-8",
    )
    files = [parse_file(str(pkg / "__init__.py"), str(tmp_path)),
             parse_file(str(pkg / "impl.py"), str(tmp_path)),
             parse_file(str(consumer), str(tmp_path))]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_calls(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # both `Session()` (from pkg import) and `p.Session()` (import pkg as p)
    # must resolve to the real class through the package __init__ re-export
    assert ("consumer", "pkg.impl::Session", "resolved") in by
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolver.py::test_reexport_through_package_init -v`
Expected: FAIL — the edge is `("consumer", "pkg::Session", "unresolved")` (target qname `pkg::Session` has no node).

- [ ] **Step 3: Write minimal implementation**

Replace `resolve_calls` to build the global import map and pass it down:

```python
def resolve_calls(parsed_files: list[ParsedFile], existing_qnames: set[str]) -> list[Edge]:
    mod_syms = _module_symbols(parsed_files)
    all_import_maps = {pf.module_qname: _import_map(pf) for pf in parsed_files}
    edges: list[Edge] = []
    for pf in parsed_files:
        local = mod_syms.get(pf.module_qname, {})
        imports = _import_map(pf)
        for c in pf.raw_calls:
            edges.append(_resolve_one(c, local, imports, existing_qnames, all_import_maps))
    return edges
```

Add the re-export helper after `_resolve_one`:

```python
def _resolve_reexport(current: str, name: str, all_import_maps: dict,
                      existing: set[str], seen: set[str] | None = None) -> str | None:
    """Follow a module's own import bindings to where `name` is re-exported from.

    binding is (module, imported_name, is_star); imported_name is the EXPORTED
    name (aliases like `from .m import X as Y` make it differ from the local
    name), so recursion carries binding[1], not `name`.
    """
    tgt = qname.join(current, name)
    if tgt in existing:
        return tgt
    if current in (seen or set()):
        return None  # import cycle
    binding = (all_import_maps.get(current) or {}).get(name)
    if not binding or not binding[1]:
        return None  # no binding / module import / star import
    return _resolve_reexport(binding[0], binding[1], all_import_maps,
                             existing, (seen or set()) | {current})
```

Update `_resolve_one` — signature gains `all_import_maps`; the two import-based branches fall back to re-export when the direct target doesn't exist:

```python
def _resolve_one(c: RawCall, local: dict, imports: dict,
                 existing: set[str], all_import_maps: dict) -> Edge:
    base = Edge(source=c.source_qname, target=c.target_expr, kind="call",
                file_path=c.file_path, call_line=c.call_line, resolution="unresolved")
    if c.call_form == CALL_SIMPLE:
        name = c.target_expr
        if name in local:
            return _resolved(base, local[name], existing)
        if name in imports:
            mod, imp_name, _star = imports[name]
            if imp_name:  # from m import name
                tgt = qname.join(mod, imp_name)
                if tgt not in existing:
                    tgt = _resolve_reexport(mod, imp_name, all_import_maps, existing) or tgt
                return _resolved(base, tgt, existing)
            return _resolved(base, mod, existing)  # imported module itself
        return base  # unresolved
    if c.call_form == CALL_ATTRIBUTE:
        head = c.target_expr.split(".", 1)[0]
        rest = c.target_expr[len(head) + 1:]
        if head in imports:
            mod, imp_name, _ = imports[head]
            if imp_name is None:  # import m / import m as head -> m.rest
                tgt = qname.join(mod, rest)
                if tgt not in existing:
                    tgt = _resolve_reexport(mod, rest, all_import_maps, existing) or tgt
                return _resolved(base, tgt, existing)
        if head in local and local[head] in existing:
            cls_qn = local[head]
            tgt = qname.join(cls_qn, rest)
            return _resolved(base, tgt, existing)
        base.resolution = "dynamic"
        return base
    return base  # other -> unresolved
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: all PASS, including pre-existing `test_resolve_simple_and_attribute`, `test_resolve_dynamic_for_obj_method`, `test_resolve_cls_method`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_resolver.py code_review_ai/resolver.py
git commit -m "feat(resolver): resolve package __init__ re-export chains"
```

---

### Task 4: Add constructor→`__init__` edges

**Files:**
- Modify: `code_review_ai/resolver.py:47-55` (`resolve_calls`)
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: Task 3 — `resolve_calls` already builds `all_import_maps` and calls `_resolve_one`.
- Produces: `resolve_calls` additionally emits a `call` edge `source → Class.__init__` whenever a resolved edge targets a class whose `__init__` exists in the global qname set.

- [ ] **Step 1: Write the failing test** (append to `tests/test_resolver.py`)

```python
def test_constructor_links_to_init(tmp_path):
    mod = tmp_path / "svc.py"
    mod.write_text(
        "class Service:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "s = Service()\n",
        encoding="utf-8",
    )
    pf = parse_file(str(mod), str(tmp_path))
    qnames = {n.qualified_name for n in pf.nodes}
    edges = resolve_calls([pf], qnames)
    by = {(e.source, e.target, e.kind, e.resolution) for e in edges}
    assert ("svc", "svc::Service", "call", "resolved") in by          # to the class
    assert ("svc", "svc::Service.__init__", "call", "resolved") in by  # to __init__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolver.py::test_constructor_links_to_init -v`
Expected: FAIL — `("svc", "svc::Service.__init__", "call", "resolved")` not present.

- [ ] **Step 3: Write minimal implementation**

In `resolve_calls`, build the class set and, for each resolved edge targeting a class with an existing `__init__`, append the `__init__` edge:

```python
def resolve_calls(parsed_files: list[ParsedFile], existing_qnames: set[str]) -> list[Edge]:
    mod_syms = _module_symbols(parsed_files)
    all_import_maps = {pf.module_qname: _import_map(pf) for pf in parsed_files}
    class_qnames = {n.qualified_name for f in parsed_files for n in f.nodes if n.kind == "class"}
    edges: list[Edge] = []
    for pf in parsed_files:
        local = mod_syms.get(pf.module_qname, {})
        imports = _import_map(pf)
        for c in pf.raw_calls:
            edge = _resolve_one(c, local, imports, existing_qnames, all_import_maps)
            edges.append(edge)
            if edge.resolution == "resolved" and edge.target in class_qnames:
                init_qn = qname.join(edge.target, "__init__")
                if init_qn in existing_qnames:
                    edges.append(Edge(source=edge.source, target=init_qn, kind="call",
                                      file_path=edge.file_path, call_line=edge.call_line,
                                      resolution="resolved"))
    return edges
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_resolver.py code_review_ai/resolver.py
git commit -m "feat(resolver): link constructor call sites to __init__"
```

---

### Task 5: End-to-end src-layout integration test

**Files:**
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: Tasks 1-4 — src-strip + relative imports + re-export chain + constructor edge.
- Produces: a regression test proving the full pipeline: a `src/`-layout package that re-exports a function, consumed by a test module, yields a resolved caller edge from the test function to the real symbol.

- [ ] **Step 1: Write the failing test** (append to `tests/test_resolver.py`)

```python
def test_src_layout_test_reaches_changed_symbol(tmp_path):
    pkg = tmp_path / "src" / "app"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .service import login\n", encoding="utf-8")
    (pkg / "service.py").write_text("def login(user, pw):\n    return True\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_app.py").write_text(
        "from app import login\n"
        "def test_login():\n"
        "    assert login('u', 'p')\n",
        encoding="utf-8",
    )
    files = [parse_file(str(pkg / "__init__.py"), str(tmp_path)),
             parse_file(str(pkg / "service.py"), str(tmp_path)),
             parse_file(str(tests / "test_app.py"), str(tmp_path))]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    edges = resolve_calls(files, qnames)
    by = {(e.source, e.target, e.resolution) for e in edges}
    # test function is a resolved caller of the real symbol behind `from app import login`
    assert ("tests.test_app::test_login", "app.service::login", "resolved") in by
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolver.py::test_src_layout_test_reaches_changed_symbol -v`
Expected: FAIL before all four fixes are in — no `("tests.test_app::test_login", "app.service::login", "resolved")` edge.

- [ ] **Step 3: Run test to verify it passes**

Run: `uv run pytest tests/test_resolver.py::test_src_layout_test_reaches_changed_symbol -v`
Expected: PASS (all fixes are already committed in Tasks 1-4).

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest`
Expected: all PASS (no regression across `test_parser`, `test_resolver`, `test_indexer`, `test_flow_builder`, `test_benchmark`, `test_incremental`, etc.).

- [ ] **Step 5: Commit**

```bash
git add tests/test_resolver.py
git commit -m "test(resolver): end-to-end src-layout test reaches changed symbol"
```

---

### Task 6: Rerun the 30-case benchmark and check acceptance

**Files:**
- Modify: `benchmark-results/swe-bench-verified-30.json` (regenerated by the suite)

**Interfaces:**
- Consumes: Tasks 1-5. Repos are cached in `.benchmark-cache/repos/`; each case does a full rebuild on its base commit, so the new parser/resolver code is exercised.
- Produces: updated `benchmark-results/swe-bench-verified-30.json` + an acceptance verdict.

- [ ] **Step 1: Rerun the benchmark suite**

Run: `uv run python scripts/run_swebench_suite.py --cases benchmarks/swe-bench-verified-30.json --out benchmark-results/swe-bench-verified-30.json`
Expected: completes all 30 cases (~2-4 min), overwrites `benchmark-results/swe-bench-verified-30.json`.

- [ ] **Step 2: Check acceptance**

Run:
```
uv run python -c "import json; a=json.load(open('benchmark-results/swe-bench-verified-30.json'))['aggregate']; print('recall@10:', a['macro_test_file_recall_at_k']); print('precision@10:', a['macro_test_file_precision_at_k']); print('symbol_found_rate:', a['symbol_found_rate']); print('cases:', a['cases'])"
```
Expected:
- `macro_test_file_recall_at_k >= 0.5` (baseline was 0.2 → 6/30).
- `macro_test_file_precision_at_k` not sharply below 0.0789.
- If recall < 0.5: identify which fix underperformed by re-running the suite at intermediate commits (e.g. `git stash` the latest commits one at a time and re-run) before committing the results.

- [ ] **Step 3: Spot-check one newly-hit case**

Pick any case whose `candidate_files` now contains its `gold_files` (grep `"patch_file_recall_at_k": 1.0` in the results) and confirm the test file got there via a resolved edge, not by accident:
```
uv run python -c "
import json, sqlite3
d = json.load(open('benchmark-results/swe-bench-verified-30.json'))
hit = next(c for c in d['cases'] if c['patch_file_recall_at_k'] == 1.0)
print(hit['id'], '->', hit['gold_files'])
conn = sqlite3.connect(f'.benchmark-cache/indexes/{hit[\"id\"]}.db')
conn.row_factory = sqlite3.Row
for s in hit['changed_symbols']:
    callers = conn.execute('SELECT DISTINCT n.file_path FROM edges e JOIN nodes n ON n.qualified_name=e.source WHERE e.target=? AND e.resolution=?', (s, 'resolved')).fetchall()
    print(' ', s, '<-', sorted({r['file_path'].split('repos' + chr(92))[-1] for r in callers})[:3])
"
```

- [ ] **Step 4: Commit the results with the before/after summary**

```bash
git add benchmark-results/swe-bench-verified-30.json
git commit -m "bench: SWE-bench recall 0.20 -> <NEW> after resolver test-connectivity fixes

macro_test_file_recall_at_k: 0.20 -> <NEW>
macro_test_file_precision_at_k: <OLD> -> <NEW>
"
```

---

### Task 7: Put direct callers/callees first in `get_impact` output

**Files:**
- Modify: `code_review_ai/impact.py:46-79` (`get_impact`)
- Test: `tests/test_impact.py`

**Interfaces:**
- Consumes: Tasks 1-6. Fixes the benchmark regression found in Task 6: a symbol on many flows aggregates thousands of upstream nodes, burying its DIRECT resolved callers below top-10 (confirmed for `xarray.core.variable::as_compatible_data` — 1617 flows, only 3 direct-caller files incl. the gold test).
- Produces: `get_impact` returns `upstream`/`downstream` with **direct resolved callers/callees first**, then flow-derived transitive nodes. Signature unchanged. `_dedup` keeps the direct occurrence so ordering survives.
- Result for the benchmark: the changed symbol's own file (first in `_candidate_files`), then its direct-caller files (incl. tests), then transitive — gold tests surface within top-10.

- [ ] **Step 1: Write the failing test** (append to `tests/test_impact.py`; builds a tmp git repo so `list_source_files` works)

```python
import subprocess

from code_review_ai.indexer import rebuild


def _tmp_idx(tmp_path):
    (tmp_path / "a.py").write_text("def entry():\n    helper()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def helper():\n    target()\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (tmp_path / "d.py").write_text("def direct():\n    target()\n", encoding="utf-8")
    for cmd in (["git", "init"], ["git", "add", "-A"],
                ["git", "commit", "-m", "fixture"]):
        subprocess.run(cmd, cwd=tmp_path, check=True, capture_output=True)
    cfg = load_config(str(tmp_path))
    cfg.db_path = str(tmp_path / "i.db")
    cfg.repo_path = str(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def test_impact_puts_direct_callers_first(tmp_path):
    conn = _tmp_idx(tmp_path)
    # target has direct callers d::direct and b::helper, plus a purely-transitive
    # caller a::entry -> b::helper -> target. The direct callers must rank before
    # the transitive-only one. (Both direct callers are asserted because
    # _edges_fallback's DISTINCT query has no ORDER BY between them.)
    res = get_impact(conn, ["c::target"])[0]
    assert res["found"] and res["upstream"]
    qnames = [n["qname"] for n in res["upstream"]]
    assert qnames.index("d::direct") < qnames.index("a::entry")
    assert qnames.index("b::helper") < qnames.index("a::entry")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_impact.py::test_impact_puts_direct_callers_first -v`
Expected: FAIL — `res["upstream"][0]["qname"]` is not `d::direct` (aggregation order is flow-query order, direct caller buried).

- [ ] **Step 3: Write minimal implementation**

In `get_impact` (`code_review_ai/impact.py`), when the symbol is on flows, prepend the direct resolved callers/callees (from the existing `_edges_fallback`) before the flow-derived lists:

```python
        if flows:
            direct_up, direct_down = _edges_fallback(conn, qname, max_nodes_per_direction)
            for f in flows:
                up, down = _slice_flow(conn, f["flow_id"], nid, max_nodes_per_direction)
                up_all.extend(up)
                down_all.extend(down)
                entry = conn.execute(
                    "SELECT n.qualified_name FROM flows f"
                    " JOIN nodes n ON f.entry_point_id=n.id"
                    " WHERE f.id=?", (f["flow_id"],)).fetchone()
                if entry:
                    entries.add(entry["qualified_name"])
            up_all = direct_up + up_all
            down_all = direct_down + down_all
        else:
            up_all, down_all = _edges_fallback(conn, qname, max_nodes_per_direction)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_impact.py -v`
Expected: all PASS, including pre-existing `test_impact_slices_prefix_suffix` (membership-based, order-independent) and `test_impact_off_flow_fallback_to_edges`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_impact.py code_review_ai/impact.py
git commit -m "feat(impact): rank direct callers/callees before transitive in impact output"
```

---

### Task 8: Re-run the 30-case benchmark and re-check acceptance

**Files:**
- Modify: `benchmark-results/swe-bench-verified-30.json` (regenerated; keep it tracked per decision)

**Interfaces:**
- Consumes: Task 7. Repos still cached; suite still accepts `--dataset-name`.
- Produces: updated results file + verdict. Expected: recall rises above 0.267, the xarray-2905/6938 regressions recover (their gold tests are direct resolved callers).

- [ ] **Step 1: Rerun the suite**

Run: `uv run python scripts/run_swebench_suite.py --cases benchmarks/swe-bench-verified-30.json --dataset-name "SWE-bench Verified" --out benchmark-results/swe-bench-verified-30.json`
Expected: completes (~2-4 min), overwrites the results file.

- [ ] **Step 2: Check the aggregates and which cases hit**

Run:
```
uv run python -c "import json; d=json.load(open('benchmark-results/swe-bench-verified-30.json')); a=d['aggregate']; print('recall@10:', a['macro_test_file_recall_at_k']); print('precision@10:', a['macro_test_file_precision_at_k']); print('hits:', [c['id'] for c in d['cases'] if c['patch_file_recall_at_k']==1.0])"
```
Verdict: regression recovered (xarray-2905 and xarray-6938 back to recall 1.0) and recall > 0.267 is the success bar for this task. If not met, report DONE_WITH_CONCERNS with the numbers — do not diagnose alone; the controller adjudicates.

- [ ] **Step 3: Commit the updated results**

```bash
git add benchmark-results/swe-bench-verified-30.json
git commit -m "bench: SWE-bench recall after direct-caller-first ranking

macro_test_file_recall_at_k: <old> -> <new>
macro_test_file_precision_at_k: <old> -> <new>
"
```

---

## Self-Review

**Spec coverage:** All four spec fixes map to tasks: src-strip → Task 1, relative imports → Task 2, re-export chain → Task 3, constructor `__init__` edge → Task 4. Integration/regression → Task 5. Benchmark rerun + acceptance (recall ≥ 0.5, precision watch) → Task 6. Explicitly-scoped-out dynamic instance-call tracking is left untouched. Benchmark harness/manifest untouched per spec.

**Placeholder scan:** No TBD/TODO; every code step has complete code; commands and expected outputs are concrete.

**Type consistency:** `_extract_imports`/`_extract_imports_python` gain `file_path` (Task 2) and the only call site (`parse_file`) is updated in the same task. `_resolve_one` gains `all_import_maps` (Task 3), and Task 4's `resolve_calls` still passes it. `resolve_calls`/`resolve_edges` public signatures are unchanged across all tasks — indexer/update callers unaffected.
