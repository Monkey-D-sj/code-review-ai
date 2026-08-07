# change summary uncovered_changes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_change_summary`'s diff path stops silently dropping changes the parser can't attribute — every changed hunk is either covered by a function/method/class (via `changed_functions`) or listed in a new `uncovered_changes` key.

**Architecture:** `_git_diff` returns per-hunk `(start, count)` plus a deleted-files set; a new `_diff_coverage` walks the full diff and splits it into function-level records vs uncovered hunk entries; `build_change_summary` emits both. `_changed_functions` becomes a thin wrapper so `detect_changed_symbols` is untouched.

**Tech Stack:** Python 3.14, tree-sitter, git CLI, pytest (`uv run pytest`).

## Global Constraints

- `changed_functions` record shape (`qname/kind/file/start_line/end_line`) stays **exactly** unchanged — backward compat.
- `_git_diff` returns per-hunk `(start, count)` — git's `+b,m` shape, position + size together, never split.
- Every changed hunk is either covered by a function/method/class node or appears in `uncovered_changes` (the invariant).
- `symbols=` path returns `uncovered_changes: []` so both paths share one schema.
- `detect_changed_symbols` / `get_impact` / `get_test_impact` untouched.
- Top-level `summary` keys `files_changed`/`lines_added`/`lines_removed` keep their numstat semantics.
- No new config keys; feature always on.

---

### Task 1: `_git_diff` per-hunk shape + deleted-file detection

**Files:**
- Modify: `code_review_ai/changes.py:22-55` (`_git_diff`, `_overlaps`), `:126-139` (`detect_changed_symbols`), `:170-187` (`build_change_summary`)
- Test: `tests/test_changes.py`

**Interfaces:**
- Produces: `_git_diff(base: str, files: list[str] | None, cwd: str | None = None) -> tuple[dict[str, list[tuple[int, int]]], set[str]]` — `({file: [(start, count), ...]}, deleted_files)`.
- Produces: `_overlaps(start: int, end: int, hunks: list[tuple[int, int]]) -> bool` — hunks are `(start, count)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_changes.py`:

```python
def test_git_diff_per_hunk_shape_and_deleted(tmp_path):
    """_git_diff returns per-hunk (start, count) and flags deleted files."""
    repo = _git_repo(tmp_path)
    _commit(repo, "a.py", "x = 1\ny = 2\nz = 3\n")
    _commit(repo, "b.py", "keep = True\n")
    (repo / "b.py").unlink()                                          # tracked deletion
    (repo / "a.py").write_text("x = 1\ny = 2\nz = 3\nw = 4\n", encoding="utf-8")
    import code_review_ai.changes as ch
    ranges, deleted = ch._git_diff("HEAD", None, str(repo))
    assert "b.py" in deleted
    assert ranges["a.py"] == [(4, 1)]   # +4,1 hunk: new-side start=4, count=1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_changes.py::test_git_diff_per_hunk_shape_and_deleted -v`
Expected: FAIL — `_git_diff` returns a dict, not a `(ranges, deleted)` tuple (or "not in deleted").

- [ ] **Step 3: Implement `_git_diff` and `_overlaps`**

In `code_review_ai/changes.py`, replace `_git_diff` (lines 22-51):

```python
def _git_diff(base: str, files: list[str] | None,
              cwd: str | None = None) -> tuple[dict[str, list[tuple[int, int]]], set[str]]:
    """Return ({file: [(start, count), ...]}, deleted_files).

    Each hunk is git's new-side ``+b,m`` (start, count) — position and size
    together. deleted_files is the set of pure deletions (``+++ /dev/null``),
    which produce no + hunks and so would otherwise be invisible.
    """
    args = ["git", "diff", "--unified=0", base]
    if files:
        args += ["--"] + files
    # git diff output is UTF-8; text=True would decode with the locale codepage
    # (GBK on zh-CN Windows) and crash on non-ASCII content. errors="replace"
    # keeps the @@ line-range parsing robust to any undecodable bytes.
    out = subprocess.run(args, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=cwd)
    if out.returncode != 0:
        raise RuntimeError(
            f"git diff failed (exit {out.returncode}): {out.stderr.strip()}"
        )
    ranges: dict[str, list[tuple[int, int]]] = {}
    deleted: set[str] = set()
    cur_file: str | None = None
    cur_a: str | None = None
    for line in out.stdout.splitlines():
        a = re.match(r"^--- a/(.+)$", line)
        if a:
            cur_a = a.group(1)
            continue
        b = re.match(r"^\+\+\+ b/(.+)$", line)
        if b:
            cur_file = b.group(1)
            ranges.setdefault(cur_file, [])
            continue
        if re.match(r"^\+\+\+ (?:b/)?/dev/null$", line) and cur_a:
            deleted.add(cur_a)
            cur_file = None
            continue
        h = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if h and cur_file:
            start = int(h.group(1))
            count = int(h.group(2)) if h.group(2) else 1
            if count > 0:
                ranges[cur_file].append((start, count))
    return ranges, deleted
```

Replace `_overlaps` (lines 54-55):

```python
def _overlaps(start: int, end: int, hunks: list[tuple[int, int]]) -> bool:
    """True if node range [start, end] overlaps any hunk (start, count)."""
    return any(not (end < s or start > s + c - 1) for s, c in hunks)
```

- [ ] **Step 4: Update the two call sites that unpack `_git_diff`**

`detect_changed_symbols` (line 137):

```python
    diff, _ = _git_diff(_resolve_diff_base(config), files, config.repo_path)
    return [record["qname"] for record in _changed_functions(
        config, diff, kinds=("function", "method"))]
```

`build_change_summary` (line 180):

```python
    base = _resolve_diff_base(config)
    diff, _deleted = _git_diff(base, files, config.repo_path)
    numstat = _git_numstat(base, files, config.repo_path)
    functions = _changed_functions(config, diff)
```

- [ ] **Step 5: Update the three tests that monkeypatch `_git_diff`**

In `tests/test_changes.py`, all monkeypatched `_git_diff` lambdas must return a `(ranges, deleted)` tuple now:

- `test_files_mode_uses_git_diff` (line 89): `lambda base, files, cwd=None: ({"auth.py": [(5, 6)]}, set())`
- `test_deleted_symbol_reported` (line 112): `lambda base, files, cwd=None: ({"auth.py": [(2, 3)]}, set())`
- `test_build_change_summary_diff_path` (line 173): `lambda base, files, cwd=None: ({"auth.py": [(6, 7)]}, set())`

(Each `(start, count)` here widens coverage vs the old `(start, end)` read, but all three assertions still hold: `(5,6)`→[5,10] hits login [6,7]; `(2,3)`→[2,4] hits authenticate [2,3]; `(6,7)`→[6,12] hits login [6,7] and not UserService [1,3].)

- [ ] **Step 6: Run the full test file to verify all green**

Run: `uv run pytest tests/test_changes.py -v`
Expected: PASS (including the new deleted/per-hunk test).

- [ ] **Step 7: Commit**

```bash
git add code_review_ai/changes.py tests/test_changes.py
git commit -m "feat(changes): _git_diff per-hunk (start,count) + deleted detection
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: `_diff_coverage` + `uncovered_changes` in the diff path

**Files:**
- Modify: `code_review_ai/changes.py:100-123` (`_changed_functions` → wrapper), add `_diff_coverage`, `:170-187` (`build_change_summary`)
- Test: `tests/test_changes.py`

**Interfaces:**
- Consumes: `_git_diff(base, files, cwd=None) -> (ranges, deleted)`, `_git_numstat(base, files, cwd=None) -> {file: (added, removed)}`, `_overlaps(start, end, hunks)`.
- Produces: `_diff_coverage(config, diff_ranges: dict[str, list[tuple[int,int]]], numstat: dict[str, tuple[int,int]], deleted: set[str], kinds=("function","method","class")) -> tuple[list[dict], list[dict]]` — `(changed_function_records, uncovered_changes)` where each uncovered entry is `{"file": str, "hunks": [{"start": int, "count": int}], ["deleted": True]}`.
- Produces: `build_change_summary` returns `{"summary": {...numstat keys..., "uncovered_changes": N}, "changed_functions": [...], "uncovered_changes": [...]}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_changes.py`:

```python
def test_uncovered_unsupported_extension(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"README.md": [(1, 5)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"README.md": (5, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["summary"]["uncovered_changes"] == 1
    assert out["uncovered_changes"] == [
        {"file": "README.md", "hunks": [{"start": 1, "count": 5}]}]


def test_uncovered_module_level_hunk(tmp_path, monkeypatch):
    """Fixture line 5 is blank module-level — outside UserService(1-3)/login(6-7)."""
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"auth.py": [(5, 1)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (1, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["changed_functions"] == []
    assert out["uncovered_changes"] == [
        {"file": "auth.py", "hunks": [{"start": 5, "count": 1}]}]


def test_partial_coverage_splits_hunks(tmp_path, monkeypatch):
    """One file, two hunks: line 6 (inside login) covered, line 4 (module) not."""
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"auth.py": [(6, 1), (4, 1)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (2, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert [r["qname"] for r in out["changed_functions"]] == [Q("auth", "login")]
    assert out["uncovered_changes"] == [
        {"file": "auth.py", "hunks": [{"start": 4, "count": 1}]}]


def test_binary_file_uncovered(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"logo.png": (0, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["uncovered_changes"] == [{"file": "logo.png", "hunks": []}]


def test_deleted_file_uncovered(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({}, {"foo.py"}))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"foo.py": (0, 3)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["uncovered_changes"] == [{"file": "foo.py", "hunks": [], "deleted": True}]


def test_uncovered_invariant(tmp_path, monkeypatch):
    """files_changed == distinct covered files + uncovered entries."""
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"auth.py": [(6, 1), (4, 1)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (2, 0), "logo.png": (0, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    covered = {r["file"] for r in out["changed_functions"]}
    assert len(covered) + len(out["uncovered_changes"]) == out["summary"]["files_changed"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_changes.py -k "uncovered or partial_coverage or binary_file or deleted_file or invariant" -v`
Expected: FAIL — `build_change_summary` returns no `uncovered_changes` key.

- [ ] **Step 3: Implement `_diff_coverage` and refactor `_changed_functions`**

In `code_review_ai/changes.py`, replace `_changed_functions` (lines 100-123) with:

```python
def _diff_coverage(config: Config, diff_ranges: dict[str, list[tuple[int, int]]],
                   numstat: dict[str, tuple[int, int]], deleted: set[str],
                   kinds: tuple[str, ...] = ("function", "method", "class"),
                   ) -> tuple[list[dict], list[dict]]:
    """Split a diff into function-level records and uncovered hunks.

    Every changed file (numstat ∪ diff_ranges) is accounted for: hunks that
    overlap a function/method/class node become records; everything else
    (unsupported extension, module-level change, binary, deleted) becomes an
    uncovered_changes entry — {file, hunks: [{start, count}], deleted?} — so
    no change silently disappears. Returns (records, uncovered_changes).
    """
    repo = config.repo_path
    records: list[dict] = []
    uncovered: list[dict] = []
    for rel in dict.fromkeys([*numstat, *diff_ranges]):
        if rel in deleted:
            uncovered.append({"file": rel, "hunks": [], "deleted": True})
            continue
        hunks = diff_ranges.get(rel)
        if not hunks:
            # in the diff but no line hunks — binary / rename
            uncovered.append({"file": rel, "hunks": []})
            continue
        path = f"{repo}/{rel}"
        try:
            pf = parse_file(path, repo)
        except (OSError, ValueError):
            # OSError: file gone from disk. ValueError: unsupported extension
            # (e.g. *.md in the diff) — nothing to attribute, hunks stay raw.
            uncovered.append({"file": rel,
                              "hunks": [{"start": s, "count": c} for s, c in hunks]})
            continue
        changed = [n for n in pf.nodes
                   if n.kind in kinds and _overlaps(n.start_line, n.end_line, hunks)]
        for n in changed:
            records.append({"qname": n.qualified_name, "kind": n.kind,
                            "file": rel, "start_line": n.start_line,
                            "end_line": n.end_line})
        uncovered_hunks = [h for h in hunks
                           if not any(_overlaps(n.start_line, n.end_line, [h])
                                      for n in changed)]
        if uncovered_hunks:
            uncovered.append({"file": rel,
                              "hunks": [{"start": s, "count": c} for s, c in uncovered_hunks]})
    return records, uncovered


def _changed_functions(config: Config, diff_ranges: dict[str, list[tuple[int, int]]],
                       kinds: tuple[str, ...] = ("function", "method", "class")) -> list[dict]:
    """Backward-compat wrapper: function/method/class records only."""
    records, _ = _diff_coverage(config, diff_ranges, {}, set(), kinds=kinds)
    return records
```

- [ ] **Step 4: Update `build_change_summary` diff path**

Replace the tail of `build_change_summary` (lines 179-187):

```python
    base = _resolve_diff_base(config)
    diff, deleted = _git_diff(base, files, config.repo_path)
    numstat = _git_numstat(base, files, config.repo_path)
    functions, uncovered = _diff_coverage(config, diff, numstat, deleted)
    return {"summary": {"files_changed": len(numstat),
                        "lines_added": sum(added for added, _ in numstat.values()),
                        "lines_removed": sum(removed for _, removed in numstat.values()),
                        "changed_functions": len(functions),
                        "uncovered_changes": len(uncovered)},
            "changed_functions": functions,
            "uncovered_changes": uncovered}
```

Also update `build_change_summary`'s docstring (lines 172-176) to mention the new key:

```python
    """Change summary + changed functions + uncovered changes. With
    `symbols`, resolve each qname from the graph; otherwise compute from the
    git diff of `files` (or the whole tree) against a resolved base
    (diff_base, else the branch's upstream, else HEAD^). `uncovered_changes`
    lists files whose changes no function/class covers — module-level hunks,
    unsupported extensions, binary and deleted files — so the review sees
    what the graph cannot attribute. Returns {"summary", "changed_functions",
    "uncovered_changes"}.
    Raises RuntimeError if the git diff fails (e.g. no commits at all)."""
```

- [ ] **Step 5: Update `test_build_change_summary_diff_path`**

Replace its body with (adds the tuple return, `uncovered_changes` assertions):

```python
def test_build_change_summary_diff_path(tmp_path, monkeypatch):
    cfg = load_config(FIX)
    import code_review_ai.changes as ch
    monkeypatch.setattr(ch, "_git_diff",
                        lambda base, files, cwd=None: ({"auth.py": [(6, 7)]}, set()))
    monkeypatch.setattr(ch, "_git_numstat",
                        lambda base, files, cwd=None: {"auth.py": (10, 2), "logo.png": (0, 0)})
    out = build_change_summary(cfg, _conn(tmp_path))
    assert out["summary"] == {"files_changed": 2, "lines_added": 10,
                              "lines_removed": 2, "changed_functions": 1,
                              "uncovered_changes": 1}
    assert out["changed_functions"] == [
        {"qname": Q("auth", "login"), "kind": "function",
         "file": "auth.py", "start_line": 6, "end_line": 7}]
    assert out["uncovered_changes"] == [{"file": "logo.png", "hunks": []}]
```

- [ ] **Step 6: Run the test file to verify all green**

Run: `uv run pytest tests/test_changes.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add code_review_ai/changes.py tests/test_changes.py
git commit -m "feat(changes): uncovered_changes in change summary diff path
Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: `symbols=` path, CLI/MCP shape assertions, docstrings

**Files:**
- Modify: `code_review_ai/changes.py:149-167` (`_symbols_summary`), `code_review_ai/mcp_server.py:80-88` (docstring)
- Test: `tests/test_changes.py`, `tests/test_cli.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `_symbols_summary(config, conn, symbols)` — now also returns `uncovered_changes: []` and `summary.uncovered_changes: 0`.
- Produces: `get_change_summary` output schema = `{"summary", "changed_functions", "uncovered_changes"}` on both paths.

- [ ] **Step 1: Write the failing test**

Update `test_build_change_summary_symbols_path` in `tests/test_changes.py` — append after its existing asserts:

```python
    assert out["uncovered_changes"] == []
    assert out["summary"]["uncovered_changes"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_changes.py::test_build_change_summary_symbols_path -v`
Expected: FAIL — `KeyError: 'uncovered_changes'`.

- [ ] **Step 3: Implement `_symbols_summary` change**

In `code_review_ai/changes.py`, replace the return of `_symbols_summary` (lines 165-167):

```python
    return {"summary": {"files_changed": len(files), "lines_added": 0,
                        "lines_removed": 0, "changed_functions": len(symbols),
                        "uncovered_changes": 0},
            "changed_functions": records,
            "uncovered_changes": []}
```

- [ ] **Step 4: Update the two set()-shape assertions**

- `tests/test_cli.py:31`: `assert set(data) == {"summary", "changed_functions", "uncovered_changes"}`
- `tests/test_mcp_server.py:97`: `assert set(data) == {"summary", "changed_functions", "uncovered_changes"}`

- [ ] **Step 5: Update `get_change_summary` docstring in mcp_server.py**

Replace the docstring (lines 83-86):

```python
        """Change summary: from the git diff (diff_base) compute `summary`
        (diff stats incl. uncovered_changes count) + `changed_functions`
        (changed function/method/class detail) + `uncovered_changes` (files
        whose changes no function/class covers — module-level hunks,
        unsupported extensions, binary and deleted files — so the review
        knows what the graph cannot attribute). Pass explicit `symbols` to
        resolve those qnames from the graph instead of the diff. Returns a
        JSON object."""
```

- [ ] **Step 6: Run the full suite to verify all green**

Run: `uv run pytest -v`
Expected: PASS (whole suite).

- [ ] **Step 7: Commit**

```bash
git add code_review_ai/changes.py code_review_ai/mcp_server.py tests/test_changes.py tests/test_cli.py tests/test_mcp_server.py
git commit -m "feat(changes): uncovered_changes on symbols path + docs
Co-Authored-By: Claude <noreply@anthropic.com>"
```
