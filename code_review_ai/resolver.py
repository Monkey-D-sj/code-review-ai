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
            if n.kind in ("function", "class", "object", "enum", "namespace"):
                syms[qname.short(n.qualified_name)] = n.qualified_name
    return out


def _scope_symbols(parsed_files: list[ParsedFile]) -> dict[str, dict[str, str]]:
    """Build lexical-scope symbol tables for nested functions/classes.

    ``_module_symbols`` intentionally remains module-only because it is also
    used for imports and wildcard lookup.  Call resolution needs a second
    table: a nested function must see definitions in its own body, then its
    enclosing function/class, then the module.  Keeping this separate avoids
    turning same-named symbols in sibling scopes into false resolved edges.
    """
    scopes: dict[str, dict[str, str]] = {}
    for pf in parsed_files:
        module = pf.module_qname
        for node in pf.nodes:
            if node.kind not in ("function", "class", "method", "object", "enum", "namespace"):
                continue
            parent = node.parent_qname or module
            scopes.setdefault(parent, {})[qname.short(node.qualified_name)] = node.qualified_name
    return scopes


def _lexical_symbols(source_qname: str, module_qname: str,
                     scopes: dict[str, dict[str, str]],
                     module_symbols: dict[str, str]) -> dict[str, str]:
    """Return nearest-scope-first bindings visible at ``source_qname``."""
    result: dict[str, str] = {}
    chain: list[str] = [source_qname]
    current = source_qname
    while current and current != module_qname:
        if "::" in current:
            parent = current.rsplit(".", 1)[0] if "." in current.split("::", 1)[1] else module_qname
        else:
            parent = module_qname
        if parent == current:
            break
        chain.append(parent)
        current = parent
    chain.append(module_qname)
    for scope in chain:
        for name, target in scopes.get(scope, {}).items():
            result.setdefault(name, target)
    for name, target in module_symbols.items():
        result.setdefault(name, target)
    return result


def _import_map(pf: ParsedFile, path_aliases: dict[str, str] | None = None,
                existing: set[str] | None = None,
                base_url: str = "") -> dict:
    """local_name -> (module, imported_name_or_None, is_star).

    The module string is canonicalized through path_aliases and tsconfig
    baseUrl when it resolves to a real module qname (so `@/x` -> `x` and a bare
    `components/Button` -> `components.Button` consistently across call
    resolution and re-export traversal); otherwise the raw specifier is kept.
    """
    if existing is None:
        return {i.local_name: (i.module, i.imported_name, i.is_star)
                for i in pf.imports}
    return {i.local_name: (_import_module(i.module, path_aliases, base_url, existing),
                           i.imported_name, i.is_star) for i in pf.imports}


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


def _path_parts_to_module(path: str, existing: set[str]) -> str | None:
    """Path → module qname, or None when the module isn't in the graph.

    Mirrors parser._module_qname: strip a known source suffix, drop a leading
    ``src/``, join with dots. Returning None lets callers keep the raw
    specifier on unresolved edges instead of inventing phantom modules.
    """
    parts = [part for part in path.split("/") if part not in ("", ".")]
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


def _spec_to_module(spec: str, path_aliases: dict[str, str] | None,
                    existing: set[str]) -> str | None:
    """Resolve an alias-prefixed import specifier to an existing module qname,
    or None.

    Only specifiers that actually match an alias prefix are re-derived, so
    relative/bare imports are left untouched. Returns None when the alias
    target has no module in the graph, so callers keep the raw specifier on
    their unresolved edges.
    """
    matched, norm = _alias_replaced(spec, path_aliases)
    if not matched or "::" in norm:
        return None
    return _path_parts_to_module(norm, existing)


def _import_module(spec: str, path_aliases: dict[str, str] | None,
                   base_url: str, existing: set[str]) -> str:
    """Canonical module qname for an import specifier (alias + baseUrl).

    path_aliases map a prefix to a dir; baseUrl resolves bare specifiers under
    ``<baseUrl>/`` when that module exists. Already-canonical module qnames
    (the parser resolves relative specs at parse time) are never re-mapped:
    a spec already in `existing` and a relative ``./``/``../`` specifier are
    left untouched. Returns the raw specifier when neither channel resolves.
    """
    matched, norm = _alias_replaced(spec, path_aliases)
    if matched:
        return _path_parts_to_module(norm, existing) or spec
    if base_url and not spec.startswith((".", "/")) and spec not in existing:
        return _path_parts_to_module(base_url.rstrip("/") + "/" + spec,
                                     existing) or spec
    return spec


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


def _attach_call_site(c: RawCall, edge: Edge) -> None:
    """Stamp where/how a resolved call happens onto the edge's evidence.

    The graph query surface already sends a neighbor's signature; the call
    site (line + argument texts) is what lets a reviewer judge a contract
    change — e.g. a newly required argument — without opening the caller's
    file. Only filled when the resolver produced no richer evidence (dynamic
    and candidate edges keep their own)."""
    if edge.kind != "call" or edge.resolution != "resolved":
        return
    if edge.evidence_json is not None:
        return
    evidence: dict = {"call_form": c.call_form}
    if c.span is not None:
        evidence["call_line"] = c.span.start_line
    if c.args:
        evidence["args"] = list(c.args)
    edge.evidence_json = evidence


def resolve_calls(parsed_files: list[ParsedFile], existing_qnames: set[str],
                  path_aliases: dict[str, str] | None = None,
                  dependency_markers: list[str] | None = None,
                  base_url: str = "",
                  inheritance_edges: list[Edge] | None = None) -> list[Edge]:
    mod_syms = _module_symbols(parsed_files)
    scope_syms = _scope_symbols(parsed_files)
    # Per-file import maps, built once per file. ``all_import_maps`` keys by
    # module and keeps the last file's map per package (Java: several files
    # share a package) — that module-level view feeds re-export traversal; the
    # resolve loop below consumes the per-file list directly, so each file
    # resolves against its own imports.
    per_file_imports = [_import_map(pf, path_aliases, existing_qnames, base_url)
                        for pf in parsed_files]
    all_import_maps = {pf.module_qname: imap
                       for pf, imap in zip(parsed_files, per_file_imports)}
    # Star imports (`from m import *` / `export * from m`) — a separate list per
    # module because multiple stars would collapse on `_import_map`'s "*" key.
    star_map = {pf.module_qname: [_import_module(imp.module, path_aliases, base_url,
                                                 existing_qnames)
                                  for imp in pf.imports if imp.is_star]
                for pf in parsed_files
                if any(imp.is_star for imp in pf.imports)}
    module_alls = {pf.module_qname: pf.module_all
                   for pf in parsed_files if pf.module_all is not None}
    default_exports = {pf.module_qname: pf.default_export
                       for pf in parsed_files if pf.default_export}
    var_types = {qn: types for pf in parsed_files
                 for qn, types in pf.var_types.items()}
    return_types = {qn: type_name for pf in parsed_files
                    for qn, type_name in pf.return_types.items()}
    class_qnames = {n.qualified_name for f in parsed_files for n in f.nodes if n.kind == "class"}
    if inheritance_edges is None:
        inheritance_edges = _build_inherits(parsed_files, existing_qnames,
                                             path_aliases, base_url, mod_syms)
    inheritance_map = {}
    for edge in inheritance_edges:
        if edge.resolution == "resolved" and edge.kind == "extends":
            inheritance_map.setdefault(edge.source, []).append(edge.target)
    edges: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    # Per-build caches — they must live for exactly one resolve_edges call,
    # because existing_qnames/class_qnames change between builds (a module-level
    # cache would go stale). All three memoize pure lookups over build-fixed
    # tables, so results are identical to the uncached path.
    lex_cache: dict[str, dict] = {}              # source_qname -> lexical scope dict
    enclosing_cache: dict[str, str | None] = {}  # qname -> enclosing class
    reexport_memo: dict[tuple[str, str], list[str]] = {}
    for pf, imports in zip(parsed_files, per_file_imports):
        module_local = mod_syms.get(pf.module_qname, {})
        # Star imports are per-file (Java packages hold several files whose
        # wildcard imports must not cross-contaminate), so thread the file's
        # own list down instead of the module-aggregated star_map.
        star_modules = [imp.module for imp in pf.imports if imp.is_star]
        for c in pf.raw_calls:
            local = lex_cache.get(c.source_qname)
            if local is None:
                local = _lexical_symbols(c.source_qname, pf.module_qname,
                                         scope_syms, module_local)
                lex_cache[c.source_qname] = local
            for edge in _resolve_one(c, local, imports, existing_qnames, all_import_maps,
                                     mod_syms=mod_syms, source_module=pf.module_qname,
                                     var_types=var_types, path_aliases=path_aliases,
                                     class_qnames=class_qnames, star_map=star_map,
                                     module_alls=module_alls,
                                     default_exports=default_exports,
                                     star_modules=star_modules,
                                     inheritance_map=inheritance_map,
                                     return_types=return_types,
                                     reexport_memo=reexport_memo,
                                     enclosing_cache=enclosing_cache):
                _attach_call_site(c, edge)
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
                                               module_alls=module_alls,
                                               reexport_memo=reexport_memo):
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
    if language in ("typescript", "javascript"):
        member = "constructor"
    elif language == "java":
        member = qname.short(class_qn)
    else:
        member = "__init__"
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
                     module_alls: dict | None = None,
                     reexport_memo: dict | None = None) -> list[str]:
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
                                 star_map=star_map, module_alls=module_alls,
                                 reexport_memo=reexport_memo):
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
                 default_exports: dict | None = None,
                 star_modules: list | None = None,
                 inheritance_map: dict[str, list[str]] | None = None,
                 return_types: dict[str, str] | None = None,
                 reexport_memo: dict | None = None,
                 enclosing_cache: dict | None = None) -> list[Edge]:
    base = Edge(source=c.source_qname, target=c.target_expr, kind="call",
                file_path=c.file_path, resolution="unresolved")
    if c.language == "java":
        return _resolve_java(c, local, imports, existing, mod_syms,
                             source_module, base, var_types,
                             class_qnames=class_qnames, star_map=star_map,
                             module_alls=module_alls, default_exports=default_exports,
                             star_modules=star_modules,
                             inheritance_map=inheritance_map,
                             return_types=return_types,
                             enclosing_cache=enclosing_cache)
    if star_modules is None:
        star_modules = (star_map or {}).get(source_module, []) if source_module else []
    if c.call_form == CALL_CONSTRUCT:
        name = c.target_expr
        if name in local:
            return [_resolved(base, local[name], existing)]
        if name in imports:
            mod, imported, _star = imports[name]
            mod = _module_of(mod, path_aliases, existing)
            target = qname.join(mod, imported) if imported else mod
            if imported == "default":
                target = (default_exports or {}).get(mod, target)
            return [_resolved(base, target, existing)]
        return [base]
    if c.call_form == CALL_SIMPLE:
        name = c.target_expr
        if name in local:
            return [_resolved(base, local[name], existing)]
        if name in imports:
            mod, imp_name, _star = imports[name]
            mod = _module_of(mod, path_aliases, existing)  # alias @/x -> real module
            if imp_name:  # from m import name
                if imp_name == "default":
                    # TS/JS default import binds to the module's export default
                    tgt = (default_exports or {}).get(mod)
                    return [_resolved(base, tgt, existing)] if tgt else [base]
                tgt = qname.join(mod, imp_name)
                if tgt not in existing:
                    hits = _resolve_reexport(mod, imp_name, all_import_maps,
                                             existing, star_map=star_map,
                                             memo=reexport_memo)
                    if len(hits) == 1:
                        tgt = hits[0]
                    elif len(hits) > 1:  # barrel re-exports it from several modules
                        return _candidates(base, hits)
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
        # TypeScript's optional/member syntax still has a deterministic target
        # when the receiver is statically known: ``owner?.run()`` and
        # ``owner["run"]()`` are the same member lookup for graph purposes.
        # Keep the original expression in evidence, but normalize only these
        # constant-property forms before applying normal receiver binding.
        target_expr = c.target_expr
        if c.language in ("typescript", "javascript") and (
                "?" in target_expr or "[" in target_expr):
            # parser._call_target already normalises `?.` and `["x"]` on the
            # path from source, so this regex is a no-op for parser-produced
            # calls. The gate (only exprs actually carrying `?`/`[`) keeps the
            # fallback for directly-constructed RawCalls while skipping the
            # regex on the overwhelmingly common plain-dotted expression.
            target_expr = target_expr.replace("?.", ".")
            match = re.fullmatch(r"(.+)\[['\"]([A-Za-z_$][A-Za-z0-9_$]*)['\"]\]",
                                 target_expr)
            if match:
                target_expr = f"{match.group(1)}.{match.group(2)}"
        if c.language == "python":
            # A method called directly on a freshly constructed object has a
            # statically known receiver even though the source text contains
            # no variable annotation: ``Service(auth, db).create()``.  Treat
            # only a simple named constructor as deterministic; arbitrary
            # factories such as ``get_service().create()`` remain dynamic.
            constructed = re.fullmatch(
                r"([A-Za-z_][A-Za-z0-9_]*)\(.*\)\."
                r"([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
                target_expr,
                flags=re.DOTALL,
            )
            if constructed:
                receiver_type, member = constructed.groups()
                hits = _resolve_py_type(
                    receiver_type, local, imports, existing, all_import_maps,
                    mod_syms, source_module, star_modules, star_map,
                    module_alls, default_exports,
                    reexport_memo=reexport_memo,
                )
                hits = [hit for hit in hits if hit in (class_qnames or set())]
                targets = [_join_target(hit, member) for hit in hits]
                targets = [target for target in targets if target in existing]
                if len(targets) == 1:
                    return [_resolved(base, targets[0], existing)]
                if len(targets) > 1:
                    return _candidates(base, targets)
        if c.language in ("typescript", "javascript"):
            class_qn = _enclosing_class_qname_cached(c.source_qname, class_qnames,
                                                     enclosing_cache)
            if class_qn and target_expr.startswith("this."):
                target = _join_target(class_qn, target_expr[len("this."):])
                if target in existing:
                    return [_resolved(base, target, existing)]
        elif c.language == "python" and c.target_expr.startswith("super()."):
            class_qn = _enclosing_class_qname_cached(c.source_qname, class_qnames,
                                                     enclosing_cache)
        else:
            class_qn = None
        super_prefix = "super()." if c.language == "python" else "super."
        if class_qn and c.target_expr.startswith(super_prefix):
            member = c.target_expr[len(super_prefix):]
            pending = list((inheritance_map or {}).get(class_qn, []))
            seen_classes: set[str] = set()
            targets: list[str] = []
            while pending:
                parent = pending.pop(0)
                if parent in seen_classes:
                    continue
                seen_classes.add(parent)
                target = _join_target(parent, member)
                if target in existing:
                    if target not in targets:
                        targets.append(target)
                    continue
                pending.extend((inheritance_map or {}).get(parent, []))
            if len(targets) == 1:
                return [_resolved(base, targets[0], existing)]
            if len(targets) > 1:
                return _candidates(base, targets)
        head = target_expr.split(".", 1)[0]
        rest = target_expr[len(head) + 1:]
        if head not in imports:
            # CJS require("mod").foo() — the require expression is keyed as its
            # own import binding, so the receiver match uses the LAST dot (the
            # specifier string itself contains dots).
            receiver, _sep, member = target_expr.rpartition(".")
            if receiver and receiver in imports:
                head, rest = receiver, member
        if head in imports:
            mod, imp_name, _ = imports[head]
            mod = _module_of(mod, path_aliases, existing)  # alias @/x -> real module
            if imp_name is None:  # import m / import m as head -> m.rest
                target_mod, member = _module_member(mod, rest)
                tgt = qname.join(target_mod, member) if member else target_mod
                if tgt not in existing and member:
                    hits = _resolve_reexport(target_mod, member, all_import_maps,
                                             existing, star_map=star_map,
                                             memo=reexport_memo)
                    if len(hits) == 1:
                        tgt = hits[0]
                    elif len(hits) > 1:
                        return _candidates(base, hits)
                return [_resolved(base, tgt, existing)]
            if imp_name == "default":
                # default import receiver: foo.bar() -> <default-export>.bar
                default_qn = (default_exports or {}).get(mod)
                if default_qn:
                    tgt = _join_target(default_qn, rest)
                    if tgt in existing:
                        return [_resolved(base, tgt, existing)]
        if head in local and local[head] in existing:
            cls_qn = local[head]
            tgt = _join_target(cls_qn, rest)
            return [_resolved(base, tgt, existing)]
        # receiver declared type (PY-M12): `w.run()` / `self.w.run()` where the
        # receiver variable is annotated — the declared type binds the target.
        # After lexical scope + imports per §4.4; untyped/union receivers keep
        # falling through to dynamic.
        if var_types:
            scope_types = var_types.get(c.source_qname, {})
            receiver_expr, receiver_type = _receiver_declared_type(
                target_expr, scope_types)
            if receiver_expr:
                hits = _resolve_py_type(
                    receiver_type, local, imports, existing, all_import_maps,
                    mod_syms, source_module, star_modules, star_map,
                    module_alls, default_exports, reexport_memo=reexport_memo)
                receiver_member = target_expr[len(receiver_expr) + 1:]
                if len(hits) == 1:
                    tgt = _join_target(hits[0], receiver_member)
                    if tgt in existing:
                        return [_resolved(base, tgt, existing)]
                elif len(hits) > 1:
                    return _candidates(base, [_join_target(hit, receiver_member)
                                              for hit in hits])
        if c.language == "python" and head in ("self", "cls"):
            # method receiver -> enclosing class, mirroring Java's this./type
            # binding; bare `g()` stays module-scope (Python LEGB), not A.g
            enclosing = _enclosing_class_cached(c.source_qname, class_qnames,
                                                enclosing_cache)
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
                      star_map: dict | None = None,
                      memo: dict | None = None) -> list[str]:
    """All qnames `name` is re-exported to from `current`.

    binding is (module, imported_name, is_star); imported_name is the EXPORTED
    name (aliases like `from .m import X as Y` make it differ from the local
    name), so recursion carries binding[1], not `name`. A single binding chain
    resolves to at most one qname; when no single binding names it, the module's
    star re-exports (`export * from` / `from m import *`) are all probed — a
    barrel can re-export many modules at once, and the caller decides resolved
    (1) vs candidate (many) vs unresolved (0).

    ``memo`` is a per-build cache: the result is a pure function of
    (current, name) over the fixed re-export graph — the ``seen`` cycle guard
    only prunes infinite recursion and cannot change the reachable set — so
    keying by (current, name) is safe. Returns a copy so callers never mutate
    the cached list.
    """
    if memo is not None and (current, name) in memo:
        return list(memo[(current, name)])
    tgt = qname.join(current, name)
    if tgt in existing:
        result = [tgt]
    elif current in (seen or set()):
        result = []  # import cycle
    else:
        binding = (all_import_maps.get(current) or {}).get(name)
        if binding and binding[1]:
            next_module = binding[0]
            # CJS `exports.wrap = helper.run`: "helper" is a local require
            # alias, not a module qname — resolve it through the current
            # module's import map (helper -> util) before recursing, or the
            # chain dead-ends.
            if next_module not in all_import_maps:
                alias = (all_import_maps.get(current) or {}).get(next_module)
                if alias:
                    next_module = alias[0]
            result = _resolve_reexport(next_module, binding[1], all_import_maps,
                                       existing, (seen or set()) | {current},
                                       star_map, memo)
        else:
            result = []
            for module in (star_map or {}).get(current, []):
                result.extend(_resolve_reexport(module, name, all_import_maps,
                                                existing,
                                                (seen or set()) | {current},
                                                star_map, memo))
    if memo is not None:
        memo[(current, name)] = result
    return result


def _enclosing_class_qname(source_qname: str,
                           class_qnames: set[str] | None) -> str | None:
    """Return the most-specific class containing a method qname."""
    matches = [candidate for candidate in (class_qnames or set())
               if source_qname.startswith(candidate + ".")]
    return max(matches, key=len) if matches else None


def _resolve_py_type(type_name: str, local: dict, imports: dict,
                     existing: set[str], all_import_maps: dict,
                     mod_syms: dict, source_module: str | None,
                     star_modules: list, star_map: dict | None,
                     module_alls: dict | None,
                     default_exports: dict | None,
                     reexport_memo: dict | None = None) -> list[str]:
    """All class qnames a Python/TS declared type resolves to: same-module
    symbol, then import (following barrel re-exports), then star imports.

    Callers pick resolved (1) vs candidate (many) vs unresolved (0) — a
    multi-hit barrel must never silently pick the first class.
    """
    hits: list[str] = []
    if type_name in local:
        hits.append(local[type_name])
    if type_name in imports:
        mod, imported, _star = imports[type_name]
        if imported == "default":
            default_qn = (default_exports or {}).get(mod)
            if default_qn and default_qn not in hits:
                hits.append(default_qn)
        elif imported:
            candidate = qname.join(mod, imported)
            if candidate in existing and candidate not in hits:
                hits.append(candidate)
            else:
                for hit in _resolve_reexport(mod, imported, all_import_maps,
                                             existing, star_map=star_map,
                                             memo=reexport_memo):
                    if hit not in hits:
                        hits.append(hit)
    if not hits and star_modules:
        hits = _star_lookup(type_name, star_modules, mod_syms, module_alls)
    return hits


def _receiver_declared_type(target_expr: str,
                            scope_types: dict) -> tuple[str | None, str | None]:
    """The longest var_types prefix of a dotted call expression, if any.

    ``w.run()`` → (``w``, ``Widget``); ``self.w.run()`` / ``this.w.run()`` →
    (``self.w``/``this.w``, ``Widget``). Longest prefix wins so a field-access
    chain binds to the field's declared type rather than the bare receiver
    name. Returns (None, None) when no prefix is a declared variable.
    """
    segments = target_expr.split(".")
    for index in range(len(segments) - 1, 0, -1):
        prefix = ".".join(segments[:index])
        if prefix in scope_types:
            return prefix, scope_types[prefix]
    return None, None


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


def _enclosing_class_qname_cached(source_qname: str,
                                  class_qnames: set[str] | None,
                                  cache: dict[str, str | None] | None) -> str | None:
    """_enclosing_class_qname memoized per build (class_qnames is build-fixed)."""
    if cache is None:
        return _enclosing_class_qname(source_qname, class_qnames)
    if source_qname not in cache:
        cache[source_qname] = _enclosing_class_qname(source_qname, class_qnames)
    return cache[source_qname]


def _enclosing_class_cached(qualified_name: str, class_qnames: set[str] | None,
                            cache: dict[str, str | None] | None) -> str | None:
    """_enclosing_class memoized per build (class_qnames is build-fixed)."""
    if cache is None:
        return _enclosing_class(qualified_name, class_qnames)
    if qualified_name not in cache:
        cache[qualified_name] = _enclosing_class(qualified_name, class_qnames)
    return cache[qualified_name]


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
                       imports: dict, mod_syms: dict | None,
                       star_modules: list | None = None) -> list[str]:
    """All class qnames a Java type name resolves to: same-package class, then
    import, then wildcard imports (`import a.b.*`). Callers pick resolved (1)
    vs candidate (many) vs unresolved (0)."""
    hits: list[str] = []
    if mod_syms and source_module:
        same_pkg = mod_syms.get(source_module, {})
        if type_name in same_pkg:
            hits.append(same_pkg[type_name])
        # Nested Java classes are represented by a scoped type such as
        # ``FlowTarget.Inner``. Resolve the outer class from the package
        # symbol table, then retain the nested scope in the qualified name.
        parts = type_name.split(".")
        for split in range(len(parts) - 1, 0, -1):
            outer = ".".join(parts[:split])
            if outer not in same_pkg:
                continue
            nested = _join_target(same_pkg[outer], ".".join(parts[split:]))
            if nested not in hits:
                hits.append(nested)
            break
    if type_name in imports:
        mod, imported, _star = imports[type_name]
        candidate = _join_target(mod, imported) if imported else mod
        if candidate not in hits:  # same-package + import often name the same class
            hits.append(candidate)
    if not hits and star_modules:
        for module in star_modules:
            syms = (mod_syms or {}).get(module) or {}
            if type_name in syms:
                hits.append(syms[type_name])
    return hits


def _inherited_member(class_qn: str, member: str, existing: set[str],
                      inheritance_map: dict[str, list[str]] | None) -> str | None:
    """Nearest ancestor declaring `member`, via the extends chain.

    Mirrors Java method resolution for an inherited call: walk the direct
    parents of `class_qn` (BFS, most-derived first) and return the first class
    that declares `member` in `existing`. The chain is linear for classes (one
    superclass each), so first-hit is the Java-correct answer; the super.-branch
    keeps its own collect-all BFS for the multi-parent interface case. None when
    no ancestor declares it.
    """
    pending = list((inheritance_map or {}).get(class_qn, []))
    seen: set[str] = {class_qn}
    while pending:
        parent = pending.pop(0)
        if parent in seen:
            continue
        seen.add(parent)
        target = _join_target(parent, member)
        if target in existing:
            return target
        pending.extend((inheritance_map or {}).get(parent, []))
    return None


def _resolve_java(c, local: dict, imports: dict, existing: set[str],
                  mod_syms: dict | None, source_module: str | None,
                  base: Edge, var_types: dict | None = None,
                  class_qnames: set[str] | None = None,
                  star_map: dict | None = None,
                  module_alls: dict | None = None,
                  default_exports: dict | None = None,
                  star_modules: list | None = None,
                  inheritance_map: dict[str, list[str]] | None = None,
                  return_types: dict[str, str] | None = None,
                  enclosing_cache: dict | None = None) -> list[Edge]:
    """Java-aware call resolution: simple / attribute / construct forms.

    star_modules is the *per-file* wildcard-import list (Java packages hold
    several files whose `import a.b.*` must not cross-contaminate); callers
    without the per-file view fall back to the module-aggregated star_map."""
    if star_modules is None:
        star_modules = ((star_map or {}).get(source_module, [])
                        if source_module else [])
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
        enclosing = _enclosing_class_cached(c.source_qname, class_qnames,
                                            enclosing_cache)
        if enclosing:
            target = _join_target(enclosing, name)
            if target in existing:
                return [_resolved(base, target, existing)]
            inherited = _inherited_member(enclosing, name, existing,
                                          inheritance_map)
            if inherited:
                return [_resolved(base, inherited, existing)]
        if star_modules:  # import a.b.* -> unique / candidate / unresolved
            hits = _star_lookup(name, star_modules, mod_syms, module_alls)
            if len(hits) == 1:
                return [_resolved(base, hits[0], existing)]
            if len(hits) > 1:
                return _candidates(base, hits)
        return [base]
    if c.call_form == CALL_ATTRIBUTE:
        expr = c.target_expr
        # Anonymous class calls have no source-level class name. The parser
        # gives their methods a stable synthetic scope under the caller.
        if c.language == "java":
            anonymous_member = re.search(r"\}\.(\w+)$", expr.strip())
            if anonymous_member:
                target = _join_target(
                    c.source_qname, anonymous_member.group(1))
                if target in existing:
                    return [_resolved(base, target, existing)]
        # A method invocation used as a receiver is a statically resolvable
        # two-hop chain when the inner method has a declared return type:
        # ``factory.create().run()``.  Resolve the inner call first, then bind
        # its declared return class.  Unknown/inferred returns intentionally
        # stay unresolved rather than being guessed.
        if c.language == "java":
            chain = re.fullmatch(r"(.+)\(\)\.(\w+)", expr.strip())
            if chain and return_types:
                inner_expr, member = chain.groups()
                inner_form = (CALL_ATTRIBUTE if "." in inner_expr
                              else CALL_SIMPLE)
                inner = RawCall(
                    source_qname=c.source_qname,
                    target_expr=inner_expr,
                    call_form=inner_form,
                    file_path=c.file_path,
                    language="java",
                )
                inner_edges = _resolve_java(
                    inner, local, imports, existing, mod_syms, source_module,
                    base, var_types, class_qnames=class_qnames,
                    star_map=star_map, module_alls=module_alls,
                    default_exports=default_exports,
                    star_modules=star_modules,
                    inheritance_map=inheritance_map,
                    return_types=return_types,
                    enclosing_cache=enclosing_cache,
                )
                targets: list[str] = []
                for inner_edge in inner_edges:
                    if inner_edge.resolution != "resolved":
                        continue
                    return_type = return_types.get(inner_edge.target)
                    if not return_type:
                        continue
                    hits = _resolve_java_type(
                        return_type, source_module, imports, mod_syms,
                        star_modules)
                    for hit in hits:
                        target = _join_target(hit, member)
                        if target in existing:
                            targets.append(target)
                if len(set(targets)) == 1:
                    return [_resolved(base, targets[0], existing)]
                if len(set(targets)) > 1:
                    return _candidates(base, sorted(set(targets)))
        if expr.startswith("super."):
            class_qn = _enclosing_class_cached(c.source_qname, class_qnames,
                                               enclosing_cache)
            member = expr[len("super."):]
            pending = list((inheritance_map or {}).get(class_qn, []))
            targets: list[str] = []
            seen_classes: set[str] = set()
            while pending:
                parent = pending.pop(0)
                if parent in seen_classes:
                    continue
                seen_classes.add(parent)
                target = _join_target(parent, member)
                if target in existing:
                    targets.append(target)
                pending.extend((inheritance_map or {}).get(parent, []))
            if len(targets) == 1:
                return [_resolved(base, targets[0], existing)]
            if len(targets) > 1:
                return _candidates(base, targets)
        explicit_this = expr.startswith("this.")
        if explicit_this:
            expr = expr[len("this."):]
            # `this.m()` is a same-class method call, not a dynamic receiver.
            # Keep field receivers (`this.field.m()`) on the type-binding path
            # below, but resolve the direct method form through the enclosing
            # class just like a bare `m()` call.
            if "." not in expr:
                enclosing = _enclosing_class_cached(c.source_qname, class_qnames,
                                                enclosing_cache)
                if enclosing:
                    target = _join_target(enclosing, expr)
                    if target in existing:
                        return [_resolved(base, target, existing)]
                    inherited = _inherited_member(enclosing, expr, existing,
                                                  inheritance_map)
                    if inherited:
                        return [_resolved(base, inherited, existing)]
        head, sep, rest = expr.partition(".")
        if not sep:
            return [_mark_dynamic(base, c.call_form, c.target_expr)]
        # Java receiver type binding: bare identifier whose declared type we know
        if var_types:
            scope_types = var_types.get(c.source_qname, {})
            receiver_type = scope_types.get(head)
            if receiver_type:
                hits = []
                if receiver_type in local and local[receiver_type] in existing:
                    hits = [local[receiver_type]]
                if not hits:
                    hits = _resolve_java_type(receiver_type, source_module,
                                              imports, mod_syms, star_modules)
                if len(hits) == 1:
                    target = _join_target(hits[0], rest)
                    if target in existing:
                        return [_resolved(base, target, existing)]
                    inherited = _inherited_member(hits[0], rest, existing,
                                                  inheritance_map)
                    if inherited:
                        return [_resolved(base, inherited, existing)]
                elif len(hits) > 1:
                    return _candidates(base, [_join_target(h, rest)
                                              for h in hits])
        if head in imports:
            mod, imported, _star = imports[head]
            if imported:
                class_qn = _join_target(mod, imported)
                return [_resolved(base, _join_target(class_qn, rest), existing)]
            return [_resolved(base, _join_target(mod, rest), existing)]
        if head in local and local[head] in existing:
            return [_resolved(base, _join_target(local[head], rest), existing)]
        if star_modules:  # Widget.run() where Widget came from import a.b.*
            hits = _star_lookup(head, star_modules, mod_syms, module_alls)
            if len(hits) == 1:
                return [_resolved(base, _join_target(hits[0], rest), existing)]
            if len(hits) > 1:
                return _candidates(base, [_join_target(h, rest) for h in hits])
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
        if star_modules:  # new Widget() where Widget came from import a.b.*
            hits = _star_lookup(name, star_modules, mod_syms, module_alls)
            if len(hits) == 1:
                return [_resolved(base, hits[0], existing)]
            if len(hits) > 1:
                return _candidates(base, hits)
        return [base]
    return [base]  # CALL_OTHER -> unresolved


# ── edge generators: structural relationships ─────────────────────────


def _build_contains(parsed: list[ParsedFile], qnames: set[str]) -> list[Edge]:
    """CONTAINS edges: parent_qname → child (module→function, class→method, etc.)."""
    edges: list[Edge] = []
    for pf in parsed:
        for n in pf.nodes:
            parent = n.parent_qname
            if parent is None and n.qualified_name != pf.module_qname:
                parent = pf.module_qname
            if parent:
                edges.append(Edge(
                    source=parent, target=n.qualified_name,
                    kind="contains", file_path=n.file_path,
                    resolution="resolved" if parent in qnames else "unresolved",
                ))
    return edges


def _build_imports(parsed: list[ParsedFile], qnames: set[str],
                   path_aliases: dict[str, str] | None = None,
                   base_url: str = "") -> list[Edge]:
    """IMPORT edges: module → imported_module."""
    edges: list[Edge] = []
    for pf in parsed:
        for imp in pf.imports:
            # Java static imports reference a class member (module is a class
            # qname, not a module) — not an import edge. A Python wildcard
            # import still creates a module-to-module import edge; its symbol
            # visibility is resolved separately by the call resolver.
            if "::" in imp.module:
                continue
            tgt = imp.module
            resolved = tgt in qnames
            if not resolved:  # alias @/x or baseUrl -> real module qname
                cand = _import_module(tgt, path_aliases, base_url, qnames)
                if cand in qnames:
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
                    path_aliases: dict[str, str] | None = None,
                    base_url: str = "",
                    mod_syms: dict | None = None) -> list[Edge]:
    """INHERITS edges: subclass → base class / interface.

    A base_expr resolves same-module first; a cross-module base then goes
    through the file's import map (``import pkg; class User(pkg.Base)`` /
    ``from pkg import Base``), so inheritance closure follows real modules
    instead of dropping straight to unresolved. The import map is per-file —
    a Java package with several files must not share one clobbered map.
    """
    edges: list[Edge] = []
    for pf in parsed:
        imports = _import_map(pf, path_aliases, qnames, base_url)
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
                elif "::" not in tgt and mod_syms:
                    # import a.b.*; class User extends Base — the base comes
                    # from a wildcard-imported module, not a named import.
                    star_modules = [imp.module for imp in pf.imports
                                    if imp.is_star]
                    hits = _star_lookup(tgt, star_modules, mod_syms, None)
                    if len(hits) == 1 and hits[0] in qnames:
                        tgt = hits[0]
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
                    path_aliases: dict[str, str] | None,
                    base_url: str,
                    di_annotations: list[str] | None) -> list[Edge]:
    """Annotation/constructor DI edges: injection point -> dependency class.

    Field injection (``@Autowired private OwnerRepository owners;``) requires an
    annotation matching di_annotations; constructor parameters are always
    candidates (a repo-typed ctor param is a real type dependency, framework or
    not - Spring injects single-constructor params even unannotated). The dep
    type must resolve to a repo class (same-package -> import, via
    _resolve_java_type); primitives/String/external types drop out naturally.
    kind="call" matches the existing Depends()-marker DI edges, so in_degree /
    flow / dead-code consume them the same way. The import map is per-file —
    a Java package with several files must not share one clobbered map."""
    edges: list[Edge] = []
    seen: set[tuple[str, str, str]] = set()
    for pf in parsed:
        if pf.language != "java" or not pf.di_decls:
            continue
        imports = _import_map(pf, path_aliases, existing, base_url)
        star_modules = [imp.module for imp in pf.imports if imp.is_star]
        for decl in pf.di_decls:
            if (decl.mechanism == "field"
                    and not _is_di_annotated(decl.annotations, di_annotations)):
                continue
            hits = [h for h in _resolve_java_type(
                decl.dep_expr, pf.module_qname, imports, mod_syms, star_modules)
                if h in existing]
            if not hits:
                continue
            rule_id = ("JAVA-F04" if decl.mechanism == "constructor"
                       else "JAVA-F05")
            evidence = {
                "mechanism": decl.mechanism,
                "dep_type": decl.dep_expr,
                "annotations": decl.annotations,
            }
            if len(hits) == 1:
                _dedup_append(edges, seen, Edge(
                    source=decl.owner_qname, target=hits[0], kind="call",
                    file_path=pf.file_path, resolution="resolved",
                    origin="type", rule_id=rule_id, evidence_json=evidence))
            else:  # wildcard ambiguity — candidate DI edges sharing a site_id
                base = Edge(source=decl.owner_qname, target=decl.dep_expr,
                            kind="call", file_path=pf.file_path,
                            resolution="candidate",
                            origin="type", rule_id=rule_id,
                            evidence_json=evidence)
                for candidate in _candidates(base, hits):
                    _dedup_append(edges, seen, candidate)
    return edges


def resolve_edges(parsed: list[ParsedFile],
                  existing_qnames: set[str],
                  path_aliases: dict[str, str] | None = None,
                  dependency_markers: list[str] | None = None,
                  di_annotations: list[str] | None = None,
                  base_url: str = "") -> list[Edge]:
    """Resolve all edges — call, contains, import, inherits — from parsed files.

    This is the single entry point for edge generation. Indexer calls this
    once and gets the complete edge list.
    """
    edges = resolve_calls(parsed, existing_qnames, path_aliases,
                          dependency_markers, base_url)
    edges.extend(_build_contains(parsed, existing_qnames))
    edges.extend(_build_imports(parsed, existing_qnames, path_aliases, base_url))
    mod_syms = _module_symbols(parsed)
    edges.extend(_build_inherits(parsed, existing_qnames, path_aliases,
                                 base_url, mod_syms))
    edges.extend(_build_di_edges(parsed, existing_qnames, mod_syms,
                                 path_aliases, base_url, di_annotations))
    from code_review_ai.java_routing import build_route_edges
    edges.extend(build_route_edges(parsed, existing_qnames))
    return edges
