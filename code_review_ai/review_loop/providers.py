"""Provider model classes for the review loop.

Mirrors ``review_agent/providers.py`` (master) so DeepSeek reasoning models stay
protocol-valid in a tool loop. DeepSeek serves models whose assistant responses
carry ``reasoning_content``; LangChain's OpenAI serializer drops that field on
both the inbound parse and the outbound request build, but the DeepSeek API
requires echoing it back when tools are bound. ``ReasoningChatModelMixin`` adds
the outbound half; ``build_review_model`` routes DeepSeek endpoints to
``ChatDeepSeek`` and everything else to ``ChatOpenAI``.

Self-contained copy: importing ``review_agent`` would pull langgraph.
"""

from __future__ import annotations

from typing import Any


class ReasoningChatModelMixin:
    """Re-emit captured ``reasoning_content`` on assistant messages.

    Every override is defensive: if langchain internals drift, the request is
    sent unchanged rather than raising and failing a review.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            messages = payload.get("messages")
            if not isinstance(input_, list) or not isinstance(messages, list):
                return payload
            if len(messages) != len(input_):
                return payload
            for source, outgoing in zip(input_, messages):
                reasoning = getattr(source, "additional_kwargs", {}).get(
                    "reasoning_content")
                # Echo reasoning verbatim, matching how the provider's own
                # client appends the assistant turn ({content,
                # reasoning_content, tool_calls}). The base serializer drops it.
                # Only echo beside an assistant that also carries tool_calls: a
                # plain-text / empty assistant turn does not need its reasoning
                # re-sent, and echoing it there has tripped DeepSeek's
                # "tool_calls must be followed by tool replies" validation on a
                # later multi-tool request.
                has_tool_calls = bool(outgoing.get("tool_calls"))
                if (isinstance(outgoing, dict)
                        and outgoing.get("role") == "assistant"
                        and has_tool_calls
                        and isinstance(reasoning, str) and reasoning):
                    outgoing["reasoning_content"] = reasoning
        except Exception:
            pass
        return payload


def build_review_model(model_name: str, base_url: str | None,
                       api_key: str) -> Any:
    """Create the provider model, routing DeepSeek to its reasoning adapter."""
    uses_deepseek = bool(base_url and "deepseek" in base_url.lower()) or \
        (model_name or "").lower().startswith("deepseek")
    if uses_deepseek:
        try:
            from langchain_deepseek import ChatDeepSeek
        except ImportError as exc:
            raise RuntimeError("langchain-deepseek is not installed") from exc

        class DeepSeekChatOpenAI(ReasoningChatModelMixin, ChatDeepSeek):
            pass

        return DeepSeekChatOpenAI(model=model_name, temperature=0,
                                  api_key=api_key,
                                  **( {"base_url": base_url} if base_url else {}))
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("langchain-openai is not installed") from exc
    return ChatOpenAI(model=model_name, temperature=0, api_key=api_key,
                      **( {"base_url": base_url} if base_url else {}))
