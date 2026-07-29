
import fnmatch
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from code_review_ai.qname import join as qn_join

import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())
_TLS = threading.local()


def _parser(language: Language = PY_LANGUAGE) -> Parser:
    """A per-thread, per-language Parser.

    tree-sitter's Parser is not thread-safe (it holds mutable parse state),
    so the watcher rebuild thread and the main thread's diff-parsing
    (changes.detect_changed_symbols) must not share one. Each thread lazily
    gets its own parser per Language; set_language is avoided (it resets
    internal state mid-parse).
    """
    cache = getattr(_TLS, "parsers", None)
    if cache is None:
        cache = {}
        _TLS.parsers = cache
    p = cache.get(id(language))
    if p is None:
        p = Parser(language)
        cache[id(language)] = p
    return p

# Language-specific node-type configuration. Add a new entry per language.
LANG = {
    "python": {
        "def_nodes": {           # node_type -> semantic kind
            "class_definition": "class",
            "function_definition": "function",
        },
        "scope_nodes": {         # node types that open a new scope
            "class_definition",
            "function_definition",
        },
        "call_node": "call",
        "import_nodes": {
            "import_statement",
            "import_from_statement",
        },
    },
    "typescript": {
        "def_nodes": {
            "function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "method",
        },
        "scope_nodes": {
            "function_declaration", "class_declaration", "method_definition",
        },
        "call_node": "call_expression",
        "import_nodes": {
            "import_statement",
            "export_statement",
        },
        "detect_arrow_in_vars": True,
    },
    "javascript": {
        "def_nodes": {
            "function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "method",
        },
        "scope_nodes": {
            "function_declaration", "class_declaration", "method_definition",
        },
        "call_node": "call_expression",
        "import_nodes": {
            "import_statement",
            "export_statement",
        },
        "detect_arrow_in_vars": True,
    },
}

# Extension → (lang_name, lang_dict, tree_sitter_language)
_EXT_MAP: dict[str, tuple[str, dict, Language]] = {
    ".py": ("python", LANG["python"], PY_LANGUAGE),
    ".ts": ("typescript", LANG["typescript"], TS_LANGUAGE),
    ".tsx": ("typescript", LANG["typescript"], TSX_LANGUAGE),
    ".js": ("javascript", LANG["javascript"], TS_LANGUAGE),
    ".mjs": ("javascript", LANG["javascript"], TS_LANGUAGE),
    ".cjs": ("javascript", LANG["javascript"], TS_LANGUAGE),
    ".jsx": ("javascript", LANG["javascript"], TSX_LANGUAGE),
    ".vue": ("typescript", LANG["typescript"], TS_LANGUAGE),
}

# Public — derived from _EXT_MAP, single source of truth for file matching.
SOURCE_GLOBS = [f"*{ext}" for ext in _EXT_MAP]     # ["*.py", "*.ts", …]
SOURCE_SUFFIXES = tuple(_EXT_MAP.keys())            # (".py", ".ts", …)


def _lang_for_path(file_path: str) -> tuple[str, dict, Language]:
    """Return (language_name, lang_dict, tree_sitter_language) for a file path."""
    for ext, entry in _EXT_MAP.items():
        if file_path.endswith(ext):
            return entry
    raise ValueError(f"unsupported file extension: {file_path}")


# Call-form constants — for RawCall.call_form and resolution dispatch.
CALL_SIMPLE    = "simple"     # bare name:  login()
CALL_ATTRIBUTE = "attribute"  # dotted:     a.login()
CALL_OTHER     = "other"      # subscript, call-chain, etc.: vals[0]()  f()()

# ── helpers ──────────────────────────────────────────────────────────

def _is_scope(node_type: str, lang: dict) -> bool:
    return node_type in lang["scope_nodes"]


@dataclass
class ParsedNode:
    qualified_name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str
    parent_qname: str | None
    language: str = "python"


@dataclass
class RawCall:
    """A call-site extracted from AST, before resolution.

    source_qname — who makes the call (qualified name of enclosing function/module)
    target_expr — the raw text of the call target, e.g. ``login``, ``a.login``, ``vals[0]``
    call_form  — CALL_SIMPLE / CALL_ATTRIBUTE / CALL_OTHER
    """
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
    language: str = "python"
    nodes: list[ParsedNode] = field(default_factory=list)
    raw_calls: list[RawCall] = field(default_factory=list)
    imports: list[ImportEntry] = field(default_factory=list)


def list_source_files(repo_path: str, extensions: list[str] | None = None) -> list[str]:
    """Return sorted relative paths of source files from git.

    extensions: list of git ls-files globs like ["*.py", "*.ts"]. Default: ["*.py"]
    """
    if extensions is None:
        extensions = ["*.py"]
    files: set[str] = set()
    for ext in extensions:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", ext],
            cwd=repo_path, capture_output=True, text=True, check=True,
        )
        files.update(out.stdout.splitlines())
    return sorted(files)


def filter_excluded(files: list[str], patterns: list[str]) -> list[str]:
    """Return files that do NOT match any exclude glob pattern.

    Patterns are matched against the relative path (e.g. ``tests/test_auth.py``)
    and also the bare filename. A leading ``*/`` is stripped before matching
    so users can write ``*/test*`` to match files in any directory.
    """
    if not patterns:
        return files
    keep: list[str] = []
    for f in files:
        basename = Path(f).name
        excluded = False
        for raw in patterns:
            p = raw.lstrip("*/")
            if fnmatch.fnmatch(f, p) or fnmatch.fnmatch(basename, p):
                excluded = True
                break
        if not excluded:
            keep.append(f)
    return keep


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


def _walk_defs_typed(node, source, module_qname, scope_qname, parent_kind, lang, output):
    for child in node.children:
        t = child.type
        if t in lang["def_nodes"]:
            # method_definition outside a class is just an object-literal
            # shorthand — not a real definition
            if t == "method_definition" and parent_kind != "class":
                _walk_defs_typed(child, source, module_qname, scope_qname, parent_kind, lang, output)
                continue
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue  # anonymous function/class — skip
            name = name_node.text.decode("utf-8")
            qn = qn_join(module_qname, name, scope_qname)
            kind = lang["def_nodes"][t]
            if kind == "function" and parent_kind == "class":
                kind = "method"
            output.append(ParsedNode(
                qualified_name=qn, kind=kind, file_path="",
                start_line=child.start_point[0] + 1, end_line=child.end_point[0] + 1,
                signature=_sig(source, child), parent_qname=scope_qname,
            ))
            _walk_defs_typed(child, source, module_qname, qn, kind, lang, output)
        elif lang.get("detect_arrow_in_vars") and t == "variable_declarator":
            _maybe_arrow_def(child, source, module_qname, scope_qname, parent_kind, lang, output)
        else:
            _walk_defs_typed(child, source, module_qname, scope_qname, parent_kind, lang, output)


def _maybe_arrow_def(node, source, module_qname, scope_qname, parent_kind, lang, output):
    """Handle const/let x = () => {} or const x = function() {} — a
    variable_declarator whose value is an arrow_function or function_expression."""
    value = node.child_by_field_name("value")
    if value is None:
        return
    if value.type not in ("arrow_function", "function_expression"):
        _walk_defs_typed(node, source, module_qname, scope_qname, parent_kind, lang, output)
        return
    name_node = node.child_by_field_name("name")
    if name_node is None:
        return
    name = name_node.text.decode("utf-8")
    qn = qn_join(module_qname, name, scope_qname)
    kind = "method" if parent_kind == "class" else "function"
    output.append(ParsedNode(
        qualified_name=qn, kind=kind, file_path="",
        start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
        signature=_sig(source, value), parent_qname=scope_qname,
    ))
    _walk_defs_typed(value, source, module_qname, qn, kind, lang, output)


def parse_file(file_path: str, repo_root: str, lang: dict | None = None) -> ParsedFile:
    if lang is None:
        lang_name, lang, ts_lang = _lang_for_path(file_path)
    else:
        lang_name = "python"
        ts_lang = PY_LANGUAGE
    module_qname = _module_qname(file_path, repo_root)
    source = Path(file_path).read_bytes()
    line_offset = 0
    original_line_count = 0
    if file_path.endswith(".vue"):
        original_line_count = source.count(b"\n") + 1
        source, line_offset = _extract_vue_script(source)
    tree = _parser(ts_lang).parse(source)
    root = tree.root_node

    pf = ParsedFile(file_path=file_path, module_qname=module_qname, language=lang_name)
    # 当前文件 module — use original file range for .vue
    mod_end = original_line_count or root.end_point[0] + 1
    pf.nodes.append(ParsedNode(
        qualified_name=module_qname, kind="module", file_path=file_path,
        start_line=1, end_line=mod_end,
        signature="", parent_qname=None,
    ))

    # handle nodes
    _walk_defs_typed(root, source, module_qname, None, None, lang, pf.nodes)
    # handle edges
    _walk_calls(root, module_qname, None, lang, pf.raw_calls)
    pf.imports = _extract_imports(root, module_qname, lang, lang_name)

    # Batch-fill file_path, language, and apply line offset — constant across one file
    for n in pf.nodes:
        n.file_path = file_path
        n.language = lang_name
        if line_offset and n.kind != "module":
            n.start_line += line_offset
            n.end_line += line_offset
    for c in pf.raw_calls:
        c.file_path = file_path
        if line_offset:
            c.call_line += line_offset

    return pf


def _call_target(func_node) -> tuple[str, str]:
    """Return (target_expr, call_form) for a call's function child.

    Node types are tree-sitter AST labels:
      identifier         — bare name:  login()
      attribute          — dotted call (Python): a.login()
      member_expression  — dotted call (TS/JS):  a.login()
      other              — subscript, call-chain, etc.: vals[0]()  f()()
    """
    t = func_node.type
    if t == "identifier":
        return func_node.text.decode("utf-8"), CALL_SIMPLE
    if t in ("attribute", "member_expression"):
        return func_node.text.decode("utf-8"), CALL_ATTRIBUTE
    return func_node.text.decode("utf-8"), CALL_OTHER


def _walk_calls(node, module_qname, cur_scope, lang, out):
    for child in node.children:
        if child.type == lang["call_node"]:
            func = child.child_by_field_name("function")
            if func is not None:
                expr, form = _call_target(func)
                out.append(RawCall(
                    source_qname=cur_scope or module_qname,
                    target_expr=expr, call_form=form,
                    file_path="", call_line=child.start_point[0] + 1,
                ))
        if _is_scope(child.type, lang):
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                name = name_node.text.decode("utf-8")
                new_scope = qn_join(module_qname, name, cur_scope)
                _walk_calls(child, module_qname, new_scope, lang, out)
            else:
                _walk_calls(child, module_qname, cur_scope, lang, out)
        elif lang.get("detect_arrow_in_vars") and child.type == "variable_declarator":
            _maybe_arrow_scope(child, module_qname, cur_scope, lang, out)
        else:
            _walk_calls(child, module_qname, cur_scope, lang, out)


def _maybe_arrow_scope(node, module_qname, cur_scope, lang, out):
    """If a variable_declarator's value is an arrow/function expression, open a
    new scope for nested calls."""
    value = node.child_by_field_name("value")
    if value is not None and value.type in ("arrow_function", "function_expression"):
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            name = name_node.text.decode("utf-8")
            new_scope = qn_join(module_qname, name, cur_scope)
            _walk_calls(node, module_qname, new_scope, lang, out)
            return
    _walk_calls(node, module_qname, cur_scope, lang, out)


def _dotted(node) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def _extract_imports(root, module_qname, lang, lang_name: str) -> list[ImportEntry]:
    if lang_name == "python":
        return _extract_imports_python(root, module_qname, lang)
    return _extract_imports_esm(root, lang)


def _extract_imports_python(root, module_qname, lang) -> list[ImportEntry]:
    entries: list[ImportEntry] = []
    parts = module_qname.split(".") if module_qname else []
    pkg = parts[:-1] if parts else []
    for node in root.children:
        if node.type not in lang["import_nodes"]:
            continue
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


def _extract_imports_esm(root, lang) -> list[ImportEntry]:
    """Extract ES module imports: import/export statements.

    Handles:
      import {a, b} from "mod"     → imported_name per specifier
      import * as ns from "mod"    → namespace (imported_name=None)
      import d from "mod"          → default (imported_name="default")
      export {x} from "mod"        → re-export (treated as import)
    """
    entries: list[ImportEntry] = []
    for node in root.children:
        if node.type == "import_statement":
            source = _esm_source(node)
            clause = _find_child(node, "import_clause")
            if clause is None and source:
                # side-effect import: import "mod"
                entries.append(ImportEntry(source, source, None, False))
                continue
            if clause is None:
                continue
            for child in clause.children:
                if child.type == "namespace_import":
                    ident = _find_child(child, "identifier")
                    if ident:
                        entries.append(ImportEntry(ident.text.decode("utf-8"), source, None, False))
                elif child.type == "named_imports":
                    for spec in child.children:
                        if spec.type == "import_specifier":
                            name_node = spec.child_by_field_name("name")
                            alias_node = spec.child_by_field_name("alias")
                            name = name_node.text.decode("utf-8") if name_node else ""
                            local = alias_node.text.decode("utf-8") if alias_node else name
                            entries.append(ImportEntry(local, source, name, False))
                elif child.type == "identifier":
                    # default import: import foo from "mod"
                    entries.append(ImportEntry(child.text.decode("utf-8"), source, "default", False))
        elif node.type == "export_statement":
            source = _esm_source(node)
            if not source:
                continue
            # re-exports: export {x} from "mod"
            for child in node.children:
                if child.type == "export_clause":
                    for spec in child.children:
                        if spec.type == "export_specifier":
                            name_node = spec.child_by_field_name("name")
                            alias_node = spec.child_by_field_name("alias")
                            name = name_node.text.decode("utf-8") if name_node else ""
                            local = alias_node.text.decode("utf-8") if alias_node else name
                            entries.append(ImportEntry(local, source, name, False))
    return entries


_VUE_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL)


def _extract_vue_script(source: bytes) -> tuple[bytes, int]:
    """Extract concatenated <script> blocks from a .vue SFC.

    Returns (script_bytes, line_offset) where line_offset is the 0-based
    line index of the first script block's content, to be added to all
    line numbers from tree-sitter (which are 0-based).
    """
    text = source.decode("utf-8")
    match = _VUE_SCRIPT_RE.search(text)
    if match is None:
        return source, 0
    # Count newlines before the match end of the opening tag
    # <script ...>\n  ← content starts here
    tag_end = match.start(1)  # start of group 1 = start of script content
    line_offset = text[:tag_end].count("\n")
    parts = _VUE_SCRIPT_RE.findall(text)
    return "\n".join(parts).encode("utf-8"), line_offset


def _find_child(node, child_type: str):
    """Return the first child with the given type, or None."""
    for c in node.children:
        if c.type == child_type:
            return c
    return None


def _esm_source(node) -> str:
    """Extract the module source string from an import/export statement."""
    src_node = node.child_by_field_name("source")
    if src_node is None:
        return ""
    return src_node.text.decode("utf-8").strip("\"'")
