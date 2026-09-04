"""Provider model classes for the review agent.

DeepSeek serves reasoning models whose assistant responses carry
``reasoning_content``. LangChain's OpenAI message serializer drops that field
on both the inbound parse (``ChatOpenAI``) and the outbound request build, but
the DeepSeek API documents that when a request binds ``tools`` the historical
``reasoning_content`` values must be echoed back. ``ChatDeepSeek`` fixes the
inbound half (it stores the value in ``additional_kwargs``); this subclass adds
the missing outbound half so the review agent's tool loop stays protocol-valid.
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
                if (isinstance(outgoing, dict)
                        and outgoing.get("role") == "assistant"
                        and isinstance(reasoning, str) and reasoning):
                    # Echo the reasoning verbatim as a sibling of content and
                    # tool_calls, matching how the provider's own client would
                    # append the assistant turn ({content, reasoning_content,
                    # tool_calls}). The base serializer drops this field.
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
