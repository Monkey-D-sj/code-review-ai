"""Traversal policy for flow building (Phase 2, guide §3.3).

Only edges that deterministically reach a repo target are traversable by
flow_builder. ``resolved`` is the only traversable resolution — candidate,
dynamic, unresolved, external are excluded, so a flow is never built on a
guess.

Framework-inferred edges (Spring DI, routing, single-impl interface dispatch)
are emitted as ``resolved`` carrying an ``origin``/``rule_id`` provenance tag,
not as a separate resolution: a deterministic framework inference is a static
fact, and an ambiguous one downgrades to ``candidate`` at emit time (see
resolver._candidates). A per-rule allow-list gate on traversal (the former
``semantic`` resolution) was dropped — see commit history; rule_id stays on the
edge as audit evidence, but never gates traversal.
"""

RESOLVED = "resolved"
CANDIDATE = "candidate"
DYNAMIC = "dynamic"
UNRESOLVED = "unresolved"
EXTERNAL = "external"


def is_traversable(kind: str, resolution: str) -> bool:
    """Whether flow_builder may traverse an edge of this resolution.

    ``resolved`` is always traversable regardless of kind (call and structural
    edges both participate, as they always have). Every other resolution —
    candidate/dynamic/unresolved/external — is excluded, so a flow is never
    built on a guess. (``kind`` is accepted for API stability; resolution alone
    decides.)
    """
    return resolution == RESOLVED
