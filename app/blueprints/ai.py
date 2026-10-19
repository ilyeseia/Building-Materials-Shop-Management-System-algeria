"""Read-only AI workspace.

The route only orchestrates: permission checks live in ``@requires`` and in each
tool, the provider call and the local fallback live in ``app.services.ai``, and
the transaction/audit handling stays here so a provider failure can never roll
back a business operation.

Nothing on this page writes to the business data: even the question history is a
separate append-only audit table.
"""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from config import Config
from app.extensions import db
from app.forms import AIQuestionForm
from app.i18n import translate
from app.security import rate_limit, requires
from app.services import ai as ai_service
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
    try:
        result = ai_service.ask(question, _current_user())
        interaction = ai_service.record(_current_user(), question, result)
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
        flash(translate("ai.provider_fallback"), "warning")
    elif result.status == ai_service.AI_STATUS_REFUSED:
        flash(result.answer, "warning")
    return redirect(url_for("ai.index", interaction=interaction.id))


def _render(form: AIQuestionForm):
    history = ai_service.history_for(_current_user(), limit=10)
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
        tools=ai_service.available_tools(_current_user()),
        provider=ai_service.provider_info(),
        enabled=True,
    )


def _current_user():
    from flask_login import current_user

    return current_user
