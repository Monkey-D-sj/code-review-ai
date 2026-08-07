from code_review_ai import qname

from dataclasses import dataclass

from code_review_ai.parser import (ParsedFile, RawCall, CALL_SIMPLE,
                                   CALL_ATTRIBUTE, CALL_CONSTRUCT)


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
    """module_qname -> {local_name: qualified_name}, merged across files that
    share a module (Java classes in the same package)."""
    out: dict[str, dict[str, str]] = {}
    for pf in parsed_files:
        syms = out.setdefault(pf.module_qname, {})
        for n in pf.nodes:
            if n.kind in ("function", "class"):
                syms[qname.short(n.qualified_name)] = n.qualified_name
    return out


def _import_map(pf: ParsedFile) -> dict:
    """local_name -> (module, imported_name_or_None, is_star)."""
    return {i.local_name: (i.module, i.imported_name, i.is_star) for i in pf.imports}


def _exists(qname: str, existing: set[str]) -> bool:
    return qname in existing


def resolve_calls(parsed_files: list[ParsedFile], existing_qnames: set[str]) -> list[Edge]:
    mod_syms = _module_symbols(parsed_files)
    all_import_maps = {pf.module_qname: _import_map(pf) for pf in parsed_files}
    var_types = {qn: types for pf in parsed_files
                 for qn, types in pf.var_types.items()}
    class_qnames = {n.qualified_name for f in parsed_files for n in f.nodes if n.kind == "class"}
    edges: list[Edge] = []
    for pf in parsed_files:
        local = mod_syms.get(pf.module_qname, {})
        imports = _import_map(pf)
        for c in pf.raw_calls:
            edge = _resolve_one(c, local, imports, existing_qnames, all_import_maps,
                                mod_syms=mod_syms, source_module=pf.module_qname,
                                var_types=var_types)
            edges.append(edge)
            if edge.resolution == "resolved" and edge.target in class_qnames:
                init_qn = qname.join(qname.module(edge.target), "__init__", edge.target)
                if init_qn in existing_qnames:
                    edges.append(Edge(source=edge.source, target=init_qn, kind="call",
                                      file_path=edge.file_path, call_line=edge.call_line,
                                      resolution="resolved"))
    return edges


def _resolve_one(c: RawCall, local: dict, imports: dict,
                 existing: set[str], all_import_maps: dict,
                 mod_syms: dict | None = None,
                 source_module: str | None = None,
                 var_types: dict | None = None) -> Edge:
    base = Edge(source=c.source_qname, target=c.target_expr, kind="call",
                file_path=c.file_path, call_line=c.call_line, resolution="unresolved")
    if c.language == "java":
        return _resolve_java(c, local, imports, existing, mod_syms,
                             source_module, base, var_types)
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


# ── Java call resolution ──────────────────────────────────────────────


def _join_target(mod: str, name: str) -> str:
    """Join a module/class prefix with a member into a qualified name.

    When mod already contains '::' (a class qname — e.g. a Java static-import
    target), append with SCOPE_SEP; otherwise the standard module::name form."""
    if "::" in mod:
        return f"{mod}.{name}"
    return qname.join(mod, name)


def _enclosing_class(qualified_name: str) -> str | None:
    """The first scope of a qname (the class containing a method), or None."""
    mod = qname.module(qualified_name)
    rest = qualified_name[len(mod) + len(qname.MODULE_SEP):]
    if not rest:
        return None
    first_scope = rest.split(qname.SCOPE_SEP, 1)[0]
    return qname.join(mod, first_scope)


def _resolve_java_dotted(expr: str, mod_syms: dict, existing: set[str]) -> str | None:
    """Resolve a dotted call by longest module prefix (FQCN / same-package).

    e.g. com.foo.Bar.create() or Bar.create() where Bar is in a known module.
    """
    parts = expr.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        syms = mod_syms.get(mod)
        if not syms:
            continue
        head = parts[i]
        if head not in syms:
            continue
        class_qn = syms[head]
        member = ".".join(parts[i + 1:])
        if member:
            target = _join_target(class_qn, member)
            if target in existing:
                return target
        elif class_qn in existing:
            return class_qn
    return None


def _resolve_java_type(type_name: str, source_module: str | None,
                       imports: dict, mod_syms: dict | None) -> str | None:
    """Resolve a Java type name to a class qname: same-package class, then import."""
    if mod_syms and source_module:
        same_pkg = mod_syms.get(source_module, {})
        if type_name in same_pkg:
            return same_pkg[type_name]
    if type_name in imports:
        mod, imported, _star = imports[type_name]
        return _join_target(mod, imported) if imported else mod
    return None


def _resolve_java(c, local: dict, imports: dict, existing: set[str],
                  mod_syms: dict | None, source_module: str | None,
                  base: Edge, var_types: dict | None = None) -> Edge:
    """Java-aware call resolution: simple / attribute / construct forms."""
    if c.call_form == CALL_SIMPLE:
        name = c.target_expr
        if name in local:
            return _resolved(base, local[name], existing)
        if name in imports:
            mod, imported, _star = imports[name]
            if imported:
                return _resolved(base, _join_target(mod, imported), existing)
            return _resolved(base, mod, existing)
        if mod_syms and source_module:
            same_pkg = mod_syms.get(source_module, {})
            if name in same_pkg:
                return _resolved(base, same_pkg[name], existing)
        enclosing = _enclosing_class(c.source_qname)
        if enclosing:
            target = _join_target(enclosing, name)
            if target in existing:
                return _resolved(base, target, existing)
        return base
    if c.call_form == CALL_ATTRIBUTE:
        expr = c.target_expr
        if expr.startswith("this."):
            expr = expr[len("this."):]
        head, sep, rest = expr.partition(".")
        if not sep:
            base.resolution = "dynamic"
            return base
        # Java receiver type binding: bare identifier whose declared type we know
        if var_types:
            scope_types = var_types.get(c.source_qname, {})
            receiver_type = scope_types.get(head)
            if receiver_type:
                class_qn = _resolve_java_type(
                    receiver_type, source_module, imports, mod_syms)
                if class_qn:
                    target = _join_target(class_qn, rest)
                    if target in existing:
                        return _resolved(base, target, existing)
        if head in imports:
            mod, imported, _star = imports[head]
            if imported:
                class_qn = _join_target(mod, imported)
                return _resolved(base, _join_target(class_qn, rest), existing)
            return _resolved(base, _join_target(mod, rest), existing)
        if head in local and local[head] in existing:
            return _resolved(base, _join_target(local[head], rest), existing)
        if mod_syms:
            target = _resolve_java_dotted(c.target_expr, mod_syms, existing)
            if target:
                return _resolved(base, target, existing)
        base.resolution = "dynamic"
        return base
    if c.call_form == CALL_CONSTRUCT:
        name = c.target_expr
        candidates: list[str] = []
        if name in local:
            candidates.append(local[name])
        if name in imports:
            mod, imported, _star = imports[name]
            candidates.append(_join_target(mod, imported) if imported else mod)
        if mod_syms and source_module:
            same_pkg = mod_syms.get(source_module, {})
            if name in same_pkg:
                candidates.append(same_pkg[name])
        for candidate in candidates:
            if candidate in existing:
                return _resolved(base, candidate, existing)
        return base
    return base  # CALL_OTHER -> unresolved


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
            # Java static imports reference a class member (module is a class
            # qname, not a module) — not an import edge.
            if imp.is_star or "::" in imp.module:
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
    from code_review_ai.java_routing import build_route_edges
    edges.extend(build_route_edges(parsed, existing_qnames))
    return edges
