
import fnmatch
import os
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


@dataclass
class RawInherit:
    """A class inheritance relationship extracted from AST."""
    class_qname: str   # the subclass qname
    base_expr: str     # raw base class / interface expression
    relation: str      # "extends" | "implements"


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
    inherits: list[RawInherit] = field(default_factory=list)
    var_types: dict[str, dict[str, str]] = field(default_factory=dict)


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
                 repo_root: str = "") -> bool:
    """True if a node lives in a test file or has a test-style short name.

    File-path globs (``test_globs``) are matched against the **repo-relative**
    path with forward slashes (the same form ``git ls-files`` /
    ``filter_excluded`` use), not the absolute path - otherwise a repo living
    under ``.../test-platform/`` or a pytest tmp dir named ``test_impact_*``
    would tag every node as a test. A leading ``*/`` matches any leading
    directory chain; the ``*/``-stripped pattern is also matched against the
    path and the bare filename so ``*/test*`` catches a top-level
    ``test_auth.py``. Name globs (``test_names``) match the node's short name
    (e.g. ``test_*`` -> ``test_login``). Either match wins.
    """
    rel = _repo_relative_path(file_path, repo_root)
    if _matches_test_globs(rel, test_globs):
        return True
    short = qname.short(qualified_name)
    return any(fnmatch.fnmatch(short, pat) for pat in test_names)


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
                        ))
        _walk_inherits(child, module_qname, lang, out)


def parse_file(file_path: str, repo_root: str, lang: dict | None = None) -> ParsedFile:
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
        source, line_offset = _extract_vue_script(source)
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
    pf.imports = _extract_imports(root, module_qname, lang, lang_name, file_path)
    # handle inheritance
    _walk_inherits(root, module_qname, lang, pf.inherits)
    if lang_name == "java":
        pf.var_types = _collect_java_var_types(root, module_qname, lang)
        mappings = _collect_java_mappings(root, module_qname, lang)
        for n in pf.nodes:
            if n.qualified_name in mappings:
                n.mappings = mappings[n.qualified_name]

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
                     file_path: str) -> list[ImportEntry]:
    if lang_name == "python":
        return _extract_imports_python(root, module_qname, lang, file_path)
    if lang_name == "java":
        return _extract_imports_java(root, lang)
    return _extract_imports_esm(root, lang)


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
            entries.append(ImportEntry(member, class_qn, member, False))
        elif "asterisk" in child_types:
            entries.append(ImportEntry("*", full, None, True))
        else:
            pkg, _, cls = full.rpartition(".")
            entries.append(ImportEntry(cls, pkg, cls, False))
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
