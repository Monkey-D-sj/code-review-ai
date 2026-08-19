
import fnmatch
import os
import posixpath
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from code_review_ai import qname

import tree_sitter_python as tspython
import tree_sitter_typescript as tstypescript
import tree_sitter_java as tsjava
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())
TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())
JAVA_LANGUAGE = Language(tsjava.language())
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
        "call_node": {"call"},
        "import_nodes": {
            "import_statement",
            "import_from_statement",
        },
        "class_def": "class_definition",
        "class_extends": "superclasses",
        "decorator_node": "decorator",
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
        "call_node": {"call_expression"},
        "import_nodes": {
            "import_statement",
            "export_statement",
        },
        "detect_arrow_in_vars": True,
        "class_def": "class_declaration",
        "class_extends": "extends_clause",
        "class_implements": "implements_clause",
        "decorator_node": "decorator",
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
        "call_node": {"call_expression"},
        "import_nodes": {
            "import_statement",
            "export_statement",
        },
        "detect_arrow_in_vars": True,
        "class_def": "class_declaration",
        "class_extends": "extends_clause",
        "class_implements": "implements_clause",
        "decorator_node": "decorator",
    },
    "java": {
        "def_nodes": {
            "class_declaration": "class",
            "interface_declaration": "class",
            "enum_declaration": "class",
            "record_declaration": "class",
            "method_declaration": "method",
            "constructor_declaration": "method",
        },
        "scope_nodes": {
            "class_declaration", "interface_declaration", "enum_declaration",
            "record_declaration", "method_declaration", "constructor_declaration",
        },
        "call_node": {"method_invocation", "object_creation_expression"},
        "constructor_node": "object_creation_expression",
        "constructor_type_field": "type",
        "call_name_field": "name",
        "call_object_field": "object",
        "import_nodes": {"import_declaration"},
        "class_def_nodes": {
            "class_declaration", "interface_declaration",
            "enum_declaration", "record_declaration",
        },
        "inherit_fields": {
            # class/enum/record implement via the 'interfaces' FIELD (node type
            # super_interfaces); interface extends is a bare 'extends_interfaces'
            # CHILD node (no field name) — _inherit_clause falls back to a
            # child-type lookup for it.
            "class_declaration": [("superclass", "extends"), ("interfaces", "implements")],
            "interface_declaration": [("extends_interfaces", "extends")],
            "enum_declaration": [("interfaces", "implements")],
            "record_declaration": [("interfaces", "implements")],
        },
        "decorator_node": {"marker_annotation", "annotation"},
        "annotations_in_modifiers": True,
        "mockmvc_capture": True,
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
    ".java": ("java", LANG["java"], JAVA_LANGUAGE),
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
CALL_CONSTRUCT = "construct"  # new Foo()

# ── source spans (evidence provenance) ──────────────────────────────


@dataclass(frozen=True)
class SourceSpan:
    """1-based source location of an IR record, for evidence provenance.

    file_path is filled by parse_file's batch pass (walkers run with ""), and
    the same pass shifts the line numbers by the .vue script-block offset so
    spans always point at the original file, not the extracted script."""

    file_path: str
    start_line: int
    start_col: int
    end_line: int
    end_col: int


def _span(node) -> SourceSpan:
    """SourceSpan for an AST node, from its raw tree-sitter points (0-based).

    file_path is left empty here — parse_file's batch pass stamps it and the
    .vue line offset."""
    return SourceSpan(
        file_path="",
        start_line=node.start_point[0] + 1,
        start_col=node.start_point[1] + 1,
        end_line=node.end_point[0] + 1,
        end_col=node.end_point[1] + 1,
    )


def _offset_span(span: SourceSpan | None, file_path: str,
                 line_offset: int) -> SourceSpan | None:
    """Stamp a walker-built span with its real file and shift its lines onto
    the original file when the source was extracted (e.g. .vue script block)."""
    if span is None:
        return None
    return SourceSpan(
        file_path=file_path,
        start_line=span.start_line + line_offset,
        start_col=span.start_col,
        end_line=span.end_line + line_offset,
        end_col=span.end_col,
    )


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
    decorators: list[str] = field(default_factory=list)
    mappings: list[tuple[str, str]] = field(default_factory=list)
    mockmvc_requests: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class RawCall:
    """A call-site extracted from AST, before resolution.

    source_qname — who makes the call (qualified name of enclosing function/module)
    target_expr — the raw text of the call target, e.g. ``login``, ``a.login``, ``vals[0]``
    call_form  — CALL_SIMPLE / CALL_ATTRIBUTE / CALL_OTHER
    args       — raw texts of the call's top-level arguments (``Depends(get_db)`` → ``("get_db",)``);
                 consumed by the resolver to link DI-marker args (see dependency_markers)
    """
    source_qname: str
    target_expr: str
    call_form: str
    file_path: str
    language: str = "python"
    args: tuple[str, ...] = ()
    span: SourceSpan | None = None


@dataclass
class RawInherit:
    """A class inheritance relationship extracted from AST."""
    class_qname: str   # the subclass qname
    base_expr: str     # raw base class / interface expression
    relation: str      # "extends" | "implements"
    span: SourceSpan | None = None


@dataclass
class ImportEntry:
    local_name: str
    module: str
    imported_name: str | None
    is_star: bool
    span: SourceSpan | None = None


@dataclass
class DiDecl:
    """A dependency-injection declaration extracted from AST, before resolution.

    owner_qname - who receives the dependency: the class (field injection) or
                  the constructor (constructor-injection parameters)
    dep_expr    - declared type name of the injected dependency
    annotations - decorator/annotation names on the declaration; empty for
                  unannotated constructor params (Spring's implicit injection)
    mechanism   - "field" | "constructor"

    Kept config-free at parse time; the resolver filters field declarations by
    the configured di_annotations and drops deps whose type is not a repo class.
    """
    owner_qname: str
    dep_expr: str
    annotations: list[str] = field(default_factory=list)
    mechanism: str = "field"
    span: SourceSpan | None = None


@dataclass
class ParsedFile:
    file_path: str
    module_qname: str
    language: str = "python"
    nodes: list[ParsedNode] = field(default_factory=list)
    raw_calls: list[RawCall] = field(default_factory=list)
    imports: list[ImportEntry] = field(default_factory=list)
    inherits: list[RawInherit] = field(default_factory=list)
    var_types: dict[str, dict[str, str]] = field(default_factory=dict)
    di_decls: list[DiDecl] = field(default_factory=list)
    module_all: set[str] | None = None
    default_export: str | None = None


def list_source_files(repo_path: str, extensions: list[str] | None = None) -> list[str]:
    """Return sorted relative paths of source files from git.

    extensions: list of git ls-files globs like ["*.py", "*.ts"]. Default: ["*.py"]
    Single git call with all globs as pathspecs (was one subprocess per glob).
    """
    if extensions is None:
        extensions = ["*.py"]
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", *extensions],
        cwd=repo_path, capture_output=True, text=True, check=True,
        encoding="utf-8", errors="replace",
    )
    return sorted(out.stdout.splitlines())


def filter_excluded(files: list[str], patterns: list[str]) -> list[str]:
    """Return files that do NOT match any exclude glob pattern.

    Patterns are matched against the relative path (e.g. ``tests/test_auth.py``)
    and also the bare filename. A leading ``*/`` matches any leading directory
    chain, so ``*/test*`` excludes files under any directory and ``*/alembic/*``
    excludes nested alembic trees (``app/alembic/env.py``). The raw pattern is
    matched against the full path; a ``*/``-stripped variant is additionally
    matched against the path and the bare filename so ``*/test*`` still catches
    a top-level ``test_auth.py``.
    """
    if not patterns:
        return files
    keep: list[str] = []
    for f in files:
        basename = Path(f).name
        excluded = False
        for raw in patterns:
            p = raw.lstrip("*/")
            if (fnmatch.fnmatch(f, raw) or fnmatch.fnmatch(f, p)
                    or fnmatch.fnmatch(basename, p)):
                excluded = True
                break
        if not excluded:
            keep.append(f)
    return keep


def is_test_node(file_path: str, qualified_name: str,
                 test_globs: list[str], test_names: list[str],
                 repo_root: str = "", decorators: list[str] | None = None,
                 test_decorators: list[str] | None = None) -> bool:
    """True if a node lives in a test file or has a test-style short name.

    File-path globs (``test_globs``) are matched against the **repo-relative**
    path with forward slashes (the same form ``git ls-files`` /
    ``filter_excluded`` use), not the absolute path - otherwise a repo living
    under ``.../test-platform/`` or a pytest tmp dir named ``test_impact_*``
    would tag every node as a test. A leading ``*/`` matches any leading
    directory chain; the ``*/``-stripped pattern is also matched against the
    path and the bare filename so ``*/test*`` catches a top-level
    ``test_auth.py``. Name globs (``test_names``) match the node's short name
    (e.g. ``test_*`` -> ``test_login``). Decorator globs (``test_decorators``)
    match the node's decorator names (e.g. JUnit 5 ``@Test`` -> ``"Test"``) -
    the framework-annotation channel that file/name conventions can't see.
    Either match wins.
    """
    rel = _repo_relative_path(file_path, repo_root)
    if _matches_test_globs(rel, test_globs):
        return True
    short = qname.short(qualified_name)
    if any(fnmatch.fnmatch(short, pat) for pat in test_names):
        return True
    return bool(decorators and test_decorators
                and any(fnmatch.fnmatch(dec, pat)
                        for dec in decorators for pat in test_decorators))


def _repo_relative_path(file_path: str, repo_root: str) -> str:
    """Repo-relative path with forward slashes. Empty ``repo_root`` treats
    ``file_path`` as already relative (used by unit tests)."""
    rel = os.path.relpath(file_path, repo_root) if repo_root else file_path
    return rel.replace("\\", "/")


def _matches_test_globs(rel_path: str, test_globs: list[str]) -> bool:
    basename = Path(rel_path).name
    for raw in test_globs:
        pat = raw.lstrip("*/")
        if (fnmatch.fnmatch(rel_path, raw) or fnmatch.fnmatch(rel_path, pat)
                or fnmatch.fnmatch(basename, pat)):
            return True
    return False


def _module_qname(file_path: str, repo_root: str) -> str:
    rel = Path(file_path).resolve().relative_to(Path(repo_root).resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _java_module_qname(tree, file_path: str, repo_root: str) -> str:
    """Java module qname: the package declaration when present, else path-derived.

    A Java package matches how imports reference classes (`import a.b.C` binds
    local C to class a.b::C), so the package is the module in the qname model.
    """
    root = tree.root_node
    for child in root.children:
        if child.type == "package_declaration":
            # package_declaration exposes the package name as a direct
            # scoped_identifier child (no 'name' field in this grammar).
            pkg_node = _find_child(child, "scoped_identifier")
            if pkg_node is not None:
                return pkg_node.text.decode("utf-8")
    return _java_path_module(file_path, repo_root)


def _java_path_module(file_path: str, repo_root: str) -> str:
    """Path-derived module for a Java file with no package declaration."""
    rel = Path(file_path).resolve().relative_to(Path(repo_root).resolve())
    parts = list(rel.with_suffix("").parts)
    for marker in (("src", "main", "java"), ("src", "test", "java")):
        if parts[:len(marker)] == list(marker):
            return ".".join(parts[len(marker):])
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _type_base_name(type_node) -> str | None:
    """Base type identifier of a Java type node; None for primitives and `var`.

    ``List<Owner>`` -> ``List``; ``int`` / ``boolean`` -> None (no class);
    ``var`` (Java 10 inference) -> None."""
    if type_node is None:
        return None
    if type_node.type == "type_identifier":
        text = type_node.text.decode("utf-8")
        return None if text == "var" else text
    for child in type_node.children:
        found = _type_base_name(child)
        if found is not None:
            return found
    return None


def _collect_java_var_types(root, module_qname, lang) -> dict[str, dict[str, str]]:
    """Build {method_qname: {var_name: base_type}} for Java receiver binding."""
    out: dict[str, dict[str, str]] = {}
    class_defs = lang.get("class_def_nodes")
    for child in root.children:
        if class_defs and child.type in class_defs:
            _java_class_var_types(child, module_qname, lang, out)
    return out


def _java_class_var_types(node, module_qname, lang, out) -> None:
    """Collect a class's fields and per-method params/locals into out.

    Fields and methods live inside the class's ``body`` member
    (class_body/interface_body/enum_body), not as direct children."""
    body = node.child_by_field_name("body")
    members = body.children if body is not None else []
    fields: dict[str, str] = {}
    for member in members:
        if member.type != "field_declaration":
            continue
        type_name = _type_base_name(member.child_by_field_name("type"))
        if type_name is None:
            continue
        for decl in member.children:
            if decl.type == "variable_declarator":
                name_node = decl.child_by_field_name("name")
                if name_node is not None:
                    fields[name_node.text.decode("utf-8")] = type_name
    cls_name_node = node.child_by_field_name("name")
    cls_qname = (qname.join(module_qname, cls_name_node.text.decode("utf-8"))
                 if cls_name_node is not None else None)
    for member in members:
        if member.type not in ("method_declaration", "constructor_declaration"):
            continue
        method_name = member.child_by_field_name("name")
        if method_name is None:
            continue
        method_qn = qname.join(module_qname, method_name.text.decode("utf-8"), cls_qname)
        scope: dict[str, str] = dict(fields)
        _java_params(member, scope)
        _java_locals(member, scope)
        out[method_qn] = scope


def _java_params(node, scope) -> None:
    params_node = node.child_by_field_name("parameters")
    if params_node is None:
        return
    for param in params_node.children:
        if param.type != "formal_parameter":
            continue
        type_name = _type_base_name(param.child_by_field_name("type"))
        name_node = param.child_by_field_name("name")
        if type_name is not None and name_node is not None:
            scope[name_node.text.decode("utf-8")] = type_name


def _java_locals(node, scope) -> None:
    """Collect local_variable_declaration names in a method body (recursive)."""
    for child in node.children:
        if child.type == "local_variable_declaration":
            type_name = _type_base_name(child.child_by_field_name("type"))
            if type_name is None:
                continue
            for decl in child.children:
                if decl.type == "variable_declarator":
                    name_node = decl.child_by_field_name("name")
                    if name_node is not None:
                        scope[name_node.text.decode("utf-8")] = type_name
        _java_locals(child, scope)


# Receiver declared-type binding (PY-M12) collects annotated variable types for
# Python and TypeScript so the resolver can bind `w.run()` when `w: Widget`.
# Only simple identifier types are recorded — union/generic/subscript/string
# annotations and untyped variables are skipped so the resolver never guesses a
# target (those belong to Phase 4 Slice 6).
_PY_SIMPLE_TYPE_NODES = ("identifier",)
_TS_SIMPLE_TYPE_NODES = ("type_identifier",)


def _py_type_text(type_node) -> str | None:
    """Declared type text when ``type_node`` holds a simple identifier type.

    Python's grammar wraps the annotation in a ``type`` node whose child is the
    ``identifier`` (or a ``list``/``string``/``union_type`` — those return
    None and the variable stays untyped).
    """
    if type_node is None:
        return None
    inner = type_node if type_node.type in _PY_SIMPLE_TYPE_NODES else None
    if inner is None:
        inner = next((child for child in type_node.children
                      if child.type in _PY_SIMPLE_TYPE_NODES), None)
    return inner.text.decode("utf-8") if inner is not None else None


def _py_assignment_type(node) -> tuple[str, str] | None:
    """From an ``expression_statement``, (name, type_text) for an annotated
    assignment ``x: T`` / ``x: T = v`` with a plain identifier left side and a
    simple identifier type. None for attribute/union/subscript targets."""
    if node.type != "expression_statement":
        return None
    assign = next((child for child in node.children
                   if child.type == "assignment"), None)
    if assign is None:
        return None
    left = assign.child_by_field_name("left")
    type_node = assign.child_by_field_name("type")
    if left is None or left.type != "identifier":
        return None
    type_text = _py_type_text(type_node)
    return (left.text.decode("utf-8"), type_text) if type_text else None


def _py_annotated_in_children(node, scope: dict[str, str]) -> None:
    """Record annotated assignments declared directly under ``node``."""
    for child in node.children:
        anno = _py_assignment_type(child)
        if anno and anno[0] not in scope:
            scope[anno[0]] = anno[1]


def _py_param_name(param):
    """A ``typed_parameter``'s name: the ``name`` field, or — for grammar
    builds that only expose it as the first ``identifier`` child — that child."""
    name_node = param.child_by_field_name("name")
    if name_node is not None:
        return name_node
    return next((child for child in param.children
                 if child.type == "identifier"), None)


def _py_typed_parameters(params, scope: dict[str, str]) -> None:
    """Record ``w: Widget``-style parameters (skip keyword-only/star forms)."""
    if params is None:
        return
    for param in params.children:
        if param.type != "typed_parameter":
            continue
        name_node = _py_param_name(param)
        type_text = _py_type_text(param.child_by_field_name("type"))
        if name_node is not None and type_text:
            scope[name_node.text.decode("utf-8")] = type_text


def _collect_python_var_types(root, module_qname, lang) -> dict[str, dict[str, str]]:
    """Build {function/module_qname: {var_name: declared_type}} for Python.

    Module-level scope is keyed by the module qname; function scopes by their
    qname (matching ``RawCall.source_qname``). Method scopes carry the class's
    annotated fields as ``self.<field>`` so ``self.w.run()`` binds via the
    field's declared type.
    """
    out: dict[str, dict[str, str]] = {}
    module_scope: dict[str, str] = {}
    _py_annotated_in_children(root, module_scope)
    if module_scope:
        out[module_qname] = module_scope
    for child in root.children:
        if child.type == "class_definition":
            _py_class_var_types(child, module_qname, None, lang, out)
        elif child.type == "function_definition":
            _py_function_var_types(child, module_qname, None, lang, out)
    return out


def _py_function_var_types(fn, module_qname, scope_qname, lang, out) -> None:
    """Record a function's typed params + direct-body locals; recurse into
    nested classes/functions."""
    name_node = fn.child_by_field_name("name")
    if name_node is None:
        return
    qn = qname.join(module_qname, name_node.text.decode("utf-8"), scope_qname)
    scope: dict[str, str] = {}
    _py_typed_parameters(fn.child_by_field_name("parameters"), scope)
    body = fn.child_by_field_name("body")
    if body is not None:
        _py_annotated_in_children(body, scope)
    if scope:
        out[qn] = scope
    if body is not None:
        for child in body.children:
            if child.type == "class_definition":
                _py_class_var_types(child, module_qname, qn, lang, out)
            elif child.type == "function_definition":
                _py_function_var_types(child, module_qname, qn, lang, out)


def _py_class_var_types(cls, module_qname, scope_qname, lang, out) -> None:
    """Record a class's annotated fields and each method's typed scope."""
    name_node = cls.child_by_field_name("name")
    if name_node is None:
        return
    cls_qn = qname.join(module_qname, name_node.text.decode("utf-8"), scope_qname)
    fields: dict[str, str] = {}
    body = cls.child_by_field_name("body")
    if body is not None:
        _py_annotated_in_children(body, fields)
    if fields:
        out[cls_qn] = dict(fields)
    if body is not None:
        for member in body.children:
            if member.type == "function_definition":
                _py_method_var_types(member, module_qname, cls_qn, fields,
                                     lang, out)
            elif member.type == "class_definition":
                _py_class_var_types(member, module_qname, cls_qn, lang, out)


def _py_method_var_types(fn, module_qname, cls_qn, fields, lang, out) -> None:
    """Record a method's scope: fields as ``self.<field>`` + typed params and
    direct-body locals."""
    name_node = fn.child_by_field_name("name")
    if name_node is None:
        return
    qn = qname.join(module_qname, name_node.text.decode("utf-8"), cls_qn)
    scope: dict[str, str] = {f"self.{name}": type_text
                             for name, type_text in fields.items()}
    _py_typed_parameters(fn.child_by_field_name("parameters"), scope)
    body = fn.child_by_field_name("body")
    if body is not None:
        _py_annotated_in_children(body, scope)
    if scope:
        out[qn] = scope
    if body is not None:
        for child in body.children:
            if child.type == "class_definition":
                _py_class_var_types(child, module_qname, qn, lang, out)
            elif child.type == "function_definition":
                _py_function_var_types(child, module_qname, qn, lang, out)


def _ts_type_text(node) -> str | None:
    """Declared type text for a TS param/declarator/field carrying a
    ``type_annotation`` whose inner type is a simple ``type_identifier``."""
    ta = next((child for child in node.children
               if child.type == "type_annotation"), None)
    if ta is None:
        return None
    inner = next((child for child in ta.children
                  if child.type in _TS_SIMPLE_TYPE_NODES), None)
    return inner.text.decode("utf-8") if inner is not None else None


def _ts_param_name(param):
    """A ``required_parameter``/``optional_parameter``'s name: the ``name``
    field, or — for grammar builds that only expose it as the first
    ``identifier`` child — that child."""
    name_node = param.child_by_field_name("name")
    if name_node is not None:
        return name_node
    return next((child for child in param.children
                 if child.type == "identifier"), None)


def _ts_typed_parameters(fn, scope: dict[str, str]) -> None:
    params = fn.child_by_field_name("parameters")
    if params is None:
        return
    for param in params.children:
        if param.type not in ("required_parameter", "optional_parameter"):
            continue
        name_node = _ts_param_name(param)
        type_text = _ts_type_text(param)
        if name_node is not None and type_text:
            scope[name_node.text.decode("utf-8")] = type_text


def _ts_locals(fn, scope: dict[str, str]) -> None:
    """Record typed const/let declarations in the function body's direct block."""
    block = fn.child_by_field_name("body")
    if block is None:
        return
    for child in block.children:
        if child.type != "lexical_declaration":
            continue
        for decl in child.children:
            if decl.type != "variable_declarator":
                continue
            name_node = decl.child_by_field_name("name")
            type_text = _ts_type_text(decl)
            if name_node is not None and type_text:
                scope[name_node.text.decode("utf-8")] = type_text


def _ts_field_types(body, fields: dict[str, str]) -> None:
    """Record typed class fields / interface property signatures."""
    for member in body.children:
        if member.type not in ("public_field_definition", "property_signature"):
            continue
        name_node = member.child_by_field_name("name")
        type_text = _ts_type_text(member)
        if name_node is not None and type_text:
            fields[name_node.text.decode("utf-8")] = type_text


def _collect_ts_var_types(root, module_qname, lang) -> dict[str, dict[str, str]]:
    """Build {function/module_qname: {var_name: declared_type}} for TS/JS.

    JavaScript has no type annotations, so this yields empty scopes for it —
    the honest no-op. Method scopes carry fields as ``this.<field>``.
    """
    out: dict[str, dict[str, str]] = {}
    _ts_walk(root, module_qname, None, None, lang, out)
    return out


def _ts_walk(node, module_qname, scope_qname, parent_kind, lang, out) -> None:
    """Walk def nodes registering typed scopes; recurse for nested defs."""
    for child in node.children:
        child_type = child.type
        if child_type == "function_declaration":
            _ts_function(child, module_qname, scope_qname, lang, out)
        elif child_type == "class_declaration":
            _ts_class(child, module_qname, scope_qname, lang, out)
        elif child_type == "variable_declarator":
            _ts_arrow(child, module_qname, scope_qname, parent_kind, lang, out)
        elif child.children:
            _ts_walk(child, module_qname, scope_qname, parent_kind, lang, out)


def _ts_function(fn, module_qname, scope_qname, lang, out) -> None:
    name_node = fn.child_by_field_name("name")
    if name_node is None:
        return
    qn = qname.join(module_qname, name_node.text.decode("utf-8"), scope_qname)
    scope: dict[str, str] = {}
    _ts_typed_parameters(fn, scope)
    _ts_locals(fn, scope)
    if scope:
        out[qn] = scope
    _ts_walk(fn, module_qname, qn, "function", lang, out)


def _ts_class(cls, module_qname, scope_qname, lang, out) -> None:
    name_node = cls.child_by_field_name("name")
    if name_node is None:
        return
    cls_qn = qname.join(module_qname, name_node.text.decode("utf-8"), scope_qname)
    fields: dict[str, str] = {}
    body = cls.child_by_field_name("body")
    if body is not None:
        _ts_field_types(body, fields)
    if fields:
        out[cls_qn] = dict(fields)
    if body is not None:
        for member in body.children:
            if member.type == "method_definition":
                _ts_method(member, module_qname, cls_qn, fields, lang, out)
            elif member.type == "class_declaration":
                _ts_class(member, module_qname, cls_qn, lang, out)


def _ts_method(method, module_qname, cls_qn, fields, lang, out) -> None:
    name_node = method.child_by_field_name("name")
    if name_node is None:
        return
    qn = qname.join(module_qname, name_node.text.decode("utf-8"), cls_qn)
    scope: dict[str, str] = {f"this.{name}": type_text
                             for name, type_text in fields.items()}
    _ts_typed_parameters(method, scope)
    _ts_locals(method, scope)
    if scope:
        out[qn] = scope
    _ts_walk(method, module_qname, qn, "method", lang, out)


def _ts_arrow(decl, module_qname, scope_qname, parent_kind, lang, out) -> None:
    """Record a const/let x = (…) => {} / function() {} arrow function's scope."""
    value = decl.child_by_field_name("value")
    if value is None or value.type not in ("arrow_function", "function_expression"):
        _ts_walk(decl, module_qname, scope_qname, parent_kind, lang, out)
        return
    name_node = decl.child_by_field_name("name")
    if name_node is None:
        return
    qn = qname.join(module_qname, name_node.text.decode("utf-8"), scope_qname)
    scope: dict[str, str] = {}
    _ts_typed_parameters(value, scope)
    _ts_locals(value, scope)
    if scope:
        out[qn] = scope
    _ts_walk(value, module_qname, qn, "function", lang, out)


def _collect_java_di(root, module_qname, lang) -> list[DiDecl]:
    """Java DI declarations per file: annotated fields + constructor params.

    Field declarations are collected only when annotated (which annotation
    qualifies is the resolver's config call, so parse stays config-free);
    constructor params are collected unconditionally - a constructor holding
    a repo-typed dependency is a real type dependency regardless of framework
    (Spring injects single-constructor params even without @Autowired)."""
    out: list[DiDecl] = []
    class_defs = lang.get("class_def_nodes")
    for child in root.children:
        if class_defs and child.type in class_defs:
            _java_class_di(child, module_qname, lang, out)
    return out


def _java_class_di(node, module_qname, lang, out) -> None:
    """Collect one class's DI declarations: annotated fields (owner = class)
    and constructor parameters (owner = the constructor qname, so DI edges
    chain off the `new Foo()` -> constructor edge)."""
    body = node.child_by_field_name("body")
    members = body.children if body is not None else []
    cls_name_node = node.child_by_field_name("name")
    if cls_name_node is None:
        return
    cls_qname = qname.join(module_qname, cls_name_node.text.decode("utf-8"))
    deco_types = _decorator_types(lang)
    for member in members:
        if member.type == "field_declaration":
            annotations = [_decorator_name(a)
                           for a in _annotation_children(member, deco_types, lang)]
            if not annotations:
                continue  # unannotated fields are not injection points
            type_name = _type_base_name(member.child_by_field_name("type"))
            if type_name is not None:
                out.append(DiDecl(cls_qname, type_name, annotations, "field",
                                  span=_span(member)))
        elif member.type == "constructor_declaration":
            _java_ctor_di(member, module_qname, cls_qname, lang, deco_types, out)


def _java_ctor_di(member, module_qname, cls_qname, lang, deco_types,
                  out) -> None:
    """DiDecls for one constructor's parameters (owner = constructor qname)."""
    ctor_name = member.child_by_field_name("name")
    if ctor_name is None:
        return
    ctor_qname = qname.join(module_qname, ctor_name.text.decode("utf-8"), cls_qname)
    params_node = member.child_by_field_name("parameters")
    if params_node is None:
        return
    for param in params_node.children:
        if param.type != "formal_parameter":
            continue
        type_name = _type_base_name(param.child_by_field_name("type"))
        if type_name is None:
            continue  # primitive / var - never a repo class
        annotations = [_decorator_name(a)
                       for a in _annotation_children(param, deco_types, lang)]
        out.append(DiDecl(ctor_qname, type_name, annotations, "constructor",
                          span=_span(param)))


def _collect_java_mappings(root, module_qname, lang) -> dict[str, list[tuple[str, str]]]:
    """Class-prefixed Spring mappings per method qname.

    PetController uses a class-level @RequestMapping(\"/owners/{ownerId}\") plus
    method-level @GetMapping(\"/pets/new\"); the full route is the concatenation.
    Computed in a dedicated pass because it needs the enclosing class context."""
    out: dict[str, list[tuple[str, str]]] = {}
    class_defs = lang.get("class_def_nodes")
    for child in root.children:
        if class_defs and child.type in class_defs:
            _java_class_mappings(child, module_qname, lang, out)
    return out


def _java_class_mappings(node, module_qname, lang, out) -> None:
    body = node.child_by_field_name("body")
    members = body.children if body is not None else []
    prefix_paths: list[str] = []
    for ann in _annotation_children(node, _decorator_types(lang), lang):
        if _decorator_name(ann) == "RequestMapping":
            prefix_paths.extend(_annotation_strings(ann))
    cls_name_node = node.child_by_field_name("name")
    cls_qname = (qname.join(module_qname, cls_name_node.text.decode("utf-8"))
                 if cls_name_node is not None else None)
    for member in members:
        if member.type not in ("method_declaration", "constructor_declaration"):
            continue
        method_name = member.child_by_field_name("name")
        if method_name is None:
            continue
        method_qn = qname.join(module_qname, method_name.text.decode("utf-8"), cls_qname)
        mappings = _java_mappings(member, lang)
        if prefix_paths:
            mappings = [(method, _join_mapping_path(prefix, path))
                        for method, path in mappings
                        for prefix in prefix_paths]
        if mappings:
            out[method_qn] = mappings


def _join_mapping_path(prefix: str, path: str) -> str:
    if not prefix or prefix == "/":
        return path
    if path.startswith("/"):
        return prefix.rstrip("/") + path
    return prefix.rstrip("/") + "/" + path


def _sig(source: bytes, node) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body else node.end_byte
    return source[node.start_byte:end].decode("utf-8").strip()


def _decorator_types(lang) -> set[str]:
    """decorator_node as a set; a single-string config (Python/TS/JS) works too."""
    node_type = lang.get("decorator_node")
    if not node_type:
        return set()
    if isinstance(node_type, (set, tuple, frozenset)):
        return set(node_type)
    return {node_type}


def _annotation_children(node, deco_types: set[str], lang) -> list:
    """Annotation nodes decorating a def: direct children of the given types,
    plus those nested in a ``modifiers`` child (tree-sitter-java nests Java
    annotations there — not siblings, not direct children of the def)."""
    found = [child for child in node.children if child.type in deco_types]
    if lang.get("annotations_in_modifiers"):
        for child in node.children:
            if child.type == "modifiers":
                found.extend(c for c in child.children if c.type in deco_types)
    return found


def _decorator_names(node, lang) -> list[str]:
    """Collect decorator names from a node's annotation/decorator children.
    A lang without ``decorator_node`` configured is a no-op."""
    deco_types = _decorator_types(lang)
    if not deco_types:
        return []
    return [_decorator_name(c) for c in _annotation_children(node, deco_types, lang)]


def _decorator_name(deco_node) -> str:
    """Extract the decorator's name: '@app.route("/")' -> 'app.route',
    '@staticmethod' -> 'staticmethod'. A call decorator strips its arguments to
    the callee (the same field _call_target reads), so entry_decorators globs
    match on the name a user would write."""
    for child in deco_node.children:
        if child.type in ("identifier", "attribute", "member_expression",
                          "scoped_identifier", "type_identifier"):
            return child.text.decode("utf-8")
        if child.type in ("call", "call_expression"):
            func = child.child_by_field_name("function")
            if func is not None:
                return func.text.decode("utf-8")
    return ""


_MAPPING_METHODS = {
    "RequestMapping": "ANY",
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
}


def _java_mappings(node, lang) -> list[tuple[str, str]]:
    """Extract (http_method, path) pairs from Spring mapping annotations.

    Reads a def's annotation nodes (including those in a modifiers child).
    RequestMapping without an explicit method element maps to 'ANY'."""
    out: list[tuple[str, str]] = []
    for ann in _annotation_children(node, _decorator_types(lang), lang):
        method = _MAPPING_METHODS.get(_decorator_name(ann))
        if method is None:
            continue
        paths = _annotation_strings(ann)
        if method == "ANY":
            method = _request_mapping_method(ann) or "ANY"
        for path in paths:
            out.append((method, path))
    return out


def _annotation_strings(node) -> list[str]:
    """Collect quoted string values in an annotation's arguments (descendants),
    e.g. @GetMapping(\"/owners\") -> ['/owners']; { \"/a\", \"/b\" } -> both."""
    return [s.text.decode("utf-8").strip("\"'")
            for s in _collect_by_type(node, "string_literal")]


def _collect_by_type(node, node_type: str) -> list:
    out = []
    for child in node.children:
        if child.type == node_type:
            out.append(child)
        out.extend(_collect_by_type(child, node_type))
    return out


def _request_mapping_method(node) -> str | None:
    """Extract the HTTP method from @RequestMapping(method=RequestMethod.GET)."""
    for access in _collect_by_type(node, "field_access"):
        text = access.text.decode("utf-8")
        if text.startswith("RequestMethod."):
            return text.split(".")[-1].upper()
    return None


def _walk_defs_typed(node, source, module_qname, scope_qname, parent_kind, lang, output):
    """Walk AST for def nodes, capturing decorators.

    Decorators precede their def as siblings (Python wraps decorated defs in a
    ``decorated_definition`` container whose ``decorator`` children sit next to
    the inner def; TS/JS put a ``decorator`` node directly before a class or
    method, or as a child of it). Both shapes are handled: decorator siblings
    accumulate into ``pending`` and are consumed by the next def node, and a
    def node's own direct ``decorator`` children are collected too. A lang
    without ``decorator_node`` configured is a no-op.
    """
    deco_types = _decorator_types(lang)
    pending: list[str] = []
    for child in node.children:
        t = child.type
        if deco_types and t in deco_types:
            pending.append(_decorator_name(child))
            continue
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
            qn = qname.join(module_qname, name, scope_qname)
            kind = lang["def_nodes"][t]
            if kind == "function" and parent_kind == "class":
                kind = "method"
            decorators = list(pending)
            if deco_types:
                decorators.extend(_decorator_names(child, lang))
            output.append(ParsedNode(
                qualified_name=qn, kind=kind, file_path="",
                start_line=child.start_point[0] + 1, end_line=child.end_point[0] + 1,
                signature=_sig(source, child), parent_qname=scope_qname,
                decorators=decorators,
            ))
            pending = []
            _walk_defs_typed(child, source, module_qname, qn, kind, lang, output)
        elif lang.get("detect_arrow_in_vars") and t == "variable_declarator":
            pending = []
            _maybe_arrow_def(child, source, module_qname, scope_qname, parent_kind, lang, output)
        else:
            if child.children:
                pending = []
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
    qn = qname.join(module_qname, name, scope_qname)
    kind = "method" if parent_kind == "class" else "function"
    output.append(ParsedNode(
        qualified_name=qn, kind=kind, file_path="",
        start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
        signature=_sig(source, value), parent_qname=scope_qname,
    ))
    _walk_defs_typed(value, source, module_qname, qn, kind, lang, output)


_INHERIT_BASE_TYPES = ("identifier", "type_identifier", "property_identifier",
                       "attribute", "member_expression")


def _inherit_clause(node, clause_name: str):
    """Return an inheritance clause: the named field when present, else the
    child node of that type (Java interface 'extends' is a bare child node)."""
    return node.child_by_field_name(clause_name) or _find_child(node, clause_name)


def _inherit_bases(clause):
    """Yield base type nodes from an inheritance clause, descending type_list
    (Java wraps interface extends and class implements in a type_list)."""
    stack = list(clause.children)
    while stack:
        node = stack.pop()
        if node.type == "type_list":
            stack.extend(node.children)
        else:
            yield node


def _walk_inherits(node, module_qname, lang, out: list):
    """Walk AST for class inheritance: extends / implements clauses.

    Python/TS use a single ``class_def`` + ``class_extends``/``class_implements``
    field-name pair; Java declares per-node-type ``inherit_fields`` (e.g. an
    interface's supertypes live under ``extends_interfaces``, a class's under
    ``superclass``/``super_interfaces``)."""
    class_defs = lang.get("class_def_nodes")
    for child in node.children:
        t = child.type
        is_class_def = (t in class_defs) if class_defs else (t == lang.get("class_def"))
        if is_class_def:
            cls_name_node = child.child_by_field_name("name")
            if cls_name_node is None:
                continue
            cls_qname = qname.join(module_qname, cls_name_node.text.decode("utf-8"))
            if lang.get("inherit_fields"):
                pairs = lang["inherit_fields"].get(t, ())
            else:
                pairs = []
                for field_key, rel in (("class_extends", "extends"),
                                       ("class_implements", "implements")):
                    field_name = lang.get(field_key)
                    if field_name:
                        pairs.append((field_name, rel))
            for field_name, rel in pairs:
                clause = _inherit_clause(child, field_name)
                if clause is None:
                    continue
                for base in _inherit_bases(clause):
                    if base.type in _INHERIT_BASE_TYPES:
                        out.append(RawInherit(
                            class_qname=cls_qname,
                            base_expr=base.text.decode("utf-8"),
                            relation=rel,
                            span=_span(base),
                        ))
        _walk_inherits(child, module_qname, lang, out)


def parse_file(file_path: str, repo_root: str, lang: dict | None = None) -> ParsedFile:
    explicit_lang = lang is not None  # caller-passed dict wins over any .vue lang
    if lang is None:
        lang_name, lang, ts_lang = _lang_for_path(file_path)
    else:
        lang_name = "python"
        ts_lang = PY_LANGUAGE
    source = Path(file_path).read_bytes()
    line_offset = 0
    original_line_count = 0
    if file_path.endswith(".vue"):
        original_line_count = source.count(b"\n") + 1
        source, line_offset, vue_lang = _extract_vue_script(source)
        if vue_lang and not explicit_lang:
            # pick the JS/TS dialect from the block's `lang` attribute
            # (plain <script> is Vue's JS default); ts_lang stays the base
            # TypeScript grammar — JS files already share it.
            lang_name = vue_lang
            lang = LANG[vue_lang]
    tree = _parser(ts_lang).parse(source)
    root = tree.root_node
    if lang_name == "java":
        module_qname = _java_module_qname(tree, file_path, repo_root)
    else:
        module_qname = _module_qname(file_path, repo_root)

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
    mockmvc_requests: list[tuple[str, tuple[str, str]]] = []
    _walk_calls(root, module_qname, None, lang, pf.raw_calls, mockmvc_requests)
    pf.imports = _extract_imports(root, module_qname, lang, lang_name,
                                  file_path, repo_root)
    if lang_name == "python":
        pf.module_all = _extract_module_all(root)
    elif lang_name in ("typescript", "javascript"):
        pf.default_export = (_extract_esm_default_export(root, module_qname)
                             or _extract_cjs_default_export(root, module_qname))
    # handle inheritance
    _walk_inherits(root, module_qname, lang, pf.inherits)
    if lang_name == "java":
        pf.var_types = _collect_java_var_types(root, module_qname, lang)
        pf.di_decls = _collect_java_di(root, module_qname, lang)
        mappings = _collect_java_mappings(root, module_qname, lang)
        for n in pf.nodes:
            if n.qualified_name in mappings:
                n.mappings = mappings[n.qualified_name]
    elif lang_name == "python":
        pf.var_types = _collect_python_var_types(root, module_qname, lang)
    elif lang_name == "typescript":
        pf.var_types = _collect_ts_var_types(root, module_qname, lang)

    # Dedup nodes — keep first occurrence of each qualified_name (inner
    # functions with the same name can appear in nested scopes).
    seen_qns: set[str] = set()
    deduped: list[ParsedNode] = []
    for n in pf.nodes:
        if n.qualified_name not in seen_qns:
            seen_qns.add(n.qualified_name)
            deduped.append(n)
    pf.nodes = deduped

    # Batch-fill file_path, language, and apply line offset — constant across one file
    for n in pf.nodes:
        n.file_path = file_path
        n.language = lang_name
        if line_offset and n.kind != "module":
            n.start_line += line_offset
            n.end_line += line_offset
    for c in pf.raw_calls:
        c.file_path = file_path
        c.language = lang_name
        c.span = _offset_span(c.span, file_path, line_offset)
    # IR records that carry spans but no per-record file_path (inherits,
    # imports, DI decls) get their file + line offset stamped here too, so
    # evidence can always point at the original file.
    for rec in (*pf.inherits, *pf.imports, *pf.di_decls):
        rec.span = _offset_span(rec.span, file_path, line_offset)

    # Attach captured MockMvc requests to the methods that made them
    mockmvc_map: dict[str, list[tuple[str, str]]] = {}
    for scope_qname, request in mockmvc_requests:
        mockmvc_map.setdefault(scope_qname, []).append(request)
    for n in pf.nodes:
        if n.qualified_name in mockmvc_map:
            n.mockmvc_requests = mockmvc_map[n.qualified_name]

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


_ARG_PUNCT = {"(", ")", "[", "]", ",", "comment"}


def _call_args(node, lang) -> tuple[str, ...]:
    """Raw texts of a call's top-level arguments, or () when none.

    TS/JS/Java name the container as an ``arguments`` field; Python's is a bare
    ``argument_list`` child node. Both hold the args plus punctuation/separator
    nodes, which are filtered out. Texts are kept verbatim (identifiers, keyword
    args like ``use_cache=False``, nested calls, literals) — the resolver decides
    which are dependency references.
    """
    args_node = node.child_by_field_name("arguments")
    if args_node is None:
        args_node = next((c for c in node.children
                          if c.type == "argument_list"), None)
    if args_node is None:
        return ()
    return tuple(child.text.decode("utf-8")
                 for child in args_node.children
                 if child.type not in _ARG_PUNCT)


def _call_target_for(node, lang) -> tuple[str | None, str | None]:
    """Language-aware call-target extraction.

    Returns (target_expr, call_form); (None, None) when no usable target.
    Java's method_invocation splits receiver/name into separate fields; its
    object_creation_expression ('new Foo()') carries the type in a 'type' field.
    """
    if lang.get("constructor_node") and node.type == lang["constructor_node"]:
        ctor = node.child_by_field_name(lang.get("constructor_type_field", "type"))
        if ctor is None:
            return None, None
        return ctor.text.decode("utf-8"), CALL_CONSTRUCT
    name_field = lang.get("call_name_field", "function")
    func = node.child_by_field_name(name_field)
    if func is None:
        return None, None
    obj_field = lang.get("call_object_field")
    if obj_field:
        obj = node.child_by_field_name(obj_field)
        if obj is not None:
            return f"{obj.text.decode('utf-8')}.{func.text.decode('utf-8')}", CALL_ATTRIBUTE
    return _call_target(func)


_MOCKMVC_BUILDERS = {"get", "post", "put", "delete", "patch", "head", "options"}


def _mockmvc_request(node, lang) -> tuple[str, str] | None:
    """From a mockMvc.perform(...) method_invocation, return (HTTP_METHOD, path)
    or None when the call isn't a MockMvc request."""
    obj = node.child_by_field_name(lang.get("call_object_field", "object"))
    if obj is None or obj.text.decode("utf-8") != "mockMvc":
        return None
    name_node = node.child_by_field_name(lang.get("call_name_field", "name"))
    if name_node is None or name_node.text.decode("utf-8") != "perform":
        return None
    args = node.child_by_field_name("arguments")
    builder = _find_builder_call(args) if args is not None else None
    if builder is None:
        return None
    builder_name = builder.child_by_field_name("name").text.decode("utf-8")
    path = _first_string_literal(builder)
    if path is None:
        return None
    return builder_name.upper(), path


def _find_builder_call(node) -> object | None:
    """First method_invocation in the subtree whose name is a MockMvc request
    builder (the root of a get/post/... chain, possibly wrapped in .param())."""
    if node is None:
        return None
    if node.type == "method_invocation":
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.text.decode("utf-8") in _MOCKMVC_BUILDERS:
            return node
    for child in node.children:
        found = _find_builder_call(child)
        if found is not None:
            return found
    return None


def _first_string_literal(node) -> str | None:
    for literal in _collect_by_type(node, "string_literal"):
        return literal.text.decode("utf-8").strip("\"'")
    return None


def _walk_calls(node, module_qname, cur_scope, lang, out,
                mockmvc_requests: list | None = None):
    for child in node.children:
        if child.type in lang["call_node"]:
            expr, form = _call_target_for(child, lang)
            if expr is not None:
                out.append(RawCall(
                    source_qname=cur_scope or module_qname,
                    target_expr=expr, call_form=form,
                    file_path="",
                    args=_call_args(child, lang),
                    span=_span(child),
                ))
            if mockmvc_requests is not None and lang.get("mockmvc_capture"):
                request = _mockmvc_request(child, lang)
                if request is not None:
                    mockmvc_requests.append((cur_scope or module_qname, request))
        if _is_scope(child.type, lang):
            name_node = child.child_by_field_name("name")
            if name_node is not None:
                name = name_node.text.decode("utf-8")
                new_scope = qname.join(module_qname, name, cur_scope)
                _walk_calls(child, module_qname, new_scope, lang, out, mockmvc_requests)
            else:
                _walk_calls(child, module_qname, cur_scope, lang, out, mockmvc_requests)
        elif lang.get("detect_arrow_in_vars") and child.type == "variable_declarator":
            _maybe_arrow_scope(child, module_qname, cur_scope, lang, out, mockmvc_requests)
        else:
            _walk_calls(child, module_qname, cur_scope, lang, out, mockmvc_requests)


def _maybe_arrow_scope(node, module_qname, cur_scope, lang, out,
                       mockmvc_requests: list | None = None):
    """If a variable_declarator's value is an arrow/function expression, open a
    new scope for nested calls."""
    value = node.child_by_field_name("value")
    if value is not None and value.type in ("arrow_function", "function_expression"):
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            name = name_node.text.decode("utf-8")
            new_scope = qname.join(module_qname, name, cur_scope)
            _walk_calls(node, module_qname, new_scope, lang, out, mockmvc_requests)
            return
    _walk_calls(node, module_qname, cur_scope, lang, out, mockmvc_requests)


def _dotted(node) -> str:
    return node.text.decode("utf-8") if node is not None else ""


def _extract_imports(root, module_qname, lang, lang_name: str,
                     file_path: str, repo_root: str = "") -> list[ImportEntry]:
    if lang_name == "python":
        return _extract_imports_python(root, module_qname, lang, file_path)
    if lang_name == "java":
        return _extract_imports_java(root, lang)
    entries = _extract_imports_esm(root, lang, file_path, repo_root, module_qname)
    if lang_name in ("javascript", "typescript"):
        # CommonJS require/exports coexist with ESM syntax in .js/.mjs/.cjs
        # and in TS compiled with module=commonjs — merge both channels.
        entries = [*entries,
                   *_extract_imports_cjs(root, module_qname, file_path, repo_root)]
    return entries


def _extract_imports_java(root, lang) -> list[ImportEntry]:
    """Extract Java imports: regular, wildcard, and static forms.

      import a.b.C;          -> local C  from module a.b (imported_name=C)
      import a.b.*;          -> star import of module a.b
      import static a.b.C.m; -> local m from class a.b::C (imported_name=m)
    """
    entries: list[ImportEntry] = []
    for node in root.children:
        if node.type not in lang["import_nodes"]:
            continue
        # import_declaration exposes the name as a direct scoped_identifier
        # child (no 'name' field in this grammar).
        scoped = _find_child(node, "scoped_identifier")
        if scoped is None:
            continue
        full = scoped.text.decode("utf-8")
        child_types = {ch.type for ch in node.children}
        if "static" in child_types:
            module, _, member = full.rpartition(".")
            pkg, _, cls = module.rpartition(".")
            class_qn = f"{pkg}::{cls}" if cls else module
            entries.append(ImportEntry(member, class_qn, member, False,
                                       span=_span(node)))
        elif "asterisk" in child_types:
            entries.append(ImportEntry("*", full, None, True, span=_span(node)))
        else:
            pkg, _, cls = full.rpartition(".")
            entries.append(ImportEntry(cls, pkg, cls, False, span=_span(node)))
    return entries


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
            for child in node.children:
                if child.type == "dotted_name":
                    mod = child.text.decode("utf-8")
                    local = mod.split(".")[0]
                    entries.append(ImportEntry(local, mod, None, False,
                                               span=_span(node)))
                elif child.type == "aliased_import":
                    name = child.child_by_field_name("name").text.decode("utf-8")
                    alias = child.child_by_field_name("alias").text.decode("utf-8")
                    entries.append(ImportEntry(alias, name, None, False,
                                               span=_span(node)))
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
                    entries.append(ImportEntry(name, module, name, False,
                                               span=_span(node)))
                elif c.type == "aliased_import":
                    name = c.child_by_field_name("name").text.decode("utf-8")
                    alias = c.child_by_field_name("alias").text.decode("utf-8")
                    entries.append(ImportEntry(alias, module, name, False,
                                               span=_span(node)))
                elif c.type == "wildcard_import":
                    entries.append(ImportEntry("*", module, None, True,
                                               span=_span(node)))
    return entries


def _extract_module_all(root) -> set[str] | None:
    """A module's ``__all__`` literal-list, or None when not statically declared.

    Only a top-level ``__all__ = [...]`` (or tuple/set of string literals) is
    honored. A dynamic ``__all__`` (function call, concatenation) yields None,
    which makes star imports see the module's full symbol table — matching
    Python's "no __all__ means all public names" semantics.
    """
    for statement in root.children:
        if statement.type != "expression_statement":
            continue
        assignment = _find_child(statement, "assignment")
        if assignment is None:
            continue
        left = assignment.child_by_field_name("left")
        if left is None or left.type != "identifier":
            continue
        if left.text.decode("utf-8") != "__all__":
            continue
        right = assignment.child_by_field_name("right")
        if right is None or right.type not in ("list", "tuple", "set"):
            return None
        names: set[str] = set()
        for item in right.named_children:
            if item.type != "string":
                return None  # non-literal element -> treat as dynamic
            names.add(item.text.decode("utf-8").strip("'\""))
        return names
    return None


def _esm_relative_module(spec: str, file_path: str, repo_root: str) -> str | None:
    """Canonical module qname for a relative ESM specifier (``./auth``,
    ``../lib/x``), or None for non-relative specs / specs escaping the repo.

    Mirrors ``_module_qname``'s path-to-qname conventions: a known source
    suffix is stripped from the last segment (so explicit ``./auth.ts`` and
    bare ``./auth`` agree), a leading ``src/`` segment is dropped. A bare
    specifier is probed on disk first: ``./auth`` → ``auth.ts`` (extension
    resolution, JS-M09) and ``./dir`` → ``dir/index.ts`` (directory index
    resolution, JS-M10) land on the real module qname instead of a module that
    may not exist. Path-based (not module-qname-parts-based) so imports that
    hop out of a stripped ``src/`` tree still land on the right root-level
    module.
    """
    if not spec.startswith("."):
        return None
    rel = _repo_relative_path(file_path, repo_root)
    target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))
    if target.startswith(".."):
        return None  # escapes the repo root - keep the raw specifier
    parts = [part for part in target.split("/") if part not in ("", ".")]
    if not parts:
        return None
    if not _has_source_suffix(parts[-1]):
        for ext in SOURCE_SUFFIXES:
            with_ext = parts[:-1] + [parts[-1] + ext]
            if _path_exists(repo_root, with_ext):
                return _parts_to_module(with_ext)
            index = parts + ["index" + ext]
            if _path_exists(repo_root, index):
                return _parts_to_module(index)
    for ext in SOURCE_SUFFIXES:
        if parts[-1].endswith(ext):
            parts[-1] = parts[-1][: -len(ext)]
            break
    if parts and parts[0] == "src":
        parts = parts[1:]
    if not parts:
        return None
    return ".".join(parts)


def _has_source_suffix(name: str) -> bool:
    """True when the last path segment already carries a source suffix."""
    return any(name.endswith(ext) for ext in SOURCE_SUFFIXES)


def _path_exists(repo_root: str, parts: list[str]) -> bool:
    """True when ``repo_root/parts`` exists as a file (relative-module probe)."""
    return Path(repo_root).joinpath(*parts).is_file()


def _parts_to_module(parts: list[str]) -> str:
    """Path parts → module qname, mirroring _module_qname (strip a source suffix
    from the last segment, drop a leading src/, join with dots)."""
    if parts and _has_source_suffix(parts[-1]):
        for ext in SOURCE_SUFFIXES:
            if parts[-1].endswith(ext):
                parts[-1] = parts[-1][: -len(ext)]
                break
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def _extract_imports_esm(root, lang, file_path: str, repo_root: str,
                         module_qname: str = "") -> list[ImportEntry]:
    """Extract ES module imports: import/export statements.

    Relative specifiers (./auth, ../lib/x) are canonicalized to the imported
    module's qname at parse time - the same policy as Python relative imports -
    so call/import resolution matches real repo modules instead of keeping the
    raw "./auth" text. Non-relative specifiers (package names, configured path
    aliases) pass through unchanged.

    Handles:
      import {a, b} from "mod"     → imported_name per specifier
      import * as ns from "mod"    → namespace (imported_name=None)
      import d from "mod"          → default (imported_name="default")
      export {x} from "mod"        → re-export (treated as import)
      export {x}                   → local re-export binding back to this module
      export * from "mod"          → star re-export (barrel)
    """
    def _mod(source: str) -> str:
        return _esm_relative_module(source, file_path, repo_root) or source

    entries: list[ImportEntry] = []
    for node in root.children:
        if node.type == "import_statement":
            source = _mod(_esm_source(node))
            clause = _find_child(node, "import_clause")
            if clause is None and source:
                # side-effect import: import "mod"
                entries.append(ImportEntry(source, source, None, False,
                                           span=_span(node)))
                continue
            if clause is None:
                continue
            for child in clause.children:
                if child.type == "namespace_import":
                    ident = _find_child(child, "identifier")
                    if ident:
                        entries.append(ImportEntry(ident.text.decode("utf-8"),
                                                   source, None, False,
                                                   span=_span(node)))
                elif child.type == "named_imports":
                    for spec in child.children:
                        if spec.type == "import_specifier":
                            name_node = spec.child_by_field_name("name")
                            alias_node = spec.child_by_field_name("alias")
                            name = name_node.text.decode("utf-8") if name_node else ""
                            local = alias_node.text.decode("utf-8") if alias_node else name
                            entries.append(ImportEntry(local, source, name, False,
                                                       span=_span(node)))
                elif child.type == "identifier":
                    # default import: import foo from "mod"
                    entries.append(ImportEntry(child.text.decode("utf-8"),
                                               source, "default", False,
                                               span=_span(node)))
        elif node.type == "export_statement":
            export_clause = _find_child(node, "export_clause")
            source = _mod(_esm_source(node))
            if export_clause is not None:
                # export {x} from "mod" / export {x} — re-export bindings. A
                # local re-export points back at this module so the re-export
                # chain closes through _resolve_reexport.
                reexport_module = source or module_qname
                for spec in export_clause.children:
                    if spec.type == "export_specifier":
                        name_node = spec.child_by_field_name("name")
                        alias_node = spec.child_by_field_name("alias")
                        name = name_node.text.decode("utf-8") if name_node else ""
                        local = alias_node.text.decode("utf-8") if alias_node else name
                        entries.append(ImportEntry(local, reexport_module, name, False,
                                                   span=_span(node)))
            elif source and any(
                    child.type == "*" and not child.is_named
                    for child in node.children):
                # export * from "mod" — a star re-export (barrel). The grammar
                # exposes the '*' as an anonymous child, not a named node.
                entries.append(ImportEntry("*", source, None, True,
                                           span=_span(node)))
    return entries


def _extract_imports_cjs(root, module_qname: str, file_path: str,
                         repo_root: str) -> list[ImportEntry]:
    """Extract CommonJS imports: require() bindings and exports re-export
    bindings, complementing the ESM channel in the same file.

    Handles:
      const m = require("mod")         → module import (local m)
      const {a, b: c} = require("mod") → named imports (a, c)
      require("mod")                   → side-effect import
      require("mod").foo()             → receiver-alias keyed on the require expr
      exports.foo = bar                → re-export binding (bar)
      module.exports = { ... }         → object barrel re-export bindings
    `module.exports = <def>` is the default export (see _extract_cjs_default_export).
    """
    entries: list[ImportEntry] = []
    for call in _iter_require_calls(root):
        spec = _require_spec(call)
        if spec is None:
            continue
        module = _esm_relative_module(spec, file_path, repo_root) or spec
        call_text = call.text.decode("utf-8").strip()
        entries.append(ImportEntry(call_text, module, None, False,
                                   span=_span(call)))
        entries.extend(_require_bindings(call, module, _span(call)))
    entries.extend(_cjs_export_bindings(root, module_qname))
    return entries


def _iter_require_calls(root) -> list:
    """All `require(...)` call_expression nodes (function field is `require`)."""
    found: list = []

    def walk(node):
        if node.type == "call_expression":
            func = node.child_by_field_name("function")
            if (func is not None and func.type == "identifier"
                    and func.text.decode("utf-8") == "require"):
                found.append(node)
        for child in node.children:
            walk(child)

    walk(root)
    return found


def _require_spec(call) -> str | None:
    """The module specifier string of a require call, or None when it has none."""
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    for child in args.children:
        if child.type == "string":
            return child.text.decode("utf-8").strip("\"'")
    return None


def _require_bindings(call, module: str, span) -> list[ImportEntry]:
    """Name bindings for `const m = require(...)` / `const {a, b: c} = require(...)`.

    The require expression itself is keyed by the caller; this adds the
    declarator name(s) so `m.foo()` and destructured calls resolve through the
    import map. A require call not inside a variable_declarator (bare
    side-effect, `require("mod").foo()` object) yields nothing extra.
    """
    parent = call.parent
    if parent is None or parent.type != "variable_declarator":
        return []
    name_node = parent.child_by_field_name("name")
    if name_node is None:
        return []
    entries: list[ImportEntry] = []
    if name_node.type == "identifier":
        entries.append(ImportEntry(name_node.text.decode("utf-8"), module,
                                   None, False, span=span))
    elif name_node.type == "object_pattern":
        for child in name_node.children:
            if child.type == "shorthand_property_identifier_pattern":
                local = child.text.decode("utf-8")
                entries.append(ImportEntry(local, module, local, False,
                                           span=span))
            elif child.type == "pair_pattern":
                key_node = child.child_by_field_name("key")
                value_node = child.child_by_field_name("value")
                if key_node is not None and value_node is not None:
                    entries.append(ImportEntry(
                        value_node.text.decode("utf-8"), module,
                        key_node.text.decode("utf-8"), False, span=span))
    return entries


def _member_object_prop(node) -> tuple[str, str]:
    """(object_text, property_text) of a member_expression's first level."""
    if node.type != "member_expression":
        return "", ""
    obj = node.child_by_field_name("object")
    prop = node.child_by_field_name("property")
    if obj is None or prop is None:
        return "", ""
    return obj.text.decode("utf-8"), prop.text.decode("utf-8")


def _cjs_export_bindings(root, module_qname: str) -> list[ImportEntry]:
    """Re-export bindings from `exports.foo = X` / `module.exports.foo = X` and
    `module.exports = { ... }` object barrels.

    Each binding names the exported local (foo) so a consumer's
    `const { foo } = require("m")` chains through _resolve_reexport to the
    referenced symbol. `module.exports = <named def>` is the default export and
    is handled by _extract_cjs_default_export.
    """
    entries: list[ImportEntry] = []
    for node in root.children:
        if node.type != "expression_statement":
            continue
        assignment = next((c for c in node.children
                           if c.type == "assignment_expression"), None)
        if assignment is None:
            continue
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression":
            continue
        obj, prop = _member_object_prop(left)
        if obj == "exports":
            binding = _cjs_rhs_binding(right, module_qname)
            if binding is not None:
                entries.append(ImportEntry(prop, binding[0], binding[1], False,
                                           span=_span(node)))
        elif obj == "module.exports":
            binding = _cjs_rhs_binding(right, module_qname)
            if binding is not None:
                entries.append(ImportEntry(prop, binding[0], binding[1], False,
                                           span=_span(node)))
        elif obj == "module" and prop == "exports" and right.type == "object":
            entries.extend(_cjs_object_bindings(right, module_qname,
                                                _span(node)))
    return entries


def _cjs_rhs_binding(right, module_qname: str) -> tuple[str, str] | None:
    """(module, imported_name) a re-export RHS resolves to, or None.

    identifier       → (module_qname, ident)     exports.foo = bar
    member_expression → (object, property)        exports.foo = util.helper
    function/class/object/literal → None (fresh local, no stable symbol)
    """
    if right.type == "identifier":
        return module_qname, right.text.decode("utf-8")
    if right.type == "member_expression":
        obj, prop = _member_object_prop(right)
        if obj and prop:
            return obj, prop
    return None


def _cjs_object_bindings(obj_node, module_qname: str, span) -> list[ImportEntry]:
    """Per-property re-export bindings of a `module.exports = { ... }` barrel."""
    entries: list[ImportEntry] = []
    for child in obj_node.children:
        if child.type == "shorthand_property_identifier_pattern":
            name = child.text.decode("utf-8")
            entries.append(ImportEntry(name, module_qname, name, False,
                                       span=span))
        elif child.type == "pair":
            key_node = child.child_by_field_name("key")
            value_node = child.child_by_field_name("value")
            if key_node is None:
                continue
            key = key_node.text.decode("utf-8")
            if value_node is None:  # { a } shorthand inside a pair
                entries.append(ImportEntry(key, module_qname, key, False,
                                           span=span))
            elif value_node.type == "identifier":
                entries.append(ImportEntry(key, module_qname,
                                           value_node.text.decode("utf-8"),
                                           False, span=span))
            elif value_node.type == "member_expression":
                obj, prop = _member_object_prop(value_node)
                if obj and prop:
                    entries.append(ImportEntry(key, obj, prop, False,
                                               span=span))
    return entries


def _extract_cjs_default_export(root, module_qname: str) -> str | None:
    """The qname a module's `module.exports = X` names, or None.

    X may be a named function/class expression or a reference to a local
    symbol; a fresh anonymous function/class or an object literal has no stable
    qname → None, so a default import of it degrades to unresolved.
    """
    for node in root.children:
        if node.type != "expression_statement":
            continue
        assignment = next((c for c in node.children
                           if c.type == "assignment_expression"), None)
        if assignment is None:
            continue
        left = assignment.child_by_field_name("left")
        right = assignment.child_by_field_name("right")
        if left is None or right is None or left.type != "member_expression":
            continue
        obj, prop = _member_object_prop(left)
        if not (obj == "module" and prop == "exports"):
            continue
        if right.type in ("function_expression", "class"):
            name_node = right.child_by_field_name("name")
            if name_node is not None:
                return qname.join(module_qname, name_node.text.decode("utf-8"))
        if right.type == "identifier":
            return qname.join(module_qname, right.text.decode("utf-8"))
    return None


def _extract_esm_default_export(root, module_qname: str) -> str | None:
    """The qname a module's ``export default`` names, or None.

    Handles ``export default function foo()`` / ``export default class Foo``
    (the declared qname) and ``export default foo`` (reference to a local). A
    constant/expression default (``export default 42``) has no stable qname →
    None, so a default import of it degrades to unresolved. The qname is stored
    even before it exists in the graph; resolution checks existence.
    """
    for node in root.children:
        if node.type != "export_statement":
            continue
        if _find_child(node, "export_clause") is not None:
            continue  # export {x} — not a default export
        if any(child.type == "*" and not child.is_named for child in node.children):
            continue  # export * from — not a default export
        declaration = _find_child(node, "function_declaration")
        if declaration is None:
            declaration = _find_child(node, "class_declaration")
        if declaration is not None:
            name = declaration.child_by_field_name("name")
            if name is not None:
                return qname.join(module_qname, name.text.decode("utf-8"))
            continue
        for child in node.children:
            if child.type == "identifier":
                return qname.join(module_qname, child.text.decode("utf-8"))
    return None


_VUE_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL)
_VUE_LANG_RE = re.compile(r"lang\s*=\s*[\"']?([\w-]+)")


def _extract_vue_script(source: bytes) -> tuple[bytes, int, str | None]:
    """Extract concatenated <script> blocks from a .vue SFC, choosing the
    script dialect from the blocks' `lang` attribute.

    Returns (script_bytes, line_offset, lang_name) — line_offset is the
    0-based line index of the first block's content, to be added to all
    tree-sitter line numbers (which are 0-based). lang_name is
    "typescript" when any block declares lang="ts"/"tsx", "javascript"
    (Vue's plain-<script> default) otherwise — including lang="js"/"jsx"
    and blocks with no lang at all. None when the file has no <script>.
    """
    text = source.decode("utf-8")
    match = _VUE_SCRIPT_RE.search(text)
    if match is None:
        return source, 0, None
    # Count newlines before the start of the first block's content
    # <script ...>\n  ← content starts here
    tag_end = match.start(2)
    line_offset = text[:tag_end].count("\n")
    parts = _VUE_SCRIPT_RE.findall(text)  # list of (attrs, content)
    langs = [m.group(1).lower() for attrs, _content in parts
             if (m := _VUE_LANG_RE.search(attrs))]
    lang_name = "typescript" if any(l.startswith("ts") for l in langs) else "javascript"
    return "\n".join(content for _attrs, content in parts).encode("utf-8"), line_offset, lang_name


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
