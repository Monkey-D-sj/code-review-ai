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
    module_qname = _module_qname(file_path, repo_root)
    source = Path(file_path).read_bytes()
    tree = _PARSER.parse(source)
    root = tree.root_node

    pf = ParsedFile(file_path=file_path, module_qname=module_qname)
    pf.nodes.append(ParsedNode(
        qualified_name=module_qname, kind="module", file_path=file_path,
        start_line=1, end_line=root.end_point[0] + 1,
        signature="", parent_qname=None,
    ))

    _walk_defs_typed(root, source, file_path, module_qname, None, pf.nodes)

    # Reclassify functions inside classes to "method"
    parents = {n.qualified_name: n.kind for n in pf.nodes}
    for n in pf.nodes:
        if n.parent_qname and parents.get(n.parent_qname) == "class":
            n.kind = "method"

    _walk_calls(root, source, file_path, module_qname, None, pf.raw_calls)
    pf.imports = _extract_imports(root, module_qname)

    return pf


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
