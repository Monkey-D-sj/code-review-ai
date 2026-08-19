import fnmatch
import hashlib
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

# Upper bound on candidate edges produced for one call site (a star/wildcard
# import that names a symbol in several modules). Mirrors impact._UNCERTAINTY_LIMIT.
_MAX_CANDIDATES = 20


@dataclass
class Edge:
    """A resolved call edge.

    source     — caller qualified name
    target     — callee: resolved qname (if resolution=resolved), raw expr otherwise
    resolution — resolved / dynamic / unresolved
    origin     — how the edge was derived: syntax|module|type|framework|heuristic
    rule_id    — the framework/heuristic rule that produced it (e.g. JAVA-F04);
                 None for plain syntax-derived edges
    confidence — 0.0..1.0; how sure we are of the resolution
    evidence_json — structured evidence (JSON-serializable dict); serialized at
                 write time, e.g. {"call_form", "target_expr"} for dynamic edges
    site_id    — groups the candidate edges of one call site (Phase 4); None today
    """
    source: str
    target: str
    kind: str
    file_path: str
    resolution: str
    origin: str = "syntax"
    rule_id: str | None = None
    confidence: float = 1.0
    evidence_json: dict | None = None
    site_id: str | None = None


def _mark_dynamic(base: Edge, call_form: str, target_expr: str) -> Edge:
    """Mark an attribute-call edge as dynamic with syntax evidence.

    The receiver type is not statically known, so the target depends on a
    runtime value. The raw expression and call form are kept as evidence so the
    AI reviewer can see exactly what was left unbound."""
    base.resolution = "dynamic"
    base.evidence_json = {"call_form": call_form, "target_expr": target_expr}
    return base


def _candidates(base: Edge, targets: list[str]) -> list[Edge]:
    """One candidate edge per possible target, grouped by a stable site id.

    All candidates from one call site share a site_id derived from
    (source, target_expr) so a reviewer can see they are alternatives for the
    same expression. At most _MAX_CANDIDATES are kept; the evidence records the
    full candidate list and truncation.
    """
    site = hashlib.sha1(
        f"{base.source}\x00{base.target}".encode("utf-8")).hexdigest()[:12]
    edges: list[Edge] = []
    seen_targets: set[str] = set()
    for tgt in targets:
        if tgt in seen_targets:
            continue
        seen_targets.add(tgt)
        edges.append(Edge(
            source=base.source, target=tgt, kind=base.kind,
            file_path=base.file_path, resolution="candidate",
            origin=base.origin, rule_id=base.rule_id,
            confidence=base.confidence,
            evidence_json={
                "candidates": targets[: _MAX_CANDIDATES],
                "truncated": len(targets) > _MAX_CANDIDATES,
            },
            site_id=site,
        ))
    return edges[: _MAX_CANDIDATES]


def _star_lookup(name: str, star_modules: list[str], mod_syms: dict,
                 module_alls: dict | None) -> list[str]:
    """Symbols named ``name`` across the modules a star import pulls in.

    A module's ``__all__`` (when statically declared) gates visibility; no
    ``__all__`` means every symbol is a candidate. Returns all hits so the
    caller can pick resolved (1) vs candidate (many) vs unresolved (0).
    """
    hits: list[str] = []
    for module in star_modules:
        syms = mod_syms.get(module) or {}
        allowed = (module_alls or {}).get(module)
        if allowed is not None and name not in allowed:
            continue
        if name in syms:
            hits.append(syms[name])
    return hits


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
    # Star imports (`from m import *` / `export * from m`) — a separate list per
    # module because multiple stars would collapse on `_import_map`'s "*" key.
    star_map = {pf.module_qname: [_module_of(imp.module, path_aliases, existing_qnames)
                                  for imp in pf.imports if imp.is_star]
                for pf in parsed_files
                if any(imp.is_star for imp in pf.imports)}
    module_alls = {pf.module_qname: pf.module_all
                   for pf in parsed_files if pf.module_all is not None}
    var_types = {qn: types for pf in parsed_files
                 for qn, types in pf.var_types.items()}
    class_qnames = {n.qualified_name for f in parsed_files for n in f.nodes if n.kind == "class"}
    edges: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    for pf in parsed_files:
        local = mod_syms.get(pf.module_qname, {})
        imports = _import_map(pf, path_aliases, existing_qnames)
        for c in pf.raw_calls:
            for edge in _resolve_one(c, local, imports, existing_qnames, all_import_maps,
                                     mod_syms=mod_syms, source_module=pf.module_qname,
                                     var_types=var_types, path_aliases=path_aliases,
                                     class_qnames=class_qnames, star_map=star_map,
                                     module_alls=module_alls):
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
                                               path_aliases=path_aliases,
                                               star_map=star_map,
                                               module_alls=module_alls):
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
                     path_aliases: dict[str, str] | None = None,
                     star_map: dict | None = None,
                     module_alls: dict | None = None) -> list[str]:
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
        for edge in _resolve_one(fake, local, imports, existing, all_import_maps,
                                 mod_syms=mod_syms, source_module=source_module,
                                 var_types=var_types, path_aliases=path_aliases,
                                 star_map=star_map, module_alls=module_alls):
            if edge.resolution == "resolved":
                resolved.append(edge.target)
    return resolved


def _module_member(mod: str, rest: str) -> tuple[str, str]:
    """The (target_module, member) a dotted walk over an imported module lands on.

    ``import pkg.b`` binds ``pkg`` to module ``pkg.b``, so the call ``pkg.b.fn()``
    walks the full module and takes member ``fn`` → (``pkg.b``, ``fn``), while the
    partial walk ``pkg.fn()`` consumes only the head and resolves on the parent
    package → (``pkg``, ``fn``). ``rest`` is consumed from the front only while
    its segments match the module's own path, so a reference that merely reaches
    the module (``pkg.b`` as a value) lands on the module itself with an empty
    member. Single-segment modules (plain ``import m``) leave ``rest`` on ``m``.
    """
    mod_parts = mod.split(".")
    rest_parts = rest.split(".")
    i = 0
    while (i < len(rest_parts) and i + 1 < len(mod_parts)
           and rest_parts[i] == mod_parts[i + 1]):
        i += 1
    member = ".".join(rest_parts[i:])
    return ".".join(mod_parts[:i + 1]), member


def _resolve_one(c: RawCall, local: dict, imports: dict,
                 existing: set[str], all_import_maps: dict,
                 mod_syms: dict | None = None,
                 source_module: str | None = None,
                 var_types: dict | None = None,
                 path_aliases: dict[str, str] | None = None,
                 class_qnames: set[str] | None = None,
                 star_map: dict | None = None,
                 module_alls: dict | None = None,
                 default_exports: dict | None = None) -> list[Edge]:
    base = Edge(source=c.source_qname, target=c.target_expr, kind="call",
                file_path=c.file_path, resolution="unresolved")
    if c.language == "java":
        return _resolve_java(c, local, imports, existing, mod_syms,
                             source_module, base, var_types,
                             class_qnames=class_qnames, star_map=star_map,
                             module_alls=module_alls, default_exports=default_exports)
    star_modules = (star_map or {}).get(source_module, []) if source_module else []
    if c.call_form == CALL_SIMPLE:
        name = c.target_expr
        if name in local:
            return [_resolved(base, local[name], existing)]
        if name in imports:
            mod, imp_name, _star = imports[name]
            mod = _module_of(mod, path_aliases, existing)  # alias @/x -> real module
            if imp_name:  # from m import name
                tgt = qname.join(mod, imp_name)
                if tgt not in existing:
                    tgt = _resolve_reexport(mod, imp_name, all_import_maps,
                                            existing, star_map=star_map) or tgt
                return [_resolved(base, tgt, existing)]
            return [_resolved(base, mod, existing)]  # imported module itself
        if star_modules:  # from m import * -> unique / multi-candidate / unresolved
            hits = _star_lookup(name, star_modules, mod_syms, module_alls)
            if len(hits) == 1:
                return [_resolved(base, hits[0], existing)]
            if len(hits) > 1:
                return _candidates(base, hits)
        return [base]  # unresolved
    if c.call_form == CALL_ATTRIBUTE:
        head = c.target_expr.split(".", 1)[0]
        rest = c.target_expr[len(head) + 1:]
        if head in imports:
            mod, imp_name, _ = imports[head]
            mod = _module_of(mod, path_aliases, existing)  # alias @/x -> real module
            if imp_name is None:  # import m / import m as head -> m.rest
                target_mod, member = _module_member(mod, rest)
                tgt = qname.join(target_mod, member) if member else target_mod
                if tgt not in existing:
                    tgt = (_resolve_reexport(target_mod, member, all_import_maps, existing,
                                             star_map=star_map)
                           if member else None) or tgt
                return [_resolved(base, tgt, existing)]
        if head in local and local[head] in existing:
            cls_qn = local[head]
            tgt = _join_target(cls_qn, rest)
            return [_resolved(base, tgt, existing)]
        if c.language == "python" and head in ("self", "cls"):
            # method receiver -> enclosing class, mirroring Java's this./type
            # binding; bare `g()` stays module-scope (Python LEGB), not A.g
            enclosing = _enclosing_class(c.source_qname, class_qnames)
            if enclosing:
                tgt = _join_target(enclosing, rest)
                if tgt in existing:
                    return [_resolved(base, tgt, existing)]
        if star_modules:  # helper.run() where helper came from `from m import *`
            hits = _star_lookup(head, star_modules, mod_syms, module_alls)
            if len(hits) == 1:
                tgt = _join_target(hits[0], rest)
                return [_resolved(base, tgt, existing)]
            if len(hits) > 1:
                return _candidates(base, [_join_target(hit, rest) for hit in hits])
        return [_mark_dynamic(base, c.call_form, c.target_expr)]
    return [base]  # other -> unresolved


def _resolve_reexport(current: str, name: str, all_import_maps: dict,
                      existing: set[str], seen: set[str] | None = None,
                      star_map: dict | None = None) -> str | None:
    """Follow a module's own import bindings to where `name` is re-exported from.

    binding is (module, imported_name, is_star); imported_name is the EXPORTED
    name (aliases like `from .m import X as Y` make it differ from the local
    name), so recursion carries binding[1], not `name`. When no single binding
    names it, the module's star re-exports (`export * from` / `from m import *`)
    are probed — a barrel can re-export many modules at once.
    """
    tgt = qname.join(current, name)
    if tgt in existing:
        return tgt
    if current in (seen or set()):
        return None  # import cycle
    binding = (all_import_maps.get(current) or {}).get(name)
    if binding and binding[1]:
        return _resolve_reexport(binding[0], binding[1], all_import_maps,
                                 existing, (seen or set()) | {current}, star_map)
    for module in (star_map or {}).get(current, []):
        hit = _resolve_reexport(module, name, all_import_maps, existing,
                                (seen or set()) | {current}, star_map)
        if hit:
            return hit
    return None  # no binding / module import / unresolved star re-export


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


def _enclosing_class(qualified_name: str,
                     class_qnames: set[str] | None = None) -> str | None:
    """The innermost class enclosing a method qname, or None.

    ``Outer.Inner.m`` calling ``self.g()`` binds to ``Outer.Inner``, not the
    outermost ``Outer`` — the scope chain is walked longest-prefix-first and the
    first prefix that is a known class wins (function scopes in between, e.g. a
    closure ``C.m.inner``, are skipped so it still binds to ``C``). Without
    ``class_qnames`` the previous behaviour is kept: the first scope of the
    qname.
    """
    mod = qname.module(qualified_name)
    rest = qualified_name[len(mod) + len(qname.MODULE_SEP):]
    if not rest:
        return None
    scopes = rest.split(qname.SCOPE_SEP)
    if class_qnames:
        # longest scope prefix (excluding the leaf) that is a known class
        for i in range(len(scopes) - 1, 0, -1):
            prefix = qname.join(mod, qname.SCOPE_SEP.join(scopes[:i]))
            if prefix in class_qnames:
                return prefix
        return None
    first_scope = scopes[0]
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
                  base: Edge, var_types: dict | None = None,
                  class_qnames: set[str] | None = None,
                  star_map: dict | None = None,
                  module_alls: dict | None = None,
                  default_exports: dict | None = None) -> list[Edge]:
    """Java-aware call resolution: simple / attribute / construct forms."""
    if c.call_form == CALL_SIMPLE:
        name = c.target_expr
        if name in local:
            return [_resolved(base, local[name], existing)]
        if name in imports:
            mod, imported, _star = imports[name]
            if imported:
                return [_resolved(base, _join_target(mod, imported), existing)]
            return [_resolved(base, mod, existing)]
        if mod_syms and source_module:
            same_pkg = mod_syms.get(source_module, {})
            if name in same_pkg:
                return [_resolved(base, same_pkg[name], existing)]
        enclosing = _enclosing_class(c.source_qname, class_qnames)
        if enclosing:
            target = _join_target(enclosing, name)
            if target in existing:
                return [_resolved(base, target, existing)]
        return [base]
    if c.call_form == CALL_ATTRIBUTE:
        expr = c.target_expr
        if expr.startswith("this."):
            expr = expr[len("this."):]
        head, sep, rest = expr.partition(".")
        if not sep:
            return [_mark_dynamic(base, c.call_form, c.target_expr)]
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
                        return [_resolved(base, target, existing)]
        if head in imports:
            mod, imported, _star = imports[head]
            if imported:
                class_qn = _join_target(mod, imported)
                return [_resolved(base, _join_target(class_qn, rest), existing)]
            return [_resolved(base, _join_target(mod, rest), existing)]
        if head in local and local[head] in existing:
            return [_resolved(base, _join_target(local[head], rest), existing)]
        if mod_syms:
            target = _resolve_java_dotted(c.target_expr, mod_syms, existing)
            if target:
                return [_resolved(base, target, existing)]
        return [_mark_dynamic(base, c.call_form, c.target_expr)]
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
                return [_resolved(base, candidate, existing)]
        return [base]
    return [base]  # CALL_OTHER -> unresolved


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
                origin="module",
            ))
    return edges


def _resolve_inherit_base(base_expr: str, imports: dict,
                          mod_syms: dict | None) -> str | None:
    """Resolve a base-class expression through the file's imports.

    Covers both ``import pkg; class User(pkg.Base)`` (module-alias head) and
    ``from pkg import Base; class User(Base)`` (direct import). The resolved
    qname is returned only if it exists in the graph — callers verify.
    """
    if base_expr in imports:
        mod, imported, _is_star = imports[base_expr]
        candidate = qname.join(mod, imported) if imported else mod
        return candidate
    head, sep, rest = base_expr.partition(".")
    if sep and head in imports:
        mod, _imported, _is_star = imports[head]
        return qname.join(mod, rest)
    return None


def _build_inherits(parsed: list[ParsedFile], qnames: set[str],
                    all_import_maps: dict | None = None,
                    mod_syms: dict | None = None) -> list[Edge]:
    """INHERITS edges: subclass → base class / interface.

    A base_expr resolves same-module first; a cross-module base then goes
    through the file's import map (``import pkg; class User(pkg.Base)`` /
    ``from pkg import Base``), so inheritance closure follows real modules
    instead of dropping straight to unresolved.
    """
    edges: list[Edge] = []
    for pf in parsed:
        imports = (all_import_maps or {}).get(pf.module_qname, {})
        for ih in pf.inherits:
            tgt = ih.base_expr
            resolved = tgt in qnames
            if not resolved and "::" not in tgt:
                scoped = qname.join(pf.module_qname, tgt)
                if scoped in qnames:
                    tgt = scoped
                    resolved = True
            if not resolved:
                candidate = _resolve_inherit_base(tgt, imports, mod_syms)
                if candidate and candidate in qnames:
                    tgt = candidate
                    resolved = True
            edges.append(Edge(
                source=ih.class_qname, target=tgt, kind=ih.relation,
                file_path=pf.file_path,
                resolution="resolved" if resolved else "unresolved",
                origin="type",
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
                file_path=pf.file_path, resolution="resolved",
                origin="type",
                rule_id="JAVA-F04" if decl.mechanism == "constructor" else "JAVA-F05",
                evidence_json={
                    "mechanism": decl.mechanism,
                    "dep_type": decl.dep_expr,
                    "annotations": decl.annotations,
                }))
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
    mod_syms = _module_symbols(parsed)
    import_maps = {pf.module_qname: _import_map(pf, path_aliases, existing_qnames)
                   for pf in parsed}
    edges.extend(_build_inherits(parsed, existing_qnames, import_maps, mod_syms))
    edges.extend(_build_di_edges(parsed, existing_qnames, mod_syms,
                                 import_maps, di_annotations))
    from code_review_ai.java_routing import build_route_edges
    edges.extend(build_route_edges(parsed, existing_qnames))
    return edges
