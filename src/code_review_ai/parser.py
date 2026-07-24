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

    return pf
