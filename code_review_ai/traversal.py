"""Traversal policy for flow building (Phase 2, guide §3.3).

Only edges that deterministically reach a repo target are traversable by
flow_builder. ``resolved`` always is; ``semantic`` is traversable only when
the framework rule that produced it has been explicitly registered here
(the allow-list carries the mis-match negative-case tests that vet a rule,
guide §3.3); every other resolution — candidate, dynamic, unresolved,
external — is excluded, so a flow is never built on a guess.
"""

RESOLVED = "resolved"
SEMANTIC = "semantic"
CANDIDATE = "candidate"
DYNAMIC = "dynamic"
UNRESOLVED = "unresolved"
EXTERNAL = "external"

# rule_id -> traversable. The registry is the allow-list is_traversable
# consumes; Phase 6 semantic adapters populate it via register_semantic_rule.
_SEMANTIC_RULES: dict[str, bool] = {}


def register_semantic_rule(rule_id: str, traversable: bool = True) -> None:
    """Allow (or revoke) traversal for a semantic rule's edges.

    An unregistered semantic edge is never traversable — the framework rule
    behind it has not been vetted, so its resolution must not build flows.
    Setting traversable=False removes an entry from the allow-list."""
    _SEMANTIC_RULES[rule_id] = traversable


def is_traversable(kind: str, resolution: str,
                   rule_id: str | None = None) -> bool:
    """Whether flow_builder may traverse an edge of this (kind, resolution).

    ``resolved`` is always traversable regardless of kind (call and structural
    edges both participate, as they always have); ``semantic`` only when its
    rule_id is registered with traversable=True; candidate/dynamic/unresolved/
    external never are.
    """
    if resolution == RESOLVED:
        return True
    if resolution == SEMANTIC:
        return rule_id is not None and _SEMANTIC_RULES.get(rule_id, False)
    return False
