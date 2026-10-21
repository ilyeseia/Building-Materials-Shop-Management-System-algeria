"""Registry of OpenAI-compatible AI providers and their models.

The assistant speaks one dialect: the OpenAI ``/chat/completions`` API.  That
is what every provider listed in :data:`PROVIDER_CATALOG` implements, which is
why a single client can talk to a hosted model, to a free-tier gateway or to a
runtime running on the shop's own machine.

The catalogue is data, not code: an administrator picks an entry, pastes the
provider's key and is done.  ``free_tier`` marks the providers that give a
usable quota without a paid subscription, and ``suggested_models`` gives the
model identifiers that are known to work with that endpoint (including the
``:free`` variants of OpenRouter and the locally served models of Ollama and
LM Studio).

Nothing in this module ever returns a decrypted credential to a caller that is
not the HTTP client itself, and no credential is written to a log.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable

from flask import current_app
from sqlalchemy import select

from app.extensions import db
from app.models.ai import (
    AI_PROVIDER_LOCAL_RUNTIME,
    AI_PROVIDER_OPENAI_COMPATIBLE,
    AIModel,
    AIProvider,
)
from app.services import ai_keys


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    """One provider the administrator can add with a single click."""

    code: str
    name: str
    base_url: str
    description: str
    free_tier: bool = False
    needs_key: bool = True
    provider_type: str = AI_PROVIDER_OPENAI_COMPATIBLE
    suggested_models: tuple[str, ...] = ()
    signup_url: str = ""


#: OpenAI-compatible endpoints.  ``base_url`` is the root of the API; the
#: client appends ``/chat/completions`` and ``/models``.
PROVIDER_CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        code="local",
        name="Local assistant (no model)",
        base_url="",
        description=(
            "The deterministic built-in assistant. It needs no key, makes no "
            "network call and answers the stock, sales and balance questions."
        ),
        free_tier=True,
        needs_key=False,
        provider_type=AI_PROVIDER_LOCAL_RUNTIME,
        signup_url="",
    ),
    CatalogEntry(
        code="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        description=(
            "One key, many models from many vendors.  Models whose name ends "
            "with ':free' run on the free quota and cost nothing."
        ),
        free_tier=True,
        suggested_models=(
            "deepseek/deepseek-r1:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemini-2.0-flash-exp:free",
            "qwen/qwen-2.5-72b-instruct:free",
            "mistralai/mistral-small-3.1-24b-instruct:free",
        ),
        signup_url="https://openrouter.ai/keys",
    ),
    CatalogEntry(
        code="groq",
        name="Groq",
        base_url="https://api.groq.com/openai/v1",
        description=(
            "Very fast inference on an LPU.  The developer plan has a free "
            "tier that is enough for a shop's daily questions."
        ),
        free_tier=True,
        suggested_models=(
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
        ),
        signup_url="https://console.groq.com/keys",
    ),
    CatalogEntry(
        code="gemini",
        name="Google AI Studio (Gemini)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        description=(
            "Google's free tier for Gemini, reachable through the "
            "OpenAI-compatible endpoint."
        ),
        free_tier=True,
        suggested_models=("gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"),
        signup_url="https://aistudio.google.com/app/apikey",
    ),
    CatalogEntry(
        code="mistral",
        name="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        description="European host.  A free experimental tier is available.",
        free_tier=True,
        suggested_models=("mistral-small-latest", "open-mistral-nemo", "ministral-8b-latest"),
        signup_url="https://console.mistral.ai/api-keys",
    ),
    CatalogEntry(
        code="cerebras",
        name="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        description="Fast inference on wafer-scale hardware, with a free tier.",
        free_tier=True,
        suggested_models=("llama-3.3-70b", "llama3.1-8b", "qwen-3-32b"),
        signup_url="https://cloud.cerebras.ai",
    ),
    CatalogEntry(
        code="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        description="Very cheap reasoning and chat models.",
        suggested_models=("deepseek-chat", "deepseek-reasoner"),
        signup_url="https://platform.deepseek.com/api_keys",
    ),
    CatalogEntry(
        code="together",
        name="Together AI",
        base_url="https://api.together.xyz/v1",
        description="An open-model catalogue with free endpoints.",
        free_tier=True,
        suggested_models=(
            "meta-llama/Llama-3.3-70B-Instruct-Turbo",
            "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
            "mistralai/Mixtral-8x7B-Instruct-v0.1",
        ),
        signup_url="https://api.together.ai/settings/api-keys",
    ),
    CatalogEntry(
        code="siliconflow",
        name="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        description="A gateway for many open models with a small free quota.",
        free_tier=True,
        suggested_models=(
            "Qwen/Qwen2.5-72B-Instruct",
            "deepseek-ai/DeepSeek-V3",
            "meta-llama/Llama-3.3-70B-Instruct",
        ),
        signup_url="https://cloud.siliconflow.cn/account/ak",
    ),
    CatalogEntry(
        code="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        description="The reference implementation of this API dialect.",
        suggested_models=("gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"),
        signup_url="https://platform.openai.com/api-keys",
    ),
    CatalogEntry(
        code="ollama",
        name="Ollama (on this machine)",
        base_url="http://127.0.0.1:11434/v1",
        description=(
            "A model runtime on the shop's own server.  No key and no data "
            "leaves the building."
        ),
        free_tier=True,
        needs_key=False,
        provider_type=AI_PROVIDER_LOCAL_RUNTIME,
        suggested_models=("llama3.2", "qwen2.5", "mistral", "gemma3"),
        signup_url="https://ollama.com/download",
    ),
    CatalogEntry(
        code="lmstudio",
        name="LM Studio (on this machine)",
        base_url="http://127.0.0.1:1234/v1",
        description="Another local runtime with an OpenAI-compatible server.",
        free_tier=True,
        needs_key=False,
        provider_type=AI_PROVIDER_LOCAL_RUNTIME,
        suggested_models=("local-model"),
        signup_url="https://lmstudio.ai",
    ),
    CatalogEntry(
        code="custom",
        name="Custom endpoint",
        base_url="",
        description="Any other service that implements /chat/completions.",
        signup_url="",
    ),
)

_CATALOG_BY_CODE = {entry.code: entry for entry in PROVIDER_CATALOG}


def catalog() -> list[CatalogEntry]:
    """The catalogue as a list, for the "add a provider" picker."""
    return list(PROVIDER_CATALOG)


def catalog_entry(code: str) -> CatalogEntry | None:
    return _CATALOG_BY_CODE.get(str(code or "").strip().lower())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def list_providers(only_active: bool = False) -> list[AIProvider]:
    statement = select(AIProvider).order_by(
        AIProvider.is_default.desc(), AIProvider.name
    )
    if only_active:
        statement = statement.where(AIProvider.is_active.is_(True))
    return list(db.session.execute(statement).scalars())


def get_provider(provider_id: int) -> AIProvider | None:
    return db.session.get(AIProvider, provider_id)


def get_provider_by_code(code: str) -> AIProvider | None:
    return db.session.execute(
        select(AIProvider).filter_by(code=str(code or "").strip().lower())
    ).scalar_one_or_none()


def default_provider() -> AIProvider | None:
    """The provider the assistant should use, if one is configured."""
    provider = db.session.execute(
        select(AIProvider)
        .where(AIProvider.is_default.is_(True), AIProvider.is_active.is_(True))
        .limit(1)
    ).scalar_one_or_none()
    if provider is not None:
        return provider
    # No explicit default: use the first active provider that has a usable
    # credential and a model.
    for candidate in list_providers(only_active=True):
        if candidate.active_model is not None and (
            not candidate.needs_key or candidate.has_key
        ):
            return candidate
    return None


def set_default(provider: AIProvider) -> None:
    """Make one provider the default; the others stop being the default."""
    for other in db.session.execute(select(AIProvider)).scalars():
        other.is_default = other.id == provider.id
    db.session.flush()


def create_provider(
    *,
    code: str,
    name: str,
    base_url: str,
    api_key: str | None = None,
    description: str = "",
    provider_type: str = AI_PROVIDER_OPENAI_COMPATIBLE,
    free_tier: bool = False,
    is_active: bool = True,
    notes: str = "",
) -> AIProvider:
    """Create a provider and encrypt its credential."""
    provider = AIProvider(
        code=str(code or "").strip().lower(),
        name=(name or "").strip(),
        description=description or "",
        provider_type=provider_type,
        base_url=(base_url or "").strip().rstrip("/"),
        free_tier=bool(free_tier),
        is_active=bool(is_active),
        notes=notes or "",
    )
    if api_key:
        provider.api_key_encrypted = ai_keys.encrypt_key(api_key)
        provider.key_hint = ai_keys.key_hint(api_key)
    db.session.add(provider)
    db.session.flush()
    return provider


def update_provider(provider: AIProvider, **changes) -> AIProvider:
    """Apply changes; a new ``api_key`` replaces the stored credential."""
    api_key = changes.pop("api_key", None)
    for field in (
        "name", "description", "base_url", "provider_type", "free_tier",
        "is_active", "notes",
    ):
        if field in changes and changes[field] is not None:
            setattr(provider, field, changes[field])
    if "base_url" in changes and provider.base_url:
        provider.base_url = provider.base_url.rstrip("/")
    if api_key is not None:
        if api_key == "":
            # An empty box means "leave the stored key alone".
            pass
        else:
            provider.api_key_encrypted = ai_keys.encrypt_key(api_key)
            provider.key_hint = ai_keys.key_hint(api_key)
    db.session.flush()
    return provider


def delete_provider(provider: AIProvider) -> None:
    db.session.delete(provider)
    db.session.flush()


def list_models(provider_id: int | None = None) -> list[AIModel]:
    statement = select(AIModel).order_by(AIModel.sort_order, AIModel.id)
    if provider_id is not None:
        statement = statement.where(AIModel.provider_id == provider_id)
    return list(db.session.execute(statement).scalars())


def get_model(model_id: int) -> AIModel | None:
    return db.session.get(AIModel, model_id)


def create_model(
    provider: AIProvider,
    *,
    code: str,
    label: str = "",
    description: str = "",
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    supports_tools: bool = True,
    free_tier: bool = False,
    is_active: bool = True,
    is_default: bool = False,
    sort_order: int = 0,
) -> AIModel:
    model = AIModel(
        provider_id=provider.id,
        code=(code or "").strip(),
        label=(label or code or "").strip(),
        description=description or "",
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        supports_tools=bool(supports_tools),
        free_tier=bool(free_tier),
        is_active=bool(is_active),
        is_default=bool(is_default),
        sort_order=int(sort_order or 0),
    )
    db.session.add(model)
    db.session.flush()
    if model.is_default:
        set_default_model(provider, model)
    return model


def update_model(model: AIModel, **changes) -> AIModel:
    for field in (
        "code", "label", "description", "context_window", "max_output_tokens",
        "supports_tools", "free_tier", "is_active", "sort_order",
    ):
        if field in changes and changes[field] is not None:
            setattr(model, field, changes[field])
    db.session.flush()
    if changes.get("is_default"):
        set_default_model(model.provider, model)
    return model


def set_default_model(provider: AIProvider, model: AIModel) -> None:
    for other in provider.models:
        other.is_default = other.id == model.id
    db.session.flush()


def delete_model(model: AIModel) -> None:
    db.session.delete(model)
    db.session.flush()


def credential_for(provider: AIProvider) -> str | None:
    """Decrypt a provider's credential for the HTTP client.

    Returns ``None`` when the provider needs no key.  Raises
    :class:`ai_keys.KeyUnavailable` when the stored value cannot be opened.
    """
    if not provider.needs_key:
        return None
    return ai_keys.decrypt_key(provider.api_key_encrypted)


# ---------------------------------------------------------------------------
# Connectivity
# ---------------------------------------------------------------------------


@dataclass
class ConnectionResult:
    """The outcome of a connectivity test."""

    ok: bool
    message_key: str = "ai.test_ok"
    detail: str = ""
    models: list[dict] = None  # type: ignore[assignment]
    latency_ms: int = 0
    error_key: str | None = None

    def __post_init__(self) -> None:
        if self.models is None:
            self.models = []


def _http_json(
    url: str,
    *,
    key: str | None,
    timeout: int,
    method: str = "GET",
    payload: dict | None = None,
) -> dict:
    headers = {
        "Accept": "application/json",
        "User-Agent": "bmshop-ai/1.0",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        # The body of a provider error can contain the key in rare setups, so
        # only the status is reported.
        raise ProviderRequestError(f"http_{error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ProviderRequestError(type(error).__name__) from error
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError) as error:
        raise ProviderRequestError("bad_json") from error
    if not isinstance(parsed, dict):
        raise ProviderRequestError("bad_json")
    return parsed


class ProviderRequestError(Exception):
    """A provider request failed; the reason is a short, safe code."""

    def __init__(self, reason: str = "unknown") -> None:
        super().__init__(reason)
        self.reason = reason


def list_remote_models(
    provider: AIProvider, *, timeout: int | None = None
) -> list[dict]:
    """Ask a provider which models it offers.

    Returns a list of ``{"id": ..., "label": ...}``.  Raises
    :class:`ProviderRequestError` when the provider cannot be reached or
    refuses the credential.
    """
    if not provider.base_url:
        raise ProviderRequestError("no_base_url")
    key = credential_for(provider)
    url = f"{provider.base_url.rstrip('/')}/models"
    body = _http_json(
        url,
        key=key,
        timeout=timeout or int(current_app.config.get("AI_TIMEOUT_SECONDS", 20)),
    )
    entries = body.get("data")
    if not isinstance(entries, list):
        raise ProviderRequestError("bad_response")
    models: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identifier = entry.get("id")
        if not identifier:
            continue
        models.append({
            "id": str(identifier),
            "label": str(entry.get("name") or identifier),
        })
    models.sort(key=lambda item: item["id"].lower())
    return models


def test_provider(
    provider: AIProvider, *, timeout: int | None = None
) -> ConnectionResult:
    """Check that the provider answers and that the credential works."""
    import time

    started = time.perf_counter()
    if not provider.base_url:
        return ConnectionResult(
            ok=False,
            message_key="ai.test_no_base_url",
            error_key="ai.test_no_base_url",
        )
    if provider.needs_key and not provider.has_key:
        return ConnectionResult(
            ok=False,
            message_key="ai.test_no_key",
            error_key="ai.test_no_key",
        )
    try:
        models = list_remote_models(provider, timeout=timeout)
    except ai_keys.KeyUnavailable:
        return ConnectionResult(
            ok=False,
            message_key="ai.test_key_unreadable",
            error_key="ai.test_key_unreadable",
        )
    except ProviderRequestError as error:
        return ConnectionResult(
            ok=False,
            message_key="ai.test_failed",
            detail=error.reason,
            error_key="ai.test_failed",
        )
    latency = int((time.perf_counter() - started) * 1000)
    return ConnectionResult(
        ok=True,
        message_key="ai.test_ok",
        detail=str(len(models)),
        models=models,
        latency_ms=latency,
    )


def usable_providers() -> Iterable[AIProvider]:
    """Every active provider that could actually answer a question."""
    for provider in list_providers(only_active=True):
        if provider.active_model is None:
            continue
        if provider.needs_key and not provider.has_key:
            continue
        yield provider
