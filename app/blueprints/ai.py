"""Read-only AI workspace and the provider/model administration.

The route only orchestrates: permission checks live in ``@requires`` and in each
tool, the provider call and the local fallback live in ``app.services.ai``, and
the transaction/audit handling stays here so a provider failure can never roll
back a business operation.

Nothing on this page writes to the business data: even the question history is a
separate append-only audit table.

The administration part (``/ai/providers`` and ``/ai/models``) manages which
OpenAI-compatible endpoints the assistant may use.  API keys are stored
encrypted and are never rendered, logged or audited in clear.
"""
from __future__ import annotations

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from config import Config
from app.extensions import db
from app.forms import AIQuestionForm, AIProviderForm, AIModelForm
from app.i18n import translate
from app.security import rate_limit, requires
from app.services import ai as ai_service
from app.services import ai_keys, ai_providers
from app.services.errors import BusinessError

bp = Blueprint("ai", __name__)


@bp.route("/ai", methods=["GET"])
@requires("ai.view")
def index():
    if not ai_service.is_enabled():
        flash(translate("ai.disabled"), "warning")
        return redirect(url_for("dashboard.index"))

    return _render(AIQuestionForm())


@bp.post("/ai/ask")
@requires("ai.view")
@rate_limit(Config.AI_RATE_LIMIT_PER_HOUR, 3600, scope="ai.ask", by_user=True)
def ask():
    if not ai_service.is_enabled():
        abort(404)
    form = AIQuestionForm()
    if not form.validate_on_submit():
        for field_errors in form.errors.values():
            for error in field_errors:
                flash(translate(error) if "." in str(error) else str(error), "danger")
        return redirect(url_for("ai.index"))
    return _answer(form)


def _answer(form: AIQuestionForm):
    if not ai_service.is_enabled():
        abort(404)
    question = form.question.data or ""
    user = _current_user()
    target_key = (form.target.data or "").strip() or ai_service.AUTO_TARGET
    known = {option["key"] for option in ai_service.target_options()}
    if target_key not in known and target_key not in {ai_service.AUTO_TARGET, ai_service.LOCAL_TARGET}:
        flash(translate("ai.target_missing"), "warning")
        target_key = ai_service.AUTO_TARGET
    try:
        result = ai_service.ask(
            question,
            user,
            target_key=target_key,
            use_data=not bool(form.general_mode.data),
            history=_conversation(user),
        )
        interaction = ai_service.record(user, question, result)
        db.session.commit()
    except BusinessError as error:
        db.session.rollback()
        flash(error.message(), "danger")
        return redirect(url_for("ai.index"))
    except Exception:  # noqa: BLE001 - provider/DB failure must not 500 the workspace
        db.session.rollback()
        from flask import current_app

        current_app.logger.exception("AI workspace failed to answer a question")
        flash(translate("ai.error"), "danger")
        return redirect(url_for("ai.index"))

    if result.status == ai_service.AI_STATUS_UNAVAILABLE:
        if result.error_key == "ai.general_needs_provider":
            flash(translate("ai.general_needs_provider"), "warning")
        else:
            flash(translate("ai.provider_fallback"), "warning")
            if result.error_detail and _can_manage():
                flash(
                    translate(
                        "ai.provider_fallback_detail",
                        provider=result.error_provider or translate("ai.provider_external"),
                        detail=result.error_detail[:300],
                    ),
                    "danger",
                )
    elif result.status == ai_service.AI_STATUS_REFUSED:
        flash(result.answer, "warning")
    return redirect(url_for("ai.index", interaction=interaction.id))


def _conversation(user) -> list[tuple[str, str]]:
    """The last few exchanges, oldest first, for a follow-up question."""
    try:
        rows = ai_service.history_for(user, limit=3)
    except Exception:  # noqa: BLE001 - history is a convenience, never a blocker
        return []
    return [(row.question, row.answer or "") for row in reversed(rows)]


def _can_manage() -> bool:
    from app.security import has_permission

    return has_permission(_current_user(), "ai.manage")


def _render(form: AIQuestionForm):
    user = _current_user()
    history = ai_service.history_for(user, limit=10)
    latest = None
    interaction_id = request.args.get("interaction", type=int)
    if interaction_id:
        latest = next((row for row in history if row.id == interaction_id), None)
    if latest is None and history:
        latest = history[0]
    return render_template(
        "ai/index.html",
        form=form,
        history=history,
        latest=latest,
        tools=ai_service.available_tools(user),
        provider=ai_service.provider_info(),
        targets=ai_service.target_options(),
        can_manage=_can_manage(),
        enabled=True,
    )


def _current_user():
    from flask_login import current_user

    return current_user


# ======================================================================
# Provider administration
# ======================================================================

@bp.route("/ai/providers")
@requires("ai.manage")
def providers():
    """The registered providers and which one is in use."""
    overview = ai_service.provider_overview()
    return render_template(
        "ai/providers.html",
        title_key="ai.providers_title",
        providers=ai_providers.list_providers(),
        default_provider=overview.get("default"),
        targets=overview.get("targets", []),
        environment=_environment_status(),
        catalog=ai_providers.catalog(),
    )


@bp.route("/ai/providers/new", methods=["GET", "POST"])
@requires("ai.manage")
def provider_new():
    form = AIProviderForm()
    _fill_catalog_choices(form)
    requested = (request.args.get("catalog") or "").strip().lower()
    entry = ai_providers.catalog_entry(requested) if requested else None
    if request.method == "GET":
        form.catalog_code.data = requested or "openrouter"
        if entry is not None:
            form.code.data = entry.code
            form.name.data = entry.name
            form.base_url.data = entry.base_url
            form.free_tier.data = entry.free_tier
            form.description.data = entry.description
    if form.validate_on_submit():
        entry = ai_providers.catalog_entry(form.catalog_code.data or "")
        code = (form.code.data or "").strip().lower()
        base_url = (form.base_url.data or "").strip()
        name = (form.name.data or "").strip()
        provider_type = "openai_compatible"
        if entry is not None:
            code = code or entry.code
            base_url = base_url or entry.base_url
            name = name or entry.name
            provider_type = entry.provider_type
        if not code:
            flash(translate("ai.provider_code_required"), "danger")
            return render_template("ai/provider_form.html", form=form,
                                   title_key="ai.provider_new", entry=entry)
        if ai_providers.get_provider_by_code(code) is not None:
            flash(translate("ai.provider_code_taken"), "danger")
            return render_template("ai/provider_form.html", form=form,
                                   title_key="ai.provider_new", entry=entry)
        provider = ai_providers.create_provider(
            code=code,
            name=name,
            base_url=base_url,
            api_key=form.api_key.data or None,
            description=form.description.data or "",
            provider_type=provider_type,
            free_tier=bool(form.free_tier.data or (entry.free_tier if entry else False)),
            is_active=bool(form.is_active.data),
            notes=form.notes.data or "",
        )
        if form.is_default.data:
            ai_providers.set_default(provider)
        _audit("created", "ai_provider", provider.id, summary=provider.code)
        db.session.commit()
        flash(translate("ai.provider_added"), "success")
        return redirect(url_for("ai.providers"))
    return render_template("ai/provider_form.html", form=form,
                           title_key="ai.provider_new", entry=entry)


@bp.route("/ai/providers/<int:provider_id>/edit", methods=["GET", "POST"])
@requires("ai.manage")
def provider_edit(provider_id: int):
    provider = ai_providers.get_provider(provider_id)
    if provider is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("ai.providers"))
    form = AIProviderForm(obj=provider)
    _fill_catalog_choices(form)
    if not form.is_submitted():
        form.catalog_code.data = ""
        form.is_default.data = provider.is_default
        form.free_tier.data = provider.free_tier
    if form.validate_on_submit():
        entry = ai_providers.catalog_entry(form.catalog_code.data or "")
        changes = {
            "name": form.name.data,
            "base_url": form.base_url.data,
            "description": form.description.data,
            "notes": form.notes.data,
            "free_tier": bool(form.free_tier.data),
            "is_active": bool(form.is_active.data),
        }
        if entry is not None and not (form.base_url.data or "").strip():
            changes["base_url"] = entry.base_url
        ai_providers.update_provider(provider, api_key=form.api_key.data, **changes)
        if form.is_default.data:
            ai_providers.set_default(provider)
        else:
            provider.is_default = False
            db.session.flush()
        _audit("updated", "ai_provider", provider.id, summary=provider.code)
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("ai.providers"))
    return render_template("ai/provider_form.html", form=form,
                           title_key="ai.provider_edit", entry=None, provider=provider)


@bp.post("/ai/providers/<int:provider_id>/delete")
@requires("ai.manage")
def provider_delete(provider_id: int):
    provider = ai_providers.get_provider(provider_id)
    if provider is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("ai.providers"))
    code = provider.code
    ai_providers.delete_provider(provider)
    _audit("deleted", "ai_provider", None, summary=code)
    db.session.commit()
    flash(translate("ai.provider_deleted"), "success")
    return redirect(url_for("ai.providers"))


@bp.post("/ai/providers/<int:provider_id>/default")
@requires("ai.manage")
def provider_default(provider_id: int):
    provider = ai_providers.get_provider(provider_id)
    if provider is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("ai.providers"))
    ai_providers.set_default(provider)
    _audit("defaulted", "ai_provider", provider.id, summary=provider.code)
    db.session.commit()
    flash(translate("ai.provider_default_set"), "success")
    return redirect(url_for("ai.providers"))


@bp.post("/ai/providers/<int:provider_id>/test")
@requires("ai.manage")
def provider_test(provider_id: int):
    """Try to reach the provider and list the models it offers."""
    provider = ai_providers.get_provider(provider_id)
    if provider is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("ai.providers"))
    result = ai_providers.test_provider(provider)
    if result.ok:
        _audit("tested", "ai_provider", provider.id,
               summary=f"{provider.code} models={len(result.models)}")
        db.session.commit()
        flash(translate(result.message_key, count=len(result.models),
                        ms=result.latency_ms), "success")
    else:
        flash(translate(result.message_key, detail=result.detail), "danger")
    return redirect(url_for("ai.providers"))


# ======================================================================
# Model administration
# ======================================================================

@bp.route("/ai/models")
@requires("ai.manage")
def models():
    provider_id = request.args.get("provider", type=int)
    return render_template(
        "ai/models.html",
        title_key="ai.models_title",
        models=ai_providers.list_models(provider_id),
        providers=ai_providers.list_providers(),
        selected_provider=provider_id,
    )


@bp.route("/ai/models/new", methods=["GET", "POST"])
@requires("ai.manage")
def model_new():
    form = AIModelForm()
    _fill_provider_choices(form)
    provider_id = request.args.get("provider", type=int)
    if not form.is_submitted() and provider_id:
        form.provider_id.data = provider_id
    selected = None
    if form.is_submitted() and form.provider_id.data:
        selected = ai_providers.get_provider(form.provider_id.data)
    elif provider_id:
        selected = ai_providers.get_provider(provider_id)
    if form.validate_on_submit():
        provider = ai_providers.get_provider(form.provider_id.data)
        if provider is None:
            flash(translate("error.not_found"), "danger")
            return redirect(url_for("ai.models"))
        model = ai_providers.create_model(
            provider,
            code=form.code.data,
            label=form.label.data or form.code.data,
            description=form.description.data or "",
            context_window=form.context_window.data,
            max_output_tokens=form.max_output_tokens.data,
            supports_tools=bool(form.supports_tools.data),
            free_tier=bool(form.free_tier.data),
            is_active=bool(form.is_active.data),
            is_default=bool(form.is_default.data),
            sort_order=form.sort_order.data or 0,
        )
        _audit("created", "ai_model", model.id, summary=f"{provider.code}/{model.code}")
        db.session.commit()
        flash(translate("ai.model_added"), "success")
        return redirect(url_for("ai.models", provider=provider.id))
    return render_template(
        "ai/model_form.html", form=form, title_key="ai.model_new",
        provider=selected, suggestions=_model_suggestions(selected),
    )


@bp.route("/ai/models/<int:model_id>/edit", methods=["GET", "POST"])
@requires("ai.manage")
def model_edit(model_id: int):
    model = ai_providers.get_model(model_id)
    if model is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("ai.models"))
    form = AIModelForm(obj=model)
    _fill_provider_choices(form)
    if not form.is_submitted():
        form.provider_id.data = model.provider_id
        form.is_default.data = model.is_default
    if form.validate_on_submit():
        provider = ai_providers.get_provider(form.provider_id.data)
        if provider is None:
            flash(translate("error.not_found"), "danger")
            return redirect(url_for("ai.models"))
        model.provider_id = provider.id
        ai_providers.update_model(
            model,
            code=form.code.data,
            label=form.label.data or form.code.data,
            description=form.description.data or "",
            context_window=form.context_window.data,
            max_output_tokens=form.max_output_tokens.data,
            supports_tools=bool(form.supports_tools.data),
            free_tier=bool(form.free_tier.data),
            is_active=bool(form.is_active.data),
            sort_order=form.sort_order.data or 0,
            is_default=bool(form.is_default.data),
        )
        _audit("updated", "ai_model", model.id, summary=f"{provider.code}/{model.code}")
        db.session.commit()
        flash(translate("common.saved"), "success")
        return redirect(url_for("ai.models", provider=provider.id))
    return render_template("ai/model_form.html", form=form,
                           title_key="ai.model_edit", provider=model.provider,
                           suggestions=_model_suggestions(model.provider))


@bp.post("/ai/models/<int:model_id>/delete")
@requires("ai.manage")
def model_delete(model_id: int):
    model = ai_providers.get_model(model_id)
    if model is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("ai.models"))
    provider_id = model.provider_id
    code = model.code
    ai_providers.delete_model(model)
    _audit("deleted", "ai_model", None, summary=code)
    db.session.commit()
    flash(translate("ai.model_deleted"), "success")
    return redirect(url_for("ai.models", provider=provider_id))


@bp.post("/ai/models/<int:model_id>/toggle")
@requires("ai.manage")
def model_toggle(model_id: int):
    model = ai_providers.get_model(model_id)
    if model is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("ai.models"))
    provider_id = model.provider_id
    model.is_active = not model.is_active
    _audit("toggled", "ai_model", model.id, summary=model.code)
    db.session.commit()
    flash(translate("common.saved"), "success")
    return redirect(url_for("ai.models", provider=provider_id))


@bp.post("/ai/models/<int:model_id>/test")
@requires("ai.manage")
def model_test(model_id: int):
    """Prove that one model accepts a chat completion before relying on it."""
    model = ai_providers.get_model(model_id)
    if model is None:
        flash(translate("error.not_found"), "danger")
        return redirect(url_for("ai.models"))
    result = ai_providers.test_model(model.provider, model.code)
    _audit(
        "tested", "ai_model", model.id,
        summary=f"{model.code} {'ok' if result.ok else 'failed'} {result.latency_ms}ms",
    )
    db.session.commit()
    if result.ok:
        flash(translate("ai.model_test_ok", ms=result.latency_ms), "success")
    else:
        flash(
            translate("ai.model_test_failed", detail=(result.detail or "")[:300]),
            "danger",
        )
    return redirect(url_for("ai.models", provider=model.provider_id))


# ======================================================================
# Helpers
# ======================================================================

def _fill_catalog_choices(form: AIProviderForm) -> None:
    form.catalog_code.choices = [("", "—")] + [
        (entry.code, entry.name) for entry in ai_providers.catalog()
    ]


def _fill_provider_choices(form: AIModelForm) -> None:
    form.provider_id.choices = [
        (provider.id, provider.name) for provider in ai_providers.list_providers()
    ]


def _model_suggestions(provider) -> list[str]:
    """Well-known model ids for the provider, as a ``<datalist>`` hint."""
    if provider is None:
        return []
    entry = ai_providers.catalog_entry(provider.code)
    return list(entry.suggested_models) if entry is not None else []


def _environment_status() -> dict:
    """The legacy environment configuration, without revealing the key."""
    provider = str(Config.AI_PROVIDER or "local").strip().lower()
    if provider == "local":
        return {"configured": False, "provider": "local", "model": None, "key_hint": None}
    key = Config.AI_API_KEY
    return {
        "configured": bool(key),
        "provider": provider,
        "model": Config.AI_MODEL,
        "key_hint": ai_keys.key_hint(key),
    }


def _audit(action: str, entity_type: str, entity_id: int | None, summary: str) -> None:
    from app.services import audit

    audit.record(action, entity_type, entity_id, summary=summary)
