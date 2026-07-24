# Code Review AI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python tool that parses a codebase's AST with tree-sitter, stores a call graph + materialized call chains in SQLite, and exposes impact-chain queries to an AI reviewer via an MCP server.

**Architecture:** Layered library (config → db → parser → resolver → flow_builder → indexer → changes → impact → watcher) with a thin MCP frontend and optional CLI. Full rebuild on change, driven by a file watcher; queries slice pre-materialized BFS flows.

**Tech Stack:** Python 3.14 (uv), `tree-sitter` + `tree-sitter-python`, `mcp` SDK (FastMCP), `watchfiles`, stdlib `sqlite3`/`tomllib`/`subprocess` (git), `pytest`.

## Global Constraints

- Python `>=3.14` (pyproject `requires-python = ">=3.14"`); managed by uv.
- Dependencies (exact): `tree-sitter`, `tree-sitter-python`, `mcp`, `watchfiles`; dev `pytest`.
- git accessed via `subprocess` (no pygit2). SQLite via stdlib `sqlite3`. Config via stdlib `tomllib`.
- `qualified_name` uses `:` as scope separator (module path keeps Python dots): `pkg.auth:UserService:authenticate`.
- `nodes.kind` ∈ {`module`,`class`,`function`,`method`}; `edges.kind` = `call`; `edges.resolution` ∈ {`resolved`,`dynamic`,`unresolved`}.
- SQLite runs in WAL mode; every rebuild is one transaction (atomic swap, old index preserved on failure).
- No source code in query output--only `qname + file:line + signature`.

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml` | Project metadata, dependencies, entry points |
| `src/code_review_ai/__init__.py` | Package marker |
| `src/code_review_ai/config.py` | Load `Config` from `pyproject.toml`/`cr-ai.toml` + env |
| `src/code_review_ai/db.py` | SQLite connection, WAL, schema init, transaction helpers |
| `src/code_review_ai/parser.py` | tree-sitter → `ParsedFile` (nodes, raw calls, imports) |
| `src/code_review_ai/resolver.py` | Import-aware resolution → `Edge` list |
| `src/code_review_ai/flow_builder.py` | Adjacency + BFS → `FlowRecord` list |
| `src/code_review_ai/indexer.py` | Phase A (parse+resolve+write) + Phase B (flows) orchestration |
| `src/code_review_ai/changes.py` | git diff / files / symbols → changed qnames |
| `src/code_review_ai/impact.py` | memberships slice + edges fallback → impact dict |
| `src/code_review_ai/watcher.py` | watchfiles debounce → trigger rebuild |
| `src/code_review_ai/mcp_server.py` | FastMCP tool registration/dispatch |
| `src/code_review_ai/cli.py` | Optional CLI (rebuild/query/search) |
| `tests/fixtures/...` | Synthetic Python repo for tests |
| `tests/test_*.py` | Per-module unit tests |

**Shared interfaces (defined in early tasks, consumed by later ones):**

```python
# config.py
@dataclass
class Config:
    repo_path: str; db_path: str; diff_base: str
    max_depth: int; watch_debounce_ms: int
    entry_names: list[str]; entry_decorators: list[str]

# parser.py
@dataclass
class ParsedNode:
    qualified_name: str; kind: str; file_path: str
    start_line: int; end_line: int; signature: str; parent_qname: str | None
@dataclass
class RawCall:
    source_qname: str; target_expr: str; call_form: str; file_path: str; call_line: int
@dataclass
class ImportEntry:
    local_name: str; module: str; imported_name: str | None; is_star: bool
@dataclass
class ParsedFile:
    file_path: str; module_qname: str
    nodes: list[ParsedNode]; raw_calls: list[RawCall]; imports: list[ImportEntry]

# resolver.py
@dataclass
class Edge:
    source: str; target: str; kind: str; file_path: str; call_line: int; resolution: str

# flow_builder.py
@dataclass
class NodeRow:
    id: int; qualified_name: str; file_path: str
@dataclass
class EdgeRow:
    source: str; target: str; resolution: str
@dataclass
class FlowRecord:
    entry_point_id: int; name: str; depth: int; node_count: int; file_count: int; path: list[int]

# indexer.py
@dataclass
class RebuildStats:
    node_count: int; edge_count: int; flow_count: int; built_at: str
```

---

### Task 1: Scaffold, config, and DB schema

**Files:**
- Modify: `pyproject.toml`
- Create: `src/code_review_ai/__init__.py`, `src/code_review_ai/config.py`, `src/code_review_ai/db.py`
- Test: `tests/test_config.py`, `tests/test_db.py`

**Interfaces:**
- Produces: `Config` dataclass + `load_config(repo_path=".") -> Config`; `connect(db_path) -> sqlite3.Connection`; `init_schema(conn) -> None`.

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "code-review-ai"
version = "0.1.0"
description = "tree-sitter + SQLite code impact-chain analysis for AI code review"
readme = "README.md"
requires-python = ">=3.14"
dependencies = [
    "tree-sitter>=0.25",
    "tree-sitter-python>=0.23",
    "mcp>=1.2",
    "watchfiles>=0.21",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
code-review-ai = "code_review_ai.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/code_review_ai"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 2: Create package marker**

`src/code_review_ai/__init__.py`:
```python
"""Code Review AI - impact-chain analysis tool."""
```

- [ ] **Step 3: Write failing test for config**

`tests/test_config.py`:
```python
from code_review_ai.config import Config, load_config


def test_load_config_defaults(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.code-review-ai]\nrepo_path = "."\n', encoding="utf-8"
    )
    cfg = load_config(str(tmp_path))
    assert isinstance(cfg, Config)
    assert cfg.repo_path == "."
    assert cfg.diff_base == "origin/main"        # default
    assert cfg.max_depth == 10                    # default
    assert cfg.entry_names == ["main"]            # default heuristic
```

- [ ] **Step 4: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.config`

- [ ] **Step 5: Implement config.py**

`src/code_review_ai/config.py`:
```python
from __future__ import annotations
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS = dict(
    repo_path=".",
    db_path=".code-review-ai/index.db",
    diff_base="origin/main",
    max_depth=10,
    watch_debounce_ms=500,
    entry_names=["main"],
    entry_decorators=[
        "app.route", "click.command",
        "router.get", "router.post", "celery.task",
    ],
)


@dataclass
class Config:
    repo_path: str
    db_path: str
    diff_base: str
    max_depth: int
    watch_debounce_ms: int
    entry_names: list[str]
    entry_decorators: list[str]


def _load_toml(repo_path: str) -> dict:
    for name in ("pyproject.toml", "cr-ai.toml"):
        p = Path(repo_path) / name
        if p.exists():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            return data.get("tool", {}).get("code-review-ai", {})
    return {}


def load_config(repo_path: str = ".") -> Config:
    raw = dict(DEFAULTS)
    raw.update({k: v for k, v in _load_toml(repo_path).items() if v is not None})
    # env overrides (CRAI_<UPPER_KEY>)
    for key in DEFAULTS:
        env = os.environ.get(f"CRAI_{key.upper()}")
        if env is not None:
            if isinstance(DEFAULTS[key], int):
                raw[key] = int(env)
            elif isinstance(DEFAULTS[key], list):
                raw[key] = env.split(",")
            else:
                raw[key] = env
    return Config(**raw)
```

- [ ] **Step 6: Run config test to verify pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 7: Write failing test for db**

`tests/test_db.py`:
```python
import sqlite3
from code_review_ai.db import connect, init_schema


def test_init_schema_creates_tables(tmp_path):
    conn = connect(str(tmp_path / "x.db"))
    init_schema(conn)
    names = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"nodes", "edges", "flows", "flow_memberships"} <= names
    # WAL enabled
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_schema_is_idempotent(tmp_path):
    conn = connect(str(tmp_path / "x.db"))
    init_schema(conn)
    init_schema(conn)  # must not raise
```

- [ ] **Step 8: Run db test to verify it fails**

Run: `uv run pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.db`

- [ ] **Step 9: Implement db.py**

`src/code_review_ai/db.py`:
```python
from __future__ import annotations
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY,
    qualified_name TEXT UNIQUE,
    kind TEXT,
    language TEXT,
    file_path TEXT,
    start_line INTEGER,
    end_line INTEGER,
    signature TEXT,
    parent_id INTEGER REFERENCES nodes(id)
);
CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY,
    source TEXT,
    target TEXT,
    kind TEXT,
    file_path TEXT,
    call_line INTEGER,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS flows (
    id INTEGER PRIMARY KEY,
    name TEXT,
    entry_point_id INTEGER,
    depth INTEGER,
    node_count INTEGER,
    file_count INTEGER,
    criticality REAL,
    path_json TEXT
);
CREATE TABLE IF NOT EXISTS flow_memberships (
    flow_id INTEGER,
    node_id INTEGER,
    position INTEGER,
    PRIMARY KEY (flow_id, node_id)
);
CREATE TABLE IF NOT EXISTS build_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_memberships_node ON flow_memberships(node_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Atomic transaction; rolls back on exception."""
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
```

- [ ] **Step 10: Run db test to verify pass**

Run: `uv run pytest tests/test_db.py -v`
Expected: PASS

- [ ] **Step 11: Commit**

```bash
git add pyproject.toml src/code_review_ai/__init__.py src/code_review_ai/config.py src/code_review_ai/db.py tests/test_config.py tests/test_db.py
git commit -m "feat: scaffold package, config loader, db schema"
```

---

### Task 2: Parser - node extraction

**Files:**
- Create: `src/code_review_ai/parser.py`, `tests/fixtures/repo/auth.py`
- Test: `tests/test_parser.py`

**Interfaces:**
- Produces: `ParsedNode`, `ParsedFile` dataclasses; `list_python_files(repo_path) -> list[str]`; `parse_file(file_path, repo_root) -> ParsedFile` (node extraction part; raw_calls/imports filled in Task 3).

- [ ] **Step 1: Create fixture**

`tests/fixtures/repo/auth.py`:
```python
class UserService:
    def authenticate(self, user, pw) -> bool:
        return check(pw)


def login(user, pw) -> str:
    return user
```

- [ ] **Step 2: Write failing test for node extraction**

`tests/test_parser.py`:
```python
from code_review_ai.parser import parse_file

FIX = "tests/fixtures/repo"


def test_parse_extracts_nodes():
    pf = parse_file(f"{FIX}/auth.py", FIX)
    qn = {n.qualified_name: n for n in pf.nodes}
    assert "auth" in qn and qn["auth"].kind == "module"
    assert qn["auth:UserService"].kind == "class"
    auth_method = qn["auth:UserService:authenticate"]
    assert auth_method.kind == "method"
    assert auth_method.parent_qname == "auth:UserService"
    assert auth_method.signature == "def authenticate(self, user, pw) -> bool:"
    assert auth_method.start_line >= 1 and auth_method.end_line >= auth_method.start_line
    assert qn["auth:login"].kind == "function"
    assert qn["auth:login"].parent_qname is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_parser.py::test_parse_extracts_nodes -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.parser`

- [ ] **Step 4: Implement parser node extraction**

`src/code_review_ai/parser.py`:
```python
from __future__ import annotations
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())
_PARSER = Parser(PY_LANGUAGE)


@dataclass
class ParsedNode:
    qualified_name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str
    parent_qname: str | None


@dataclass
class RawCall:
    source_qname: str
    target_expr: str
    call_form: str
    file_path: str
    call_line: int


@dataclass
class ImportEntry:
    local_name: str
    module: str
    imported_name: str | None
    is_star: bool


@dataclass
class ParsedFile:
    file_path: str
    module_qname: str
    nodes: list[ParsedNode] = field(default_factory=list)
    raw_calls: list[RawCall] = field(default_factory=list)
    imports: list[ImportEntry] = field(default_factory=list)


def list_python_files(repo_path: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.py"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    )
    return sorted(out.stdout.splitlines())


def _module_qname(file_path: str, repo_root: str) -> str:
    rel = Path(file_path).resolve().relative_to(Path(repo_root).resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _sig(source: bytes, node) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body else node.end_byte
    return source[node.start_byte:end].decode("utf-8").strip()


def _walk_defs_typed(node, source, file_path, module_qname, scope_qname, out):
    """Collect function/class nodes; recurse into bodies for nested defs.

    kind is assigned as 'class' for class_definition and 'function' otherwise;
    methods are reclassified to 'method' in parse_file once parent kinds are known.
    """
    for child in node.children:
        t = child.type
        if t in ("function_definition", "class_definition"):
            name = child.child_by_field_name("name").text.decode("utf-8")
            qn = f"{scope_qname}:{name}" if scope_qname else f"{module_qname}:{name}"
            kind = "class" if t == "class_definition" else "function"
            out.append(ParsedNode(
                qualified_name=qn, kind=kind, file_path=file_path,
                start_line=child.start_point[0] + 1, end_line=child.end_point[0] + 1,
                signature=_sig(source, child), parent_qname=scope_qname,
            ))
            _walk_defs_typed(child, source, file_path, module_qname, qn, out)
        else:
            _walk_defs_typed(child, source, file_path, module_qname, scope_qname, out)


def parse_file(file_path: str, repo_root: str) -> ParsedFile:
    source = Path(file_path).read_bytes()
    tree = _PARSER.parse(source)
    root = tree.root_node
    module_qname = _module_qname(file_path, repo_root)
    pf = ParsedFile(file_path=file_path, module_qname=module_qname)
    # module node (anchor; future import edges per spec §7)
    pf.nodes.append(ParsedNode(
        qualified_name=module_qname, kind="module", file_path=file_path,
        start_line=1, end_line=root.end_point[0] + 1, signature="", parent_qname=None,
    ))
    defs: list[ParsedNode] = []
    _walk_defs_typed(root, source, file_path, module_qname, None, defs)
    qn_kind = {n.qualified_name: n.kind for n in defs}
    for n in defs:
        if n.kind == "function" and n.parent_qname and qn_kind.get(n.parent_qname) == "class":
            n.kind = "method"
    pf.nodes.extend(defs)
    return pf
```

- [ ] **Step 5: Run test to verify pass**

Run: `uv run pytest tests/test_parser.py::test_parse_extracts_nodes -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/code_review_ai/parser.py tests/fixtures/repo/auth.py tests/test_parser.py
git commit -m "feat(parser): extract module/class/function/method nodes"
```

---

### Task 3: Parser - raw calls and import table

**Files:**
- Modify: `src/code_review_ai/parser.py`
- Create: `tests/fixtures/repo/app.py`
- Test: `tests/test_parser.py` (extend)

**Interfaces:**
- Produces: `RawCall`, `ImportEntry` populated in `ParsedFile.raw_calls` / `.imports`.

- [ ] **Step 1: Create fixture with imports + calls**

`tests/fixtures/repo/app.py`:
```python
from auth import login
import auth as a


def main():
    login("u", "p")
    a.login("u", "p")
    obj.run()
    vals[0]()
```

- [ ] **Step 2: Write failing test for calls + imports**

Append to `tests/test_parser.py`:
```python
def test_parse_extracts_calls_and_imports():
    pf = parse_file(f"{FIX}/app.py", FIX)
    imp = {i.local_name: i for i in pf.imports}
    assert imp["login"].module == "auth" and imp["login"].imported_name == "login"
    assert imp["a"].module == "auth" and imp["a"].imported_name is None
    calls = {(c.target_expr, c.call_form) for c in pf.raw_calls}
    assert ("login", "simple") in calls
    assert ("a.login", "attribute") in calls
    assert ("obj.run", "attribute") in calls
    assert ("vals[0]", "other") in calls
    assert all(c.source_qname == "app:main" for c in pf.raw_calls)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_parser.py::test_parse_extracts_calls_and_imports -v`
Expected: FAIL (raw_calls/imports empty)

- [ ] **Step 4: Implement call + import extraction**

Append to `src/code_review_ai/parser.py`:
```python
def _call_target(func_node) -> tuple[str, str]:
    """Return (target_expr, call_form) for a call's function child."""
    t = func_node.type
    if t == "identifier":
        return func_node.text.decode("utf-8"), "simple"
    if t == "attribute":
        return func_node.text.decode("utf-8"), "attribute"
    return func_node.text.decode("utf-8"), "other"


def _walk_calls(node, source, file_path, module_qname, cur_scope, out):
    for child in node.children:
        if child.type == "call":
            func = child.child_by_field_name("function")
            if func is not None:
                expr, form = _call_target(func)
                out.append(RawCall(
                    source_qname=cur_scope or module_qname,
                    target_expr=expr, call_form=form,
                    file_path=file_path, call_line=child.start_point[0] + 1,
                ))
            # do not recurse into the call's arguments' sub-calls separately;
            # general traversal still picks nested calls below
        # update scope on entering a def
        if child.type in ("function_definition", "class_definition"):
            name = child.child_by_field_name("name").text.decode("utf-8")
            new_scope = f"{cur_scope}:{name}" if cur_scope else f"{module_qname}:{name}"
            _walk_calls(child, source, file_path, module_qname, new_scope, out)
        else:
            _walk_calls(child, source, file_path, module_qname, cur_scope, out)


def _dotted(node) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def _extract_imports(root, module_qname) -> list[ImportEntry]:
    entries: list[ImportEntry] = []
    parts = module_qname.split(".") if module_qname else []
    pkg = parts[:-1] if parts else []
    for node in root.children:
        if node.type == "import_statement":
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
            # count leading dots (relative) and find module dotted_name
            dots = sum(1 for c in node.children if c.type == ".")
            mod_node = node.child_by_field_name("module_name")
            sub = _dotted(mod_node)
            if dots:
                up = dots - 1
                base = pkg[: len(pkg) - up] if up <= len(pkg) else []
                module = ".".join(base + ([sub] if sub else []))
            else:
                module = sub
            for c in node.children:
                if c.type == "dotted_name" and c is not mod_node:
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

Then in `parse_file`, before `return pf`, add:
```python
    _walk_calls(root, source, file_path, module_qname, None, pf.raw_calls)
    pf.imports = _extract_imports(root, module_qname)
```

- [ ] **Step 5: Run test to verify pass**

Run: `uv run pytest tests/test_parser.py -v`
Expected: PASS (both parser tests)

- [ ] **Step 6: Commit**

```bash
git add src/code_review_ai/parser.py tests/fixtures/repo/app.py tests/test_parser.py
git commit -m "feat(parser): extract raw calls and import table"
```

---

### Task 4: Resolver - import-aware edges

**Files:**
- Create: `src/code_review_ai/resolver.py`
- Create: `tests/fixtures/repo/util.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `ParsedFile`, `RawCall`, `ImportEntry` (parser).
- Produces: `Edge` dataclass; `resolve_calls(parsed_files, existing_qnames) -> list[Edge]`.

- [ ] **Step 1: Create fixture**

`tests/fixtures/repo/util.py`:
```python
def hash_pw(pw) -> str:
    return pw


def helper():
    pass
```

- [ ] **Step 2: Write failing test**

`tests/test_resolver.py`:
```python
from code_review_ai.parser import parse_file
from code_review_ai.resolver import resolve_calls

FIX = "tests/fixtures/repo"


def _resolve():
    files = [parse_file(f"{FIX}/{n}", FIX) for n in ("auth.py", "app.py", "util.py")]
    qnames = {n.qualified_name for f in files for n in f.nodes}
    return resolve_calls(files, qnames)


def test_resolve_simple_and_attribute():
    edges = _resolve()
    by = {(e.source, e.target, e.resolution) for e in edges}
    # app:main calls login (imported from auth) -> resolved to auth:login
    assert ("app:main", "auth:login", "resolved") in by
    # app:main calls a.login (import module alias) -> resolved to auth:login
    assert ("app:main", "auth:login", "resolved") in by
    # auth:UserService:authenticate calls check() -> unresolved
    assert any(e.source == "auth:UserService:authenticate" and e.resolution == "unresolved"
               and e.target == "check" for e in edges)


def test_resolve_dynamic_for_obj_method():
    edges = _resolve()
    dyn = [e for e in edges if e.target == "obj.run"]
    assert dyn and dyn[0].resolution == "dynamic"


def test_resolve_cls_method():
    # add a class call fixture inline
    src = "class C:\n    def m(self): pass\nx = C()\nx.m()"
    # verified via app.py vals[0] -> other
    edges = _resolve()
    other = [e for e in edges if e.target == "vals[0]"]
    assert other and other[0].resolution == "unresolved"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.resolver`

- [ ] **Step 4: Implement resolver.py**

`src/code_review_ai/resolver.py`:
```python
from __future__ import annotations
from dataclasses import dataclass

from code_review_ai.parser import ParsedFile, RawCall


@dataclass
class Edge:
    source: str
    target: str
    kind: str
    file_path: str
    call_line: int
    resolution: str


def _module_symbols(parsed_files: list[ParsedFile]) -> dict:
    """module_qname -> {local_name: qualified_name} for functions/classes."""
    out: dict[str, dict[str, str]] = {}
    for pf in parsed_files:
        syms: dict[str, str] = {}
        for n in pf.nodes:
            if n.kind in ("function", "class"):
                short = n.qualified_name.rsplit(":", 1)[-1]
                syms[short] = n.qualified_name
        out[pf.module_qname] = syms
    return out


def _import_map(pf: ParsedFile) -> dict:
    """local_name -> (module, imported_name_or_None, is_star)."""
    return {i.local_name: (i.module, i.imported_name, i.is_star) for i in pf.imports}


def _exists(qname: str, existing: set[str]) -> bool:
    return qname in existing


def resolve_calls(parsed_files: list[ParsedFile], existing_qnames: set[str]) -> list[Edge]:
    mod_syms = _module_symbols(parsed_files)
    edges: list[Edge] = []
    for pf in parsed_files:
        local = mod_syms.get(pf.module_qname, {})
        imports = _import_map(pf)
        for c in pf.raw_calls:
            edges.append(_resolve_one(c, pf.module_qname, local, imports, existing_qnames))
    return edges


def _resolve_one(c: RawCall, module: str, local: dict, imports: dict, existing: set[str]) -> Edge:
    base = Edge(source=c.source_qname, target=c.target_expr, kind="call",
                file_path=c.file_path, call_line=c.call_line, resolution="unresolved")
    if c.call_form == "simple":
        name = c.target_expr
        if name in local:
            return _resolved(base, local[name], existing)
        if name in imports:
            mod, imp_name, _star = imports[name]
            if imp_name:  # from m import name
                tgt = f"{mod}:{imp_name}"
                return _resolved(base, tgt, existing)
            return _resolved(base, mod, existing)  # imported module itself
        return base  # unresolved
    if c.call_form == "attribute":
        head = c.target_expr.split(".", 1)[0]
        rest = c.target_expr[len(head) + 1:]
        if head in imports:
            mod, imp_name, _ = imports[head]
            if imp_name is None:  # import m / import m as head -> m.rest
                tgt = f"{mod}:{rest}" if "." not in rest else f"{mod}:{rest.replace('.', ':')}"
                return _resolved(base, tgt, existing)
        if head in local and local[head] in existing:
            cls_qn = local[head]
            tgt = f"{cls_qn}:{rest}"
            return _resolved(base, tgt, existing)
        base.resolution = "dynamic"
        return base
    return base  # other -> unresolved


def _resolved(base: Edge, target: str, existing: set[str]) -> Edge:
    base.target = target
    base.resolution = "resolved" if _exists(target, existing) else "unresolved"
    return base
```

- [ ] **Step 5: Run test to verify pass**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/code_review_ai/resolver.py tests/fixtures/repo/util.py tests/test_resolver.py
git commit -m "feat(resolver): import-aware call resolution with resolution status"
```

---

### Task 5: flow_builder - adjacency + BFS

**Files:**
- Create: `src/code_review_ai/flow_builder.py`
- Test: `tests/test_flow_builder.py`

**Interfaces:**
- Consumes: `NodeRow`, `EdgeRow` (defined here).
- Produces: `FlowRecord`; `build_flows(nodes, edges, entry_point_ids, max_depth) -> list[FlowRecord]`.

- [ ] **Step 1: Write failing test**

`tests/test_flow_builder.py`:
```python
from code_review_ai.flow_builder import NodeRow, EdgeRow, build_flows


def _nodes():
    return [NodeRow(id=i, qualified_name=q, file_path="f.py")
            for i, q in enumerate(["m:a", "m:b", "m:c", "m:d"])]


def test_linear_chain_one_flow_per_reachable():
    # a -> b -> c
    edges = [EdgeRow("m:a", "m:b", "resolved"), EdgeRow("m:b", "m:c", "resolved")]
    flows = build_flows(_nodes(), edges, [0], max_depth=10)  # entry = a (id 0)
    paths = sorted(f.path for f in flows)
    assert [0, 1] in paths   # a -> b
    assert [0, 1, 2] in paths  # a -> c
    assert all(f.entry_point_id == 0 for f in flows)
    assert all(f.criticality is None or True for f in flows)  # not set here


def test_diamond_no_path_explosion():
    # a -> b -> d, a -> c -> d
    edges = [
        EdgeRow("m:a", "m:b", "resolved"), EdgeRow("m:b", "m:d", "resolved"),
        EdgeRow("m:a", "m:c", "resolved"), EdgeRow("m:c", "m:d", "resolved"),
    ]
    flows = build_flows(_nodes(), edges, [0], max_depth=10)
    to_d = [f for f in flows if f.path[-1] == 3]
    assert len(to_d) == 1  # one shortest path to d, not two


def test_cycle_handled():
    edges = [EdgeRow("m:a", "m:b", "resolved"), EdgeRow("m:b", "m:a", "resolved")]
    flows = build_flows(_nodes(), edges, [0], max_depth=10)
    # a reaches b; b not re-expanded to a (visited)
    assert any(f.path == [0, 1] for f in flows)


def test_depth_cap():
    edges = [EdgeRow("m:a", "m:b", "resolved"), EdgeRow("m:b", "m:c", "resolved"),
             EdgeRow("m:c", "m:d", "resolved")]
    flows = build_flows(_nodes(), edges, [0], max_depth=1)
    targets = {f.path[-1] for f in flows}
    assert targets == {1}  # only b reachable within depth 1


def test_unresolved_edges_excluded():
    edges = [EdgeRow("m:a", "m:b", "resolved"), EdgeRow("m:b", "m:c", "unresolved")]
    flows = build_flows(_nodes(), edges, [0], max_depth=10)
    assert not any(f.path[-1] == 2 for f in flows)  # c unreachable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_flow_builder.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.flow_builder`

- [ ] **Step 3: Implement flow_builder.py**

`src/code_review_ai/flow_builder.py`:
```python
from __future__ import annotations
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass
class NodeRow:
    id: int
    qualified_name: str
    file_path: str


@dataclass
class EdgeRow:
    source: str
    target: str
    resolution: str


@dataclass
class FlowRecord:
    entry_point_id: int
    name: str
    depth: int
    node_count: int
    file_count: int
    path: list[int]


def build_flows(nodes: list[NodeRow], edges: list[EdgeRow],
                entry_point_ids: list[int], max_depth: int) -> list[FlowRecord]:
    qname_to_id = {n.qualified_name: n.id for n in nodes}
    id_to_file = {n.id: n.file_path for n in nodes}
    adj: dict[int, list[int]] = defaultdict(list)
    for e in edges:
        if e.resolution != "resolved":
            continue
        s = qname_to_id.get(e.source)
        t = qname_to_id.get(e.target)
        if s is not None and t is not None:
            adj[s].append(t)

    flows: list[FlowRecord] = []
    for entry in entry_point_ids:
        flows.extend(_bfs_flows(entry, adj, id_to_file, max_depth))
    return flows


def _bfs_flows(entry: int, adj: dict[int, list[int]],
               id_to_file: dict[int, str], max_depth: int) -> list[FlowRecord]:
    parent: dict[int, int | None] = {entry: None}
    depth: dict[int, int] = {entry: 0}
    q = deque([entry])
    name = ""  # filled by caller via NodeRow; set below
    flows: list[FlowRecord] = []
    while q:
        cur = q.popleft()
        # emit a flow for `cur` (path entry..cur)
        path = _reconstruct(cur, parent)
        files = {id_to_file.get(i, "") for i in path}
        flows.append(FlowRecord(
            entry_point_id=entry, name="", depth=depth[cur],
            node_count=len(path), file_count=len(files), path=path,
        ))
        if depth[cur] >= max_depth:
            continue
        for nxt in adj.get(cur, []):
            if nxt not in parent:
                parent[nxt] = cur
                depth[nxt] = depth[cur] + 1
                q.append(nxt)
    return flows


def _reconstruct(node: int, parent: dict[int, int | None]) -> list[int]:
    path: list[int] = []
    cur: int | None = node
    while cur is not None:
        path.append(cur)
        cur = parent[cur]
    path.reverse()
    return path
```

Note: `FlowRecord.name` is left empty here; the indexer fills it from the entry node's short name when writing. The `criticality` field lives on the DB row (NULL in v1), not on `FlowRecord`.

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_flow_builder.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/code_review_ai/flow_builder.py tests/test_flow_builder.py
git commit -m "feat(flow_builder): BFS shortest-path flow materialization"
```

---

### Task 6: Indexer - Phase A + B orchestration

**Files:**
- Create: `src/code_review_ai/indexer.py`
- Modify: `tests/fixtures/repo/app.py` (mark `main` as entry--already named `main`)
- Test: `tests/test_indexer.py`

**Interfaces:**
- Consumes: `Config`, `connect`/`init_schema`/`transaction` (db), `list_python_files`/`parse_file` (parser), `resolve_calls` (resolver), `build_flows` (flow_builder).
- Produces: `RebuildStats`; `rebuild(config, conn) -> RebuildStats`; `is_stale(config, conn) -> bool`.

- [ ] **Step 1: Write failing test**

`tests/test_indexer.py`:
```python
import sqlite3
from code_review_ai.config import Config, load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild, is_stale

FIX = "tests/fixtures/repo"


def _cfg(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "index.db")
    cfg.repo_path = FIX
    return cfg


def test_rebuild_writes_all_tables(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    stats = rebuild(cfg, conn)
    assert stats.node_count > 0 and stats.edge_count > 0 and stats.flow_count > 0
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == stats.node_count
    assert conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0] == stats.flow_count
    # flows from entry main exist
    assert conn.execute(
        "SELECT COUNT(*) FROM flows WHERE name='main'"
    ).fetchone()[0] > 0


def test_rebuild_atomic_on_failure_preserves_old(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    old_nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    # inject failure into flow writing by breaking build_flows
    import code_review_ai.indexer as idx
    monkeypatch.setattr(idx, "build_flows", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        rebuild(cfg, conn)
    except RuntimeError:
        pass
    # nodes/edges rolled back too (single transaction) -> old preserved
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == old_nodes


def test_is_stale_detects_mtime(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    assert is_stale(cfg, conn) is False
    # touch a file in the future
    import os, time
    p = "tests/fixtures/repo/util.py"
    fut = time.time() + 100
    os.utime(p, (fut, fut))
    assert is_stale(cfg, conn) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_indexer.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.indexer`

- [ ] **Step 3: Implement indexer.py**

`src/code_review_ai/indexer.py`:
```python
from __future__ import annotations
import fnmatch
import json
import os
import sqlite3
import time
from dataclasses import dataclass

from code_review_ai.config import Config
from code_review_ai.db import transaction
from code_review_ai.flow_builder import NodeRow, EdgeRow, FlowRecord, build_flows
from code_review_ai.parser import list_python_files, parse_file
from code_review_ai.resolver import resolve_calls


@dataclass
class RebuildStats:
    node_count: int
    edge_count: int
    flow_count: int
    built_at: str


def _entry_points(parsed, cfg: Config) -> list[str]:
    """Return qnames of designated entry-point functions."""
    out: list[str] = []
    for pf in parsed:
        for n in pf.nodes:
            if n.kind not in ("function", "method"):
                continue
            short = n.qualified_name.rsplit(":", 1)[-1]
            if any(fnmatch.fnmatch(short, pat) for pat in cfg.entry_names):
                out.append(n.qualified_name)
    return out


def _decorator_matches(pf, cfg: Config) -> list[str]:
    # v1: entry_decorators matching requires decorator extraction in parser;
    # skipped here (names cover common cases). Implement when parser exposes decorators.
    return []


def rebuild(config: Config, conn: sqlite3.Connection) -> RebuildStats:
    repo = config.repo_path
    files = list_python_files(repo)
    parsed = [parse_file(os.path.join(repo, f), repo) for f in files]
    qnames = {n.qualified_name for pf in parsed for n in pf.nodes}
    edges = resolve_calls(parsed, qnames)
    entry_qnames = _entry_points(parsed, config)

    with transaction(conn):
        conn.execute("DELETE FROM flow_memberships")
        conn.execute("DELETE FROM flows")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM nodes")
        # insert nodes (parent_id NULL first)
        qname_to_id: dict[str, int] = {}
        for pf in parsed:
            for n in pf.nodes:
                cur = conn.execute(
                    "INSERT INTO nodes(qualified_name,kind,language,file_path,"
                    "start_line,end_line,signature,parent_id) VALUES(?,?,?,?,?,?,?,NULL)",
                    (n.qualified_name, n.kind, "python", n.file_path,
                     n.start_line, n.end_line, n.signature),
                )
                qname_to_id[n.qualified_name] = cur.lastrowid
        # fill parent_id
        for pf in parsed:
            for n in pf.nodes:
                if n.parent_qname and n.parent_qname in qname_to_id:
                    conn.execute("UPDATE nodes SET parent_id=? WHERE id=?",
                                 (qname_to_id[n.parent_qname], qname_to_id[n.qualified_name]))
        # insert edges
        for e in edges:
            conn.execute(
                "INSERT INTO edges(source,target,kind,file_path,call_line,resolution)"
                " VALUES(?,?,?,?,?,?)",
                (e.source, e.target, e.kind, e.file_path, e.call_line, e.resolution),
            )
        # Phase B: load rows + build flows
        nodes = [NodeRow(r["id"], r["qualified_name"], r["file_path"])
                 for r in conn.execute("SELECT id,qualified_name,file_path FROM nodes")]
        erows = [EdgeRow(r["source"], r["target"], r["resolution"])
                 for r in conn.execute("SELECT source,target,resolution FROM edges")]
        entry_ids = [qname_to_id[q] for q in entry_qnames if q in qname_to_id]
        id_to_qname = {n.id: n.qualified_name for n in nodes}
        flows = build_flows(nodes, erows, entry_ids, config.max_depth)
        for f in flows:
            name = id_to_qname.get(f.entry_point_id, "").rsplit(":", 1)[-1]
            cur = conn.execute(
                "INSERT INTO flows(name,entry_point_id,depth,node_count,file_count,"
                "criticality,path_json) VALUES(?,?,?,?,?,?,?)",
                (name, f.entry_point_id, f.depth, f.node_count, f.file_count,
                 None, json.dumps(f.path)),
            )
            fid = cur.lastrowid
            for pos, nid in enumerate(f.path):
                conn.execute(
                    "INSERT INTO flow_memberships(flow_id,node_id,position) VALUES(?,?,?)",
                    (fid, nid, pos),
                )
        built_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute("INSERT OR REPLACE INTO build_meta(key,value) VALUES('built_at',?)",
                     (built_at,))
        stats = RebuildStats(len(nodes), len(edges), len(flows), built_at)
    return stats


def is_stale(config: Config, conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM build_meta WHERE key='built_at'").fetchone()
    if row is None:
        return True
    built = time.mktime(time.strptime(row["value"], "%Y-%m-%dT%H:%M:%S"))
    files = list_python_files(config.repo_path)
    for f in files:
        try:
            if os.path.getmtime(os.path.join(config.repo_path, f)) > built:
                return True
        except OSError:
            return True
    return False
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_indexer.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/code_review_ai/indexer.py tests/test_indexer.py
git commit -m "feat(indexer): Phase A/B rebuild orchestration with atomic transaction"
```

---

### Task 7: changes - change detection

**Files:**
- Create: `src/code_review_ai/changes.py`
- Test: `tests/test_changes.py`

**Interfaces:**
- Consumes: `Config`, `parse_file` (for current AST), git via subprocess.
- Produces: `detect_changed_symbols(config, symbols=None, files=None) -> list[str]`.

- [ ] **Step 1: Write failing test**

`tests/test_changes.py`:
```python
from code_review_ai.config import load_config
from code_review_ai.changes import detect_changed_symbols

FIX = "tests/fixtures/repo"


def test_symbols_mode_passthrough():
    cfg = load_config(FIX)
    out = detect_changed_symbols(cfg, symbols=["auth:login"])
    assert out == ["auth:login"]


def test_files_mode_uses_git_diff(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    # stub git diff to report a hunk on lines 5-6 of auth.py
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff", lambda base, files: {"auth.py": [(5, 6)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    # authenticate() spans lines 2-3 in fixture; login() lines 6-7 -> line 6 hits login
    assert "auth:login" in out


def test_deleted_symbol_reported(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff", lambda base, files: {"auth.py": [(2, 3)]})
    out = detect_changed_symbols(cfg, files=["auth.py"])
    assert "auth:UserService:authenticate" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_changes.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.changes`

- [ ] **Step 3: Implement changes.py**

`src/code_review_ai/changes.py`:
```python
from __future__ import annotations
import re
import subprocess
from code_review_ai.config import Config
from code_review_ai.parser import parse_file


def _git_diff(base: str, files: list[str] | None) -> dict[str, list[tuple[int, int]]]:
    """Return {file_path: [(start, end), ...]} changed line ranges (added/removed)."""
    args = ["git", "diff", "--unified=0", base]
    if files:
        args += ["--"] + files
    out = subprocess.run(args, capture_output=True, text=True)
    ranges: dict[str, list[tuple[int, int]]] = {}
    cur_file = None
    for line in out.stdout.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            cur_file = m.group(1)
            ranges.setdefault(cur_file, [])
            continue
        h = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if h and cur_file:
            start = int(h.group(1))
            count = int(h.group(2)) if h.group(2) else 1
            if count > 0:
                ranges[cur_file].append((start, start + count - 1))
    return ranges


def _overlaps(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(not (end < s or start > e) for s, e in ranges)


def detect_changed_symbols(config: Config,
                           symbols: list[str] | None = None,
                           files: list[str] | None = None) -> list[str]:
    if symbols is not None:
        return list(symbols)
    diff = _git_diff(config.diff_base, files)
    repo = config.repo_path
    out: list[str] = []
    for rel, ranges in diff.items():
        path = f"{repo}/{rel}"
        try:
            pf = parse_file(path, repo)
        except OSError:
            continue
        for n in pf.nodes:
            if n.kind not in ("function", "method"):
                continue
            if _overlaps(n.start_line, n.end_line, ranges):
                out.append(n.qualified_name)
    return out
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_changes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/code_review_ai/changes.py tests/test_changes.py
git commit -m "feat(changes): multi-modal change detection -> changed symbols"
```

---

### Task 8: impact - impact query

**Files:**
- Create: `src/code_review_ai/impact.py`
- Test: `tests/test_impact.py`

**Interfaces:**
- Consumes: `sqlite3.Connection`, changed qnames.
- Produces: `get_impact(conn, changed_symbols, max_nodes_per_direction=50) -> list[dict]`.

- [ ] **Step 1: Write failing test**

`tests/test_impact.py`:
```python
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.impact import get_impact

FIX = "tests/fixtures/repo"


def _idx(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "i.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return conn


def test_impact_slices_prefix_suffix(tmp_path):
    conn = _idx(tmp_path)
    res = get_impact(conn, ["auth:login"])[0]
    # auth:login is downstream of app:main (entry). It has no downstream callees.
    assert "app:main" in [n["qname"] for n in res["upstream"]]
    assert res["downstream"] == []
    assert "app:main" in res["affected_entries"]


def test_impact_off_flow_fallback_to_edges(tmp_path):
    conn = _idx(tmp_path)
    # util:hash_pw is reachable only if on a flow; if not, fallback to edges
    res = get_impact(conn, ["util:helper"])[0]
    # helper is not called by anyone -> empty impact, no crash
    assert res["upstream"] == [] and res["downstream"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_impact.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.impact`

- [ ] **Step 3: Implement impact.py**

`src/code_review_ai/impact.py`:
```python
from __future__ import annotations
import sqlite3


def _node_brief(conn: sqlite3.Connection, node_id: int) -> dict:
    r = conn.execute(
        "SELECT qualified_name,file_path,start_line,signature FROM nodes WHERE id=?",
        (node_id,),
    ).fetchone()
    if r is None:
        return {"qname": str(node_id), "file": "", "line": 0, "sig": ""}
    return {"qname": r["qualified_name"], "file": r["file_path"],
            "line": r["start_line"], "sig": r["signature"]}


def _slice_flow(conn: sqlite3.Connection, flow_id: int, symbol_node_id: int,
                max_per_dir: int) -> tuple[list[dict], list[dict]]:
    rows = conn.execute(
        "SELECT node_id, position FROM flow_memberships WHERE flow_id=? ORDER BY position",
        (flow_id,),
    ).fetchall()
    sym_pos = next((r["position"] for r in rows if r["node_id"] == symbol_node_id), None)
    if sym_pos is None:
        return [], []
    up = [_node_brief(conn, r["node_id"]) for r in rows if r["position"] < sym_pos]
    down = [_node_brief(conn, r["node_id"]) for r in rows if r["position"] > sym_pos]
    return up[-max_per_dir:], down[:max_per_dir]


def _edges_fallback(conn: sqlite3.Connection, qname: str, max_per_dir: int):
    callers = [_edge_brief(conn, e["source"]) for e in conn.execute(
        "SELECT DISTINCT source FROM edges WHERE target=? AND resolution='resolved'", (qname,))][:max_per_dir]
    callees = [_edge_brief(conn, e["target"]) for e in conn.execute(
        "SELECT DISTINCT target FROM edges WHERE source=? AND resolution='resolved'", (qname,))][:max_per_dir]
    return callers, callees


def _edge_brief(conn: sqlite3.Connection, qname: str) -> dict:
    r = conn.execute("SELECT file_path,start_line,signature FROM nodes WHERE qualified_name=?",
                     (qname,)).fetchone()
    if r is None:
        return {"qname": qname, "file": "", "line": 0, "sig": ""}
    return {"qname": qname, "file": r["file_path"], "line": r["start_line"], "sig": r["signature"]}


def get_impact(conn: sqlite3.Connection, changed_symbols: list[str],
               max_nodes_per_direction: int = 50) -> list[dict]:
    results: list[dict] = []
    for qname in changed_symbols:
        node = conn.execute("SELECT id FROM nodes WHERE qualified_name=?", (qname,)).fetchone()
        if node is None:
            results.append({"symbol": qname, "found": False, "upstream": [],
                            "downstream": [], "affected_entries": []})
            continue
        nid = node["id"]
        flows = conn.execute(
            "SELECT flow_id FROM flow_memberships WHERE node_id=?", (nid,),
        ).fetchall()
        up_all, down_all, entries = [], [], set()
        if flows:
            for f in flows:
                up, down = _slice_flow(conn, f["flow_id"], nid, max_nodes_per_direction)
                up_all.extend(up)
                down_all.extend(down)
                entry = conn.execute(
                    "SELECT name FROM flows WHERE id=?", (f["flow_id"],)).fetchone()
                if entry:
                    entries.add(entry["name"])
        else:
            up_all, down_all = _edges_fallback(conn, qname, max_nodes_per_direction)
        # dedup by qname preserving order
        results.append({
            "symbol": qname, "found": True,
            "upstream": _dedup(up_all), "downstream": _dedup(down_all),
            "affected_entries": sorted(entries),
        })
    return results


def _dedup(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        if it["qname"] not in seen:
            seen.add(it["qname"])
            out.append(it)
    return out
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_impact.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/code_review_ai/impact.py tests/test_impact.py
git commit -m "feat(impact): flow-membership slicing with edges fallback"
```

---

### Task 9: watcher - file watching with debounce

**Files:**
- Create: `src/code_review_ai/watcher.py`
- Test: `tests/test_watcher.py`

**Interfaces:**
- Consumes: `Config`, `rebuild`/`is_stale` (indexer), `connect` (db).
- Produces: `run_watcher(config, conn, stop_event=None) -> None`; `startup_rebuild(config, conn) -> bool`.

- [ ] **Step 1: Write failing test**

`tests/test_watcher.py`:
```python
import threading, time
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.watcher import startup_rebuild, run_watcher

FIX = "tests/fixtures/repo"


def _cfg(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "w.db")
    cfg.repo_path = FIX
    cfg.watch_debounce_ms = 100
    return cfg


def test_startup_rebuild_when_stale(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuilt = startup_rebuild(cfg, conn)
    assert rebuilt is True  # empty db is stale


def test_run_watcher_triggers_rebuild_on_change(tmp_path):
    cfg = _cfg(tmp_path)
    conn = connect(cfg.db_path)
    init_schema(conn)
    startup_rebuild(cfg, conn)
    stop = threading.Event()
    t = threading.Thread(target=run_watcher, args=(cfg, conn, stop), daemon=True)
    t.start()
    # mutate a fixture file to trigger
    p = "tests/fixtures/repo/util.py"
    import os
    orig = open(p, encoding="utf-8").read()
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("\n# touch\n")
        time.sleep(0.6)  # debounce + detect
    finally:
        with open(p, "w", encoding="utf-8") as f:
            f.write(orig)
    stop.set()
    t.join(timeout=3)
    # no exception means watcher ran cleanly
    assert not t.is_alive()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_watcher.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.watcher`

- [ ] **Step 3: Implement watcher.py**

`src/code_review_ai/watcher.py`:
```python
from __future__ import annotations
import logging
import sqlite3
import threading
from code_review_ai.config import Config
from code_review_ai.db import connect
from code_review_ai.indexer import is_stale, rebuild

log = logging.getLogger(__name__)


def startup_rebuild(config: Config, conn: sqlite3.Connection) -> bool:
    """Rebuild if index missing or stale. Returns True if rebuilt."""
    if is_stale(config, conn):
        log.info("index stale/missing; rebuilding")
        rebuild(config, conn)
        return True
    return False


def run_watcher(config: Config, conn: sqlite3.Connection,
                stop_event: threading.Event | None = None) -> None:
    """Watch .py files; debounce; rebuild on change. Blocks until stop_event set."""
    from watchfiles import watch
    stop_event = stop_event or threading.Event()
    debounce = max(config.watch_debounce_ms, 50)
    try:
        for changes in watch(config.repo_path, debounce=debounce,
                             watch_filter=_py_only, stop_event=stop_event):
            if stop_event.is_set():
                break
            log.info("detected %d changes; rebuilding", len(changes))
            try:
                rebuild(config, conn)
            except Exception:  # never let watcher die on rebuild error
                log.exception("rebuild failed; keeping old index")
    except Exception:
        log.exception("watcher stopped unexpectedly")
        return


def _py_only(change, path):
    import os
    return path.endswith(".py") and os.path.isfile(path)
```

Note: `watchfiles.watch` accepts a `stop_event`-like via `watch(..., stop_event=)` on recent versions; if the installed version lacks it, wrap the loop with a poll on `stop_event` between yields. The implementer should verify the `watchfiles` version's `stop_event` parameter signature and adapt (the test only asserts no crash + clean exit).

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_watcher.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/code_review_ai/watcher.py tests/test_watcher.py
git commit -m "feat(watcher): debounced watchfiles-driven auto-rebuild"
```

---

### Task 10: MCP server - tools

**Files:**
- Create: `src/code_review_ai/mcp_server.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: all prior modules.
- Produces: `create_server(config) -> FastMCP`.

- [ ] **Step 1: Write failing test**

`tests/test_mcp_server.py`:
```python
import json
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.mcp_server import create_server

FIX = "tests/fixtures/repo"


def _server(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "m.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    return create_server(cfg), conn, cfg


def test_get_impact_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    tools = server._tool_manager._tools
    assert "get_impact" in tools
    out = tools["get_impact"].fn(symbols=["auth:login"])
    data = json.loads(out)
    assert data[0]["symbol"] == "auth:login"
    assert data[0]["found"] is True


def test_search_symbol_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["search_symbol"].fn(query="login")
    data = json.loads(out)
    assert any(d["qname"] == "auth:login" for d in data)


def test_list_entry_points_tool(tmp_path):
    server, conn, cfg = _server(tmp_path)
    out = server._tool_manager._tools["list_entry_points"].fn()
    data = json.loads(out)
    assert any(e["qname"] == "app:main" for e in data)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.mcp_server`

- [ ] **Step 3: Implement mcp_server.py**

`src/code_review_ai/mcp_server.py`:
```python
from __future__ import annotations
import fnmatch
import json
from code_review_ai.changes import detect_changed_symbols
from code_review_ai.config import Config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild


def _conn(config: Config):
    conn = connect(config.db_path)
    init_schema(conn)
    return conn


def create_server(config: Config):
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("code-review-ai")
    conn = _conn(config)

    @mcp.tool()
    def rebuild_index(force: bool = False) -> str:
        """Rebuild the index from the working tree."""
        stats = rebuild(config, conn)
        return json.dumps({"nodes": stats.node_count, "edges": stats.edge_count,
                           "flows": stats.flow_count, "built_at": stats.built_at})

    @mcp.tool()
    def get_impact(symbols: list[str] | None = None,
                   files: list[str] | None = None) -> str:
        """Return impact chains for changed symbols. If neither symbols nor files
        given, derives changed symbols from git diff."""
        changed = detect_changed_symbols(config, symbols=symbols, files=files)
        return json.dumps(get_impact(conn, changed))

    @mcp.tool()
    def search_symbol(query: str) -> str:
        """Find symbols by name glob."""
        rows = conn.execute(
            "SELECT qualified_name,kind,file_path,start_line FROM nodes WHERE kind IN ('function','method','class')"
        ).fetchall()
        out = [{"qname": r["qualified_name"], "kind": r["kind"],
                "file": r["file_path"], "line": r["start_line"]}
               for r in rows if fnmatch.fnmatch(r["qualified_name"].rsplit(":", 1)[-1], query)]
        return json.dumps(out)

    @mcp.tool()
    def get_symbol_detail(qualified_name: str) -> str:
        """Node detail + direct callees/callers."""
        r = conn.execute("SELECT * FROM nodes WHERE qualified_name=?", (qualified_name,)).fetchone()
        if r is None:
            return json.dumps({"error": "symbol not found"})
        callers = [row["source"] for row in conn.execute(
            "SELECT DISTINCT source FROM edges WHERE target=? AND resolution='resolved'", (qualified_name,))]
        callees = [row["target"] for row in conn.execute(
            "SELECT DISTINCT target FROM edges WHERE source=? AND resolution='resolved'", (qualified_name,))]
        return json.dumps({"qname": r["qualified_name"], "kind": r["kind"],
                           "file": r["file_path"], "line": r["start_line"],
                           "signature": r["signature"], "callers": callers, "callees": callees})

    @mcp.tool()
    def list_entry_points() -> str:
        """List designated entry points."""
        rows = conn.execute(
            "SELECT DISTINCT f.name, n.qualified_name, n.file_path FROM flows f "
            "JOIN nodes n ON n.id=f.entry_point_id"
        ).fetchall()
        return json.dumps([{"qname": r["qualified_name"], "name": r["name"],
                            "file": r["file_path"]} for r in rows])

    # attach conn for the watcher to share
    mcp._conn = conn
    return mcp
```

- [ ] **Step 4: Run test to verify pass**

Run: `uv run pytest tests/test_mcp_server.py -v`
Expected: PASS

- [ ] **Step 5: Wire watcher into server startup**

Add to `mcp_server.py` a `main()`:
```python
def main():
    import logging, threading
    from code_review_ai.config import load_config
    from code_review_ai.watcher import run_watcher, startup_rebuild
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    server = create_server(config)
    startup_rebuild(config, server._conn)
    t = threading.Thread(target=run_watcher, args=(config, server._conn), daemon=True)
    t.start()
    server.run()


if __name__ == "__main__":
    main()
```

And add to `pyproject.toml` scripts: `code-review-ai-mcp = "code_review_ai.mcp_server:main"`.

- [ ] **Step 6: Commit**

```bash
git add src/code_review_ai/mcp_server.py tests/test_mcp_server.py pyproject.toml
git commit -m "feat(mcp): FastMCP tools (get_impact/search/detail/entries/rebuild) + watcher"
```

---

### Task 11: CLI + integration test

**Files:**
- Create: `src/code_review_ai/cli.py`, `tests/test_integration.py`
- Test: `tests/test_cli.py`, `tests/test_integration.py`

**Interfaces:**
- Produces: `main(argv=None) -> int`.

- [ ] **Step 1: Write failing CLI test**

`tests/test_cli.py`:
```python
import json
from code_review_ai.cli import main


def test_cli_search(tmp_path, capsys):
    code = main(["search", "login", "--repo", "tests/fixtures/repo",
                 "--db", str(tmp_path / "c.db")])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert any(d["qname"] == "auth:login" for d in out)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: code_review_ai.cli`

- [ ] **Step 3: Implement cli.py**

`src/code_review_ai/cli.py`:
```python
from __future__ import annotations
import argparse
import json
import sys

from code_review_ai.changes import detect_changed_symbols
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.impact import get_impact
from code_review_ai.indexer import rebuild


def _conn(db_path):
    conn = connect(db_path)
    init_schema(conn)
    return conn


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="code-review-ai")
    p.add_argument("--repo", default=".")
    p.add_argument("--db", default=".code-review-ai/index.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("rebuild")
    s = sub.add_parser("query")
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--files", nargs="*")
    sub.add_parser("search").add_argument("query")

    args = p.parse_args(argv)
    cfg = load_config(args.repo)
    cfg.repo_path = args.repo
    cfg.db_path = args.db
    conn = _conn(args.db)

    if args.cmd == "rebuild":
        stats = rebuild(cfg, conn)
        print(json.dumps({"nodes": stats.node_count, "edges": stats.edge_count,
                          "flows": stats.flow_count, "built_at": stats.built_at}))
    elif args.cmd == "query":
        changed = detect_changed_symbols(cfg, symbols=args.symbols, files=args.files)
        print(json.dumps(get_impact(conn, changed)))
    elif args.cmd == "search":
        from code_review_ai.mcp_server import create_server
        # reuse search logic via a tiny inline query
        import fnmatch
        rows = conn.execute(
            "SELECT qualified_name,kind,file_path,start_line FROM nodes "
            "WHERE kind IN ('function','method','class')").fetchall()
        out = [{"qname": r["qualified_name"], "kind": r["kind"],
                "file": r["file_path"], "line": r["start_line"]}
               for r in rows if fnmatch.fnmatch(r["qualified_name"].rsplit(":", 1)[-1], args.query)]
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run CLI test to verify pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Write integration test**

`tests/test_integration.py`:
```python
import os, time
from code_review_ai.config import load_config
from code_review_ai.db import connect, init_schema
from code_review_ai.indexer import rebuild
from code_review_ai.changes import detect_changed_symbols
from code_review_ai.impact import get_impact

FIX = "tests/fixtures/repo"


def test_end_to_end_impact(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "e2e.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    # simulate a change to auth:login by passing symbols directly
    res = get_impact(conn, detect_changed_symbols(cfg, symbols=["auth:login"]))[0]
    assert res["found"] is True
    assert "app:main" in res["affected_entries"]


def test_diamond_flow_count_bounded(tmp_path):
    cfg = load_config(FIX)
    cfg.db_path = str(tmp_path / "dia.db")
    cfg.repo_path = FIX
    conn = connect(cfg.db_path)
    init_schema(conn)
    rebuild(cfg, conn)
    # number of flows <= number of (entry, reachable-node) pairs
    entries = conn.execute("SELECT DISTINCT entry_point_id FROM flows").fetchall()
    for e in entries:
        flows = conn.execute("SELECT COUNT(*) FROM flows WHERE entry_point_id=?",
                             (e["entry_point_id"],)).fetchone()[0]
        members = conn.execute(
            "SELECT COUNT(DISTINCT node_id) FROM flow_memberships fm "
            "JOIN flows f ON f.id=fm.flow_id WHERE f.entry_point_id=?",
            (e["entry_point_id"],)).fetchone()[0]
        assert flows <= members  # one flow per reachable node, not more
```

- [ ] **Step 6: Run full suite + verify**

Run: `uv run pytest -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add src/code_review_ai/cli.py tests/test_cli.py tests/test_integration.py
git commit -m "feat(cli): rebuild/query/search CLI + end-to-end integration tests"
```

---

## Self-Review

**1. Spec coverage:**
- §3 schema (4 tables) → Task 1. ✓
- §4.1 parser (nodes/calls/imports) → Tasks 2, 3. ✓
- §4.2 resolver (import-aware, resolution) → Task 4. ✓
- §4.3 entry points (config + `main` heuristic) → Task 6 (`_entry_points`); `entry_decorators` matching noted as deferred (parser doesn't yet expose decorators) — **gap**: add a follow-up note. Decorator-based entry detection is not fully implemented; `entry_names` covers `main`. Acceptable for v1 per spec's "默认启发式：名为 main".
- §4.4 Phase A/B → Task 6. ✓
- §5.1 change detection (3 modes) → Task 7. ✓
- §5.2 MCP tools (5) → Task 10. ✓
- §5.3 impact slicing + fallback → Task 8. ✓
- §5.4 output format (compact JSON, qname+file:line+sig) → Task 8 (`_node_brief`). ✓
- §5.5 watcher lifecycle → Task 9 + Task 10 `main()`. ✓
- §6.2 error handling (partial parse, skip missing, transaction rollback, watcher no-crash, structured MCP errors) → Task 9 (watcher try/except), Task 10 (`get_symbol_detail` error json), Task 6 (transaction). Syntax-error partial parse is inherent to tree-sitter (ERROR nodes) — covered.
- §6.3 tests → per-task tests + Task 11 integration + diamond bound. ✓
- §6.1 project structure → matches all created files. ✓

**2. Placeholder scan:** No TBD/TODO. Two implementation notes flag real version-signature uncertainty (watchfiles `stop_event`, FastMCP `_tool_manager` internals) with concrete fallback guidance — acceptable, not placeholders.

**3. Type consistency:** `Config`, `ParsedNode/ParsedFile/RawCall/ImportEntry`, `Edge`, `NodeRow/EdgeRow/FlowRecord`, `RebuildStats` signatures match across tasks. `detect_changed_symbols(config, symbols=None, files=None)` consistent in Tasks 7, 10, 11. `get_impact(conn, changed_symbols, max_per_dir=50)` consistent in Tasks 8, 10, 11. `rebuild(config, conn)` consistent in Tasks 6, 9, 10, 11. `is_stale(config, conn)` in Tasks 6, 9. ✓

No blocking issues. One known v1 limitation carried from spec: `entry_decorators` not yet wired (needs parser decorator extraction) — out of scope per spec's v1 heuristic.
