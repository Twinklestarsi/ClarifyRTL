"""API configuration and network hardening for this host.

Two facts about this machine force this module to exist:

1. ``.env`` contains inline comments (``OPENAI_MODEL= Kimi-K2.5 # note``) and
   the model name must be taken up to the first `` #``.
2. The shell exports a SOCKS proxy (``all_proxy=socks5://...``) and a
   ``no_proxy`` value containing ``[::1]``.  ``httpx`` parses ``no_proxy`` and
   raises ``InvalidURL: Invalid port: ':1]'`` before any request is sent.
   The endpoint is directly reachable, so every client built here bypasses
   proxy environment variables entirely (``trust_env=False``).

The library modules that build their own ``OpenAI`` client (IR generation,
requirement proposals, Property IR) are patched through
:func:`patch_library_clients`; nothing else in the repository is modified.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx

DEFAULT_ENV_PATH = Path("/nas2nfs/homes/2531930/ONE-TURN/.env")
_PROXY_KEYS = ("all_proxy", "http_proxy", "https_proxy", "no_proxy")


def load_dotenv(path: Path | str | None = None) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, dropping inline ``#`` comments and quotes."""
    source = Path(path or DEFAULT_ENV_PATH)
    values: dict[str, str] = {}
    if not source.is_file():
        return values
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value.strip('"').strip("'")
    return values


def sanitize_network_env() -> list[str]:
    """Remove proxy variables that break ``httpx`` on this host."""
    removed = []
    for key in list(os.environ):
        if key.lower() in _PROXY_KEYS:
            os.environ.pop(key, None)
            removed.append(key)
    return removed


def _http_client(timeout: float) -> httpx.Client:
    return httpx.Client(trust_env=False, timeout=httpx.Timeout(timeout))


DAG_ENV_PATH = Path("/nas2nfs/homes/2531930/ONE-TURN/DAG/.env")
PROVIDER_ENV_FILES = (DEFAULT_ENV_PATH, DAG_ENV_PATH)
_RESOLVED: tuple | None = None


def _candidates(env_path: Path | str | None = None) -> list[dict[str, str]]:
    """Ordered provider candidates: ``OPENAI_*`` first, then ``DAG_*``."""
    env = load_dotenv(env_path)
    dag = load_dotenv(DAG_ENV_PATH) if Path(DAG_ENV_PATH).is_file() else {}
    result = []
    for prefix, source in (("OPENAI", env), ("DAG", dag)):
        key = os.environ.get(f"{prefix}_API_KEY") or source.get(f"{prefix}_API_KEY")
        base = os.environ.get(f"{prefix}_BASE_URL") or source.get(f"{prefix}_BASE_URL")
        model = os.environ.get(f"{prefix}_MODEL") or source.get(f"{prefix}_MODEL")
        if key and base and model:
            result.append({"prefix": prefix, "key": key, "base": base, "model": model})
    return result


def _probe(client, model: str) -> None:
    client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": "ping"}],
        max_tokens=1, temperature=0)


def resolve_provider(*, env_path: Path | str | None = None,
                     timeout: float | None = None) -> dict[str, str]:
    """Probe configured endpoints in order and return the first working one."""
    global _RESOLVED
    if _RESOLVED is not None and env_path is None:
        return _RESOLVED
    from openai import OpenAI

    sanitize_network_env()
    errors = []
    for candidate in _candidates(env_path):
        client = OpenAI(api_key=candidate["key"], base_url=candidate["base"],
                        timeout=timeout or 300.0, max_retries=0,
                        http_client=_http_client(timeout or 300.0))
        try:
            _probe(client, candidate["model"])
        except Exception as exc:  # noqa: BLE001 - try the next provider
            errors.append(f"{candidate['prefix']}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        resolved = dict(candidate)
        if env_path is None:
            _RESOLVED = resolved
        return resolved
    raise RuntimeError("no working LLM endpoint; " + "; ".join(errors))


def chat_client(*, env_path: Path | str | None = None, timeout: float | None = None,
                verify: bool = True):
    """Return ``(client, model)`` for the first working provider."""
    from openai import OpenAI

    candidate = resolve_provider(env_path=env_path, timeout=timeout)
    client = OpenAI(api_key=candidate["key"], base_url=candidate["base"],
                    timeout=timeout or 300.0, max_retries=0,
                    http_client=_http_client(timeout or 300.0))
    return client, candidate["model"]


def provider_env(*, env_path: Path | str | None = None) -> dict[str, str]:
    """Credentials in the shape the repository's generators expect."""
    candidate = resolve_provider(env_path=env_path)
    return {"OPENAI_API_KEY": candidate["key"], "OPENAI_BASE_URL": candidate["base"],
            "OPENAI_MODEL": candidate["model"], "provider": candidate["prefix"]}


def patch_library_clients(timeout: float = 600.0) -> None:
    """Make library-built clients bypass the broken proxy environment."""
    from openai import OpenAI

    factory = lambda **kwargs: OpenAI(  # noqa: E731 - keeps call sites unchanged
        http_client=_http_client(timeout), timeout=timeout, max_retries=0, **kwargs)

    import IRgeneration.ir_generate.gen_petrinet_ir as gpi

    gpi.OpenAI = factory

    try:  # optional modules: absent in some trimmed deployments
        import assumption_modeling.requirements as requirements

        requirements.OpenAI = factory
    except (ImportError, AttributeError):
        pass
    try:
        import sva_generation.property_ir as property_ir

        property_ir.OpenAI = factory
    except (ImportError, AttributeError):
        pass