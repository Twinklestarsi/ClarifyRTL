"""Chat-completion helper that always streams.

Some OpenAI-compatible relays (e.g. the a.fe8.cn proxy serving
``gpt-5.6-luna``) return an empty ``message.content`` for non-streaming
``/chat/completions`` requests even when the call succeeds, but reliably
deliver the full text over ``stream=True``.  The pipeline routes every chat
call through :func:`create`, which forces streaming and accumulates the deltas
into an object shaped like the non-streaming ``ChatCompletion`` response so
existing ``resp.choices[0].message.content`` call sites keep working without
further changes.
"""

from types import SimpleNamespace

try:
    import openai as _openai
except Exception:  # noqa: BLE001  (openai import optional for unit tests)
    _openai = None

# Transient transport/server failures worth retrying.  These are the errors the
# a.fe8.cn relay raises while overloaded ("Our servers are currently
# overloaded", "Upstream request failed"); retrying the whole request is safe
# because completion requests are idempotent reads.
_MAX_ATTEMPTS = 6
_BACKOFF_SECONDS = [2, 4, 8, 16, 30]


def _is_transient(exc):
    if _openai is None:
        return False
    if isinstance(exc, (_openai.APIConnectionError, _openai.APITimeoutError,
                        _openai.RateLimitError)):
        return True
    # APIError is the base for relay-side failures ("upstream request failed",
    # "server overloaded"); do not retry client 4xx errors or auth issues.
    if isinstance(exc, _openai.APIStatusError):
        return False
    if isinstance(exc, _openai.APIError):
        return True
    return False


def _usage_to_obj(usage):
    """Normalize provider usage into an object with ``model_dump()``."""
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        data = usage.model_dump()
    elif isinstance(usage, dict):
        data = dict(usage)
    else:
        data = {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        data = {k: v for k, v in data.items() if v is not None}
        if not data:
            return None

    # Normalize OpenAI Responses-style names onto chat.completions names.
    if "prompt_tokens" not in data and "input_tokens" in data:
        data["prompt_tokens"] = data["input_tokens"]
    if "completion_tokens" not in data and "output_tokens" in data:
        data["completion_tokens"] = data["output_tokens"]
    if "total_tokens" not in data:
        prompt = data.get("prompt_tokens") or 0
        completion = data.get("completion_tokens") or 0
        if prompt or completion:
            data["total_tokens"] = prompt + completion

    return SimpleNamespace(
        prompt_tokens=data.get("prompt_tokens"),
        completion_tokens=data.get("completion_tokens"),
        total_tokens=data.get("total_tokens"),
        input_tokens=data.get("input_tokens", data.get("prompt_tokens")),
        output_tokens=data.get("output_tokens", data.get("completion_tokens")),
        model_dump=lambda: dict(data),
    )


def _run(client, kwargs):
    stream = client.chat.completions.create(**kwargs)
    # Most endpoints honour ``stream=True`` and return an iterable.  A few
    # OpenAI-compatible gateways (and lightweight test doubles) instead
    # return an already assembled ChatCompletion.  Accept that shape as a
    # compatibility fallback without changing the real streaming path.
    choices = getattr(stream, "choices", None)
    if choices and getattr(choices[0], "message", None) is not None:
        return stream

    content_parts = []
    response_id = None
    response_model = None
    finish_reason = None
    usage = None
    for chunk in stream:
        if response_id is None:
            response_id = getattr(chunk, "id", None)
        if response_model is None:
            response_model = getattr(chunk, "model", None)
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        if delta is not None:
            piece = getattr(delta, "content", None)
            if piece:
                content_parts.append(piece)
        if getattr(choice, "finish_reason", None):
            finish_reason = choice.finish_reason

    message = SimpleNamespace(content="".join(content_parts), role="assistant")
    choice = SimpleNamespace(message=message, finish_reason=finish_reason,
                             index=0)
    return SimpleNamespace(choices=[choice], id=response_id,
                           model=response_model, usage=_usage_to_obj(usage))


def create(client, *, model, messages, temperature=None, max_tokens=None,
           **kwargs):
    """Force a streaming chat completion and return a response-like object.

    Only the fields the codebase actually reads are populated: ``choices[0]
    .message.content``, ``choices[0].finish_reason``, ``id``, ``model`` and
    ``usage`` (``None`` unless the relay reports it).

    Transient relay/server failures are retried with a short backoff so a
    single overloaded upstream request does not abort a whole generation.
    """
    kwargs["model"] = model
    kwargs["messages"] = messages
    if temperature is not None:
        kwargs["temperature"] = temperature
    kwargs["max_tokens"] = max_tokens
    kwargs["stream"] = True
    # Ask OpenAI-compatible relays to emit a final usage chunk.
    stream_options = dict(kwargs.get("stream_options") or {})
    stream_options.setdefault("include_usage", True)
    kwargs["stream_options"] = stream_options

    last_exc = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return _run(client, kwargs)
        except Exception as exc:  # noqa: BLE001 - filter below
            # Some gateways reject ``stream_options``; retry without it once.
            if "stream_options" in kwargs and attempt == 0 and not _is_transient(exc):
                kwargs = dict(kwargs)
                kwargs.pop("stream_options", None)
                last_exc = exc
                continue
            if not _is_transient(exc):
                raise
            last_exc = exc
            delay = _BACKOFF_SECONDS[attempt] if attempt < len(
                _BACKOFF_SECONDS) else _BACKOFF_SECONDS[-1]
            import time
            time.sleep(delay)
    raise last_exc
