import fnmatch
import re

from code_review_ai import qname

from dataclasses import dataclass

from code_review_ai.parser import (ParsedFile, RawCall, CALL_SIMPLE,
                                   CALL_ATTRIBUTE, CALL_CONSTRUCT,
                                   SOURCE_SUFFIXES)
# Bare identifier or dotted path — the shapes a DI-marker argument takes when it
# names a dependency (``get_db``, ``services.get_db``). Everything else — calls,
# literals, keyword args like ``use_cache=False``, ``...`` — is not a dependency
# reference and is skipped.
_DI_ARG_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


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


def _import_map(pf: ParsedFile, path_aliases: dict[str, str] | None = None,
                existing: set[str] | None = None) -> dict:
    """local_name -> (module, imported_name_or_None, is_star).

    The module string is canonicalized through path_aliases when it resolves to
    a real module qname (so `@/x` -> `x` consistently across call resolution and
    re-export traversal); otherwise the raw specifier is kept.
    """
    if path_aliases and existing is not None:
        return {i.local_name: (_module_of(i.module, path_aliases, existing),
                               i.imported_name, i.is_star) for i in pf.imports}
    return {i.local_name: (i.module, i.imported_name, i.is_star) for i in pf.imports}


def _exists(qname: str, existing: set[str]) -> bool:
    return qname in existing


def _alias_replaced(spec: str, path_aliases: dict[str, str] | None) -> tuple[bool, str]:
    """(matched, spec) — when `spec` starts with a configured alias prefix,
    replace it (e.g. with {"@/": "src/"}, "@/hooks/x" -> "src/hooks/x") and
    return True. Otherwise return (False, spec) unchanged, so relative/bare
    imports keep their exact pre-existing resolution behavior."""
    for prefix, target in (path_aliases or {}).items():
        if spec.startswith(prefix):
            return True, target.rstrip("/") + "/" + spec[len(prefix):]
    return False, spec


def _spec_to_module(spec: str, path_aliases: dict[str, str] | None,
                    existing: set[str]) -> str | None:
    """Resolve an alias-prefixed import specifier to an existing module qname,
    or None.

    Only specifiers that actually match an alias prefix are re-derived, so
    relative/bare imports are left untouched. The alias target is converted to
    a module qname the same way parser._module_qname derives one from a
    repo-relative path (strip a known source suffix, drop a leading ``src/``,
    join with dots). Returns None when the alias target has no module in the
    graph, so callers keep the raw specifier on their unresolved edges.
    """
    matched, norm = _alias_replaced(spec, path_aliases)
    if not matched or "::" in norm:
        return None
    parts = [part for part in norm.split("/") if part not in ("", ".")]
    if not parts or not parts[-1]:
        return None
    last = parts[-1]
    for ext in SOURCE_SUFFIXES:
        if last.endswith(ext):
            parts[-1] = last[: -len(ext)]
            break
    if parts and parts[0] == "src":
        parts = parts[1:]
    candidate = ".".join(parts)
    return candidate if candidate in existing else None


def _module_of(spec: str, path_aliases: dict[str, str] | None,
               existing: set[str]) -> str:
    """Canonical module qname for an import specifier, or the raw specifier."""
    return _spec_to_module(spec, path_aliases, existing) or spec


def _dedup_append(edges: list[Edge], seen: set[tuple[str, str, str]],
                  edge: Edge) -> None:
    """Keep one edge per (source, target, kind).

    A function calling the same target N times produces N raw calls but one
    graph edge - the repeated call count carries no topological meaning for
    impact/flow queries.
    """
    key = (edge.source, edge.target, edge.kind)
    if key in seen:
        return
    seen.add(key)
    edges.append(edge)


def resolve_calls(parsed_files: list[ParsedFile], existing_qnames: set[str],
                  path_aliases: dict[str, str] | None = None,
                  dependency_markers: list[str] | None = None) -> list[Edge]:
    mod_syms = _module_symbols(parsed_files)
    all_import_maps = {pf.module_qname: _import_map(pf, path_aliases, existing_qnames)
                       for pf in parsed_files}
    var_types = {qn: types for pf in parsed_files
                 for qn, types in pf.var_types.items()}
    class_qnames = {n.qualified_name for f in parsed_files for n in f.nodes if n.kind == "class"}
    edges: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    for pf in parsed_files:
        local = mod_syms.get(pf.module_qname, {})
        imports = _import_map(pf, path_aliases, existing_qnames)
        for c in pf.raw_calls:
            edge = _resolve_one(c, local, imports, existing_qnames, all_import_maps,
                                mod_syms=mod_syms, source_module=pf.module_qname,
                                var_types=var_types, path_aliases=path_aliases)
            _dedup_append(edges, seen, edge)
            if edge.resolution == "resolved" and edge.target in class_qnames:
                init_qn = _init_member_qname(edge.target, c.language)
                if init_qn in existing_qnames:
                    _dedup_append(edges, seen, Edge(
                        source=edge.source, target=init_qn, kind="call",
                        file_path=edge.file_path,
                        resolution="resolved"))
            if dependency_markers and _is_di_marker(c.target_expr, dependency_markers):
                for dep_qn in _resolve_di_args(c, local, imports, existing_qnames,
                                               all_import_maps, mod_syms=mod_syms,
                                               source_module=pf.module_qname,
                                               var_types=var_types,
                                               path_aliases=path_aliases):
                    _dedup_append(edges, seen, Edge(
                        source=c.source_qname, target=dep_qn, kind="call",
                        file_path=c.file_path, resolution="resolved"))
    return edges


def _init_member_qname(class_qn: str, language: str) -> str:
    """The constructor member a class-instantiating edge also links to.

    Python classes initialize via ``__init__``; Java constructors are declared
    with the class's own name, so the member is ``Class.Class`` - reusing the
    class short name instead of Python's ``__init__`` convention (which never
    exists in Java and made the extra edge silently unresolvable)."""
    member = "__init__" if language != "java" else qname.short(class_qn)
    return qname.join(qname.module(class_qn), member, class_qn)


def _is_di_marker(target_expr: str, markers: list[str]) -> bool:
    """True when a call's target short name matches a dependency_markers glob
    (mirrors entry_names matching: fnmatch against qname.short)."""
    return any(fnmatch.fnmatch(qname.short(target_expr), pat) for pat in markers)


def _resolve_di_args(c: RawCall, local: dict, imports: dict,
                     existing: set[str], all_import_maps: dict,
                     mod_syms: dict | None = None,
                     source_module: str | None = None,
                     var_types: dict | None = None,
                     path_aliases: dict[str, str] | None = None) -> list[str]:
    """Resolve the callable arguments of a DI-marker call (``Depends(get_db)``)
    to qnames by reusing _resolve_one on a fabricated RawCall — the same
    local/import/reexport machinery as any normal call.

    Only bare-identifier / dotted-path args qualify; expressions (nested calls,
    literals, keyword args) yield nothing — nested calls like ``Depends(make_db())``
    are already captured by the parser as their own RawCall, so only the bare
    dependency-reference gap is filled here.
    """
    resolved: list[str] = []
    for arg in c.args:
        if not _DI_ARG_RE.fullmatch(arg):
            continue
        fake = RawCall(source_qname=c.source_qname, target_expr=arg,
                       call_form=CALL_SIMPLE if "." not in arg else CALL_ATTRIBUTE,
                       file_path=c.file_path, language=c.language)
        edge = _resolve_one(fake, local, imports, existing, all_import_maps,
                            mod_syms=mod_syms, source_module=source_module,
                            var_types=var_types, path_aliases=path_aliases)
        if edge.resolution == "resolved":
            resolved.append(edge.target)
    return resolved


def _module_member(mod: str, rest: str) -> str:
    """The member name once a module's own segments are consumed from `rest`.

    ``import a.b`` binds ``a`` to module ``a.b``, so the call ``a.b.fn()`` has
    head ``a`` and rest ``b.fn``; the module already accounts for ``b``, leaving
    member ``fn`` (target ``a.b::fn``). Single-segment modules (plain
    ``import m``) leave ``rest`` unchanged.
    """
    extra = len(mod.split(".")) - 1
    if extra <= 0:
        return rest
    rest_parts = rest.split(".")
    if extra <= len(rest_parts):
        return ".".join(rest_parts[extra:])
    return rest  # rest shorter than the module's own segments: keep as-is


def _resolve_one(c: RawCall, local: dict, imports: dict,
                 existing: set[str], all_import_maps: dict,
                 mod_syms: dict | None = None,
                 source_module: str | None = None,
                 var_types: dict | None = None,
                 path_aliases: dict[str, str] | None = None) -> Edge:
    base = Edge(source=c.source_qname, target=c.target_expr, kind="call",
                file_path=c.file_path, resolution="unresolved")
    if c.language == "java":
        return _resolve_java(c, local, imports, existing, mod_syms,
                             source_module, base, var_types)
    if c.call_form == CALL_SIMPLE:
        name = c.target_expr
        if name in local:
            return _resolved(base, local[name], existing)
        if name in imports:
            mod, imp_name, _star = imports[name]
            mod = _module_of(mod, path_aliases, existing)  # alias @/x -> real module
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
            mod = _module_of(mod, path_aliases, existing)  # alias @/x -> real module
            if imp_name is None:  # import m / import m as head -> m.rest
                member = _module_member(mod, rest)
                tgt = qname.join(mod, member) if member else mod
                if tgt not in existing:
                    tgt = _resolve_reexport(mod, member, all_import_maps, existing) or tgt
                return _resolved(base, tgt, existing)
        if head in local and local[head] in existing:
            cls_qn = local[head]
            tgt = _join_target(cls_qn, rest)
            return _resolved(base, tgt, existing)
        if c.language == "python" and head in ("self", "cls"):
            # method receiver -> enclosing class, mirroring Java's this./type
            # binding; bare `g()` stays module-scope (Python LEGB), not A.g
            enclosing = _enclosing_class(c.source_qname)
            if enclosing:
                tgt = _join_target(enclosing, rest)
                if tgt in existing:
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
                    kind="contains", file_path=n.file_path,
                    resolution="resolved" if n.parent_qname in qnames else "unresolved",
                ))
    return edges


def _build_imports(parsed: list[ParsedFile], qnames: set[str],
                   path_aliases: dict[str, str] | None = None) -> list[Edge]:
    """IMPORT edges: module → imported_module."""
    edges: list[Edge] = []
    for pf in parsed:
        for imp in pf.imports:
            # Java static imports reference a class member (module is a class
            # qname, not a module) — not an import edge.
            if imp.is_star or "::" in imp.module:
                continue
            tgt = imp.module
            resolved = tgt in qnames
            if not resolved:  # alias @/x -> real module qname
                cand = _spec_to_module(tgt, path_aliases, qnames)
                if cand is not None:
                    tgt = cand
                    resolved = True
            edges.append(Edge(
                source=pf.module_qname, target=tgt, kind="import",
                file_path=pf.file_path,
                resolution="resolved" if resolved else "unresolved",
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
                file_path=pf.file_path,
                resolution="resolved" if resolved else "unresolved",
            ))
    return edges


def _is_di_annotated(annotations: list[str], di_annotations: list[str] | None) -> bool:
    """True when a declaration's annotation matches a configured di_annotations
    glob (fnmatch against the annotation name, mirroring dependency_markers)."""
    return any(fnmatch.fnmatch(annotation, pat)
               for annotation in annotations for pat in (di_annotations or []))


def _build_di_edges(parsed: list[ParsedFile], existing: set[str], mod_syms: dict,
                    all_import_maps: dict,
                    di_annotations: list[str] | None) -> list[Edge]:
    """Annotation/constructor DI edges: injection point -> dependency class.

    Field injection (``@Autowired private OwnerRepository owners;``) requires an
    annotation matching di_annotations; constructor parameters are always
    candidates (a repo-typed ctor param is a real type dependency, framework or
    not - Spring injects single-constructor params even unannotated). The dep
    type must resolve to a repo class (same-package -> import, via
    _resolve_java_type); primitives/String/external types drop out naturally.
    kind="call" matches the existing Depends()-marker DI edges, so in_degree /
    flow / dead-code consume them the same way."""
    edges: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    for pf in parsed:
        if pf.language != "java" or not pf.di_decls:
            continue
        imports = all_import_maps.get(pf.module_qname, {})
        for decl in pf.di_decls:
            if (decl.mechanism == "field"
                    and not _is_di_annotated(decl.annotations, di_annotations)):
                continue
            class_qn = _resolve_java_type(decl.dep_expr, pf.module_qname,
                                          imports, mod_syms)
            if not class_qn or class_qn not in existing:
                continue
            _dedup_append(edges, seen, Edge(
                source=decl.owner_qname, target=class_qn, kind="call",
                file_path=pf.file_path, resolution="resolved"))
    return edges


def resolve_edges(parsed: list[ParsedFile],
                  existing_qnames: set[str],
                  path_aliases: dict[str, str] | None = None,
                  dependency_markers: list[str] | None = None,
                  di_annotations: list[str] | None = None) -> list[Edge]:
    """Resolve all edges — call, contains, import, inherits — from parsed files.

    This is the single entry point for edge generation. Indexer calls this
    once and gets the complete edge list.
    """
    edges = resolve_calls(parsed, existing_qnames, path_aliases,
                          dependency_markers)
    edges.extend(_build_contains(parsed, existing_qnames))
    edges.extend(_build_imports(parsed, existing_qnames, path_aliases))
    edges.extend(_build_inherits(parsed, existing_qnames))
    mod_syms = _module_symbols(parsed)
    import_maps = {pf.module_qname: _import_map(pf, path_aliases, existing_qnames)
                   for pf in parsed}
    edges.extend(_build_di_edges(parsed, existing_qnames, mod_syms,
                                 import_maps, di_annotations))
    from code_review_ai.java_routing import build_route_edges
    edges.extend(build_route_edges(parsed, existing_qnames))
    return edges
