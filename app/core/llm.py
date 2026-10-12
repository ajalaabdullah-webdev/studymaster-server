"""Provider-agnostic LLM access via LiteLLM.

Everything in the app calls these helpers instead of any vendor SDK directly.
Swapping OpenAI -> DeepSeek -> Kimi is a one-line change of the model string in
config (``LLM_DEFAULT_MODEL`` / ``LLM_SMART_MODEL`` / ``LLM_CHEAP_MODEL``), so the
"we might switch models tomorrow for cost" requirement is satisfied.

Model tiers:
- ``default`` : everyday work (note steps, coach, explanations) — cheap & fast.
- ``smart``   : harder reasoning / verification / marking.
- ``cheap``   : bulk/offline jobs (e.g. the question-cleanup pass). Falls back
  to ``default`` when no cheap model is configured.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

import litellm

from app.config import settings

logger = logging.getLogger("studymaster.llm")

# Keep LiteLLM quiet and resilient.
litellm.drop_params = True  # ignore params a given provider doesn't support
litellm.set_verbose = False

Tier = Literal["default", "smart", "cheap"]


def _model_for(tier: Tier) -> str:
    if tier == "smart":
        return settings.llm_smart_model
    if tier == "cheap":
        return settings.llm_cheap_model or settings.llm_default_model
    return settings.llm_default_model


def _api_key_for(model: str) -> str | None:
    provider = model.split("/", 1)[0] if "/" in model else "openai"
    if provider == "openai":
        return settings.openai_api_key
    if provider == "deepseek":
        return settings.deepseek_api_key
    if provider in {"gemini", "google"}:
        return settings.gemini_api_key
    return None


async def chat(
    messages: list[dict[str, Any]],
    *,
    tier: Tier = "default",
    temperature: float = 0.7,
    max_tokens: int | None = None,
    response_json: bool = False,
    **kwargs: Any,
) -> str:
    """Run a chat completion and return the assistant text.

    ``response_json=True`` requests a JSON object from providers that support it.
    """
    model = _model_for(tier)
    params: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "api_key": _api_key_for(model),
    }
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    if response_json:
        params["response_format"] = {"type": "json_object"}
    params.update(kwargs)

    resp = await litellm.acompletion(**params)
    return resp["choices"][0]["message"]["content"] or ""


async def chat_stream(
    messages: list[dict[str, Any]],
    *,
    tier: Tier = "default",
    temperature: float = 0.7,
    max_tokens: int | None = None,
    **kwargs: Any,
):
    """Async generator yielding response text chunks as they arrive (for SSE)."""
    model = _model_for(tier)
    params: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "api_key": _api_key_for(model),
        "stream": True,
    }
    if max_tokens is not None:
        params["max_tokens"] = max_tokens
    params.update(kwargs)

    resp = await litellm.acompletion(**params)
    async for chunk in resp:
        try:
            delta = chunk["choices"][0]["delta"].get("content")
        except (KeyError, IndexError, TypeError):
            delta = None
        if delta:
            yield delta


async def chat_json(
    messages: list[dict[str, Any]],
    *,
    tier: Tier = "default",
    temperature: float = 0.4,
    **kwargs: Any,
) -> Any:
    """Chat completion that returns parsed JSON.

    Tolerates models that wrap JSON in `````json`` fences.
    """
    raw = await chat(
        messages, tier=tier, temperature=temperature, response_json=True, **kwargs
    )
    return _loads_loose(raw)


# LaTeX in LLM JSON: models emit single backslashes (\frac, \times, \sqrt). In JSON,
# \f \t \n \r \b are valid escapes (so \frac -> formfeed+"rac"), and \s \p etc. are
# INVALID (parse error). Doubling any single backslash before a letter (except a real
# \uXXXX unicode escape) makes all LaTeX commands survive json.loads intact.
_LATEX_BACKSLASH = re.compile(r"(?<!\\)\\(?!u[0-9a-fA-F]{4})(?=[a-zA-Z])")


def _fix_latex_backslashes(text: str) -> str:
    return _LATEX_BACKSLASH.sub(r"\\\\", text)


def _loads_loose(raw: str) -> Any:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    text = _fix_latex_backslashes(text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Last resort: extract the first {...} or [...] block.
        for open_c, close_c in (("{", "}"), ("[", "]")):
            start, end = text.find(open_c), text.rfind(close_c)
            if start != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        logger.warning("LLM returned non-JSON content: %s", text[:200])
        raise


async def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts for semantic search.

    Provider-agnostic like the chat helpers: change ``EMBEDDING_MODEL`` to move
    off OpenAI. Returns one vector per input, in the same order.
    """
    if not texts:
        return []
    model = settings.embedding_model
    kwargs: dict[str, Any] = {"model": model, "input": texts}
    key = _api_key_for(model if "/" in model else f"openai/{model}")
    if key:
        kwargs["api_key"] = key
    # Only OpenAI's v3 models support shortening vectors at request time.
    if "text-embedding-3" in model:
        kwargs["dimensions"] = settings.embedding_dimensions
    resp = await litellm.aembedding(**kwargs)
    items = sorted(resp["data"], key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in items]
