from code_review_ai import qname

from dataclasses import dataclass

from code_review_ai.parser import ParsedFile, RawCall, CALL_SIMPLE, CALL_ATTRIBUTE


@dataclass
class Edge:
    """A resolved call edge.

    source     — caller qualified name
    target     — callee: resolved qname (if resolution=resolved), raw expr otherwise
    resolution — resolved / dynamic / unresolved
    """
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
    all_import_maps = {pf.module_qname: _import_map(pf) for pf in parsed_files}
    edges: list[Edge] = []
    for pf in parsed_files:
        local = mod_syms.get(pf.module_qname, {})
        imports = _import_map(pf)
        for c in pf.raw_calls:
            edges.append(_resolve_one(c, local, imports, existing_qnames, all_import_maps))
    return edges


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


def _resolved(base: Edge, target: str, existing: set[str]) -> Edge:
    base.target = target
    base.resolution = "resolved" if _exists(target, existing) else "unresolved"
    return base


# ── edge generators: structural relationships ─────────────────────────


def _build_contains(parsed: list[ParsedFile], qnames: set[str]) -> list[Edge]:
    """CONTAINS edges: parent_qname → child (module→function, class→method, etc.)."""
    edges: list[Edge] = []
    for pf in parsed:
        for n in pf.nodes:
            if n.parent_qname:
                edges.append(Edge(
                    source=n.parent_qname, target=n.qualified_name,
                    kind="contains", file_path=n.file_path, call_line=0,
                    resolution="resolved" if n.parent_qname in qnames else "unresolved",
                ))
    return edges


def _build_imports(parsed: list[ParsedFile], qnames: set[str]) -> list[Edge]:
    """IMPORT edges: module → imported_module."""
    edges: list[Edge] = []
    for pf in parsed:
        for imp in pf.imports:
            if imp.is_star:
                continue
            tgt = imp.module
            edges.append(Edge(
                source=pf.module_qname, target=tgt, kind="import",
                file_path=pf.file_path, call_line=0,
                resolution="resolved" if tgt in qnames else "unresolved",
            ))
    return edges


def _build_inherits(parsed: list[ParsedFile], qnames: set[str]) -> list[Edge]:
    """INHERITS edges: subclass → base class / interface."""
    edges: list[Edge] = []
    for pf in parsed:
        for ih in pf.inherits:
            tgt = ih.base_expr
            resolved = tgt in qnames
            if not resolved and "::" not in tgt:
                scoped = qname.join(pf.module_qname, tgt)
                if scoped in qnames:
                    tgt = scoped
                    resolved = True
            edges.append(Edge(
                source=ih.class_qname, target=tgt, kind=ih.relation,
                file_path=pf.file_path, call_line=0,
                resolution="resolved" if resolved else "unresolved",
            ))
    return edges


def resolve_edges(parsed: list[ParsedFile],
                  existing_qnames: set[str]) -> list[Edge]:
    """Resolve all edges — call, contains, import, inherits — from parsed files.

    This is the single entry point for edge generation. Indexer calls this
    once and gets the complete edge list.
    """
    edges = resolve_calls(parsed, existing_qnames)
    edges.extend(_build_contains(parsed, existing_qnames))
    edges.extend(_build_imports(parsed, existing_qnames))
    edges.extend(_build_inherits(parsed, existing_qnames))
    return edges
