"""Observer-only event hooks for the review loop.

Policy -- the action-tool budget and the evidence gate -- is deliberately NOT
here. It is hardcoded in ``loop.py`` so the control flow stays readable and the
gates can never be "not registered". Hooks exist only for side-effect-free
observers (progress display, logs); there is no ``intercept`` on purpose.

The point names are the single source of truth shared by the loop and every
consumer (terminal display, progress timeline). An observer receives
``(point, context)`` -- the same shape as the old ``progress(event, data)``
callback, so existing consumers adapt without changing their call sites.

Emitted events (a consumer must tolerate missing/``None`` context keys; only the
keys below are guaranteed for that point):

=========================  ========================================  ===========
point                      fired                                     context keys
=========================  ========================================  ===========
model_request_started      before each model invoke                  turn
model_response_received    after each model invoke returns           turn,
                                                                     response_chars,
                                                                     tool_calls
pre_tool                   before each tool runs                     name, args
post_tool                  after each tool ran                       name, status,
                                                                     response_chars
run_finished               at the single exit of run_loop            failure_reason,
                                                                     final_chars
=========================  ========================================  ===========

``post_tool``'s ``status`` is one of ``executed`` (ran), ``rejected_policy``
(its own policy/schema denial), or ``error`` (it raised) -- see ``ToolCallStatus``
in ``schemas.py``. ``run_finished`` fires on every exit path, success or failure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

POINT_MODEL_REQUEST_STARTED = "model_request_started"
POINT_MODEL_RESPONSE_RECEIVED = "model_response_received"
POINT_PRE_TOOL = "pre_tool"
POINT_POST_TOOL = "post_tool"
POINT_RUN_FINISHED = "run_finished"

Observer = Callable[[str, dict], None]


@dataclass
class Hooks:
    """A named registry of observers. ``on`` registers, ``emit`` notifies."""

    _observers: dict[str, list[Observer]] = field(default_factory=dict)

    def on(self, point: str, observer: Observer) -> None:
        """Subscribe ``observer(point, context)`` to one named point."""
        self._observers.setdefault(point, []).append(observer)

    def emit(self, point: str, **context: object) -> None:
        """Run every observer of ``point`` with a copy of the context dict.

        Observers are best-effort side channels; a raising observer stops the
        remaining ones, so observers must not throw.
        """
        for observer in list(self._observers.get(point, ())):
            observer(point, context)
