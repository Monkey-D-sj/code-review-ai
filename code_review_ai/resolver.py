from code_review_ai import qname

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
                short = qname.short(n.qualified_name)

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
                tgt = qname.join(mod, imp_name)
                return _resolved(base, tgt, existing)
            return _resolved(base, mod, existing)  # imported module itself
        return base  # unresolved
    if c.call_form == "attribute":
        head = c.target_expr.split(".", 1)[0]
        rest = c.target_expr[len(head) + 1:]
        if head in imports:
            mod, imp_name, _ = imports[head]
            if imp_name is None:  # import m / import m as head -> m.rest
                tgt = qname.join(mod, rest)
                return _resolved(base, tgt, existing)
        if head in local and local[head] in existing:
            cls_qn = local[head]
            tgt = qname.join(cls_qn, rest)
            return _resolved(base, tgt, existing)
        base.resolution = "dynamic"
        return base
    return base  # other -> unresolved


def _resolved(base: Edge, target: str, existing: set[str]) -> Edge:
    base.target = target
    base.resolution = "resolved" if _exists(target, existing) else "unresolved"
    return base
