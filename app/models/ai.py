"""Audited record of AI-workspace questions and answers.

The workspace is intentionally read-only: every tool exposed to the model or to
the local assistant reads business data and can never create, update or delete a
business record.  This table keeps the question, the answer and the tool calls
that produced it, so an administrator can reconstruct exactly what was asked and
what data left the application.

No provider credential is ever stored here, and answers are truncated to keep a
single runaway response from bloating the database.
"""
from __future__ import annotations

from app.extensions import db
from app.services.timeutil import utcnow

#: Status vocabulary used by the workspace and the audit trail.
AI_STATUS_OK = "ok"
AI_STATUS_UNAVAILABLE = "unavailable"
AI_STATUS_ERROR = "error"
AI_STATUS_REFUSED = "refused"


class AIInteraction(db.Model):
    """One question/answer exchange, append-only for audit purposes."""

    __tablename__ = "ai_interactions"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    question = db.Column(db.Text, nullable=False)
    answer = db.Column(db.Text)
    provider = db.Column(db.String(40), nullable=False, default="local")
    model = db.Column(db.String(80))
    status = db.Column(db.String(20), nullable=False, default=AI_STATUS_OK, index=True)
    #: JSON array of ``{tool, ok, permission, args, summary}`` entries.
    tool_calls = db.Column(db.Text)
    latency_ms = db.Column(db.Integer)
    error_key = db.Column(db.String(120))
    #: Short, secret-free provider message explaining a fallback (for example
    #: "HTTP 400: this model cannot be used with chat/completions").
    error_detail = db.Column(db.Text)

    user = db.relationship("User", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<AIInteraction {self.id} {self.status}>"


# ---------------------------------------------------------------------------
# Provider and model registry
# ---------------------------------------------------------------------------

#: Every supported provider speaks the OpenAI ``/chat/completions`` dialect.
AI_PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"

#: A provider that needs no credential at all (a local runtime).
AI_PROVIDER_LOCAL_RUNTIME = "local_runtime"


class AIProvider(db.Model):
    """An OpenAI-compatible endpoint the assistant may talk to.

    The credential is stored encrypted (see ``app.services.ai_keys``) and is
    never rendered, logged or returned by the API.  Only a short hint such as
    the last four characters is kept in clear so the administrator can tell
    two keys apart without the secret ever being displayed.
    """

    __tablename__ = "ai_providers"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    provider_type = db.Column(
        db.String(30), nullable=False, default=AI_PROVIDER_OPENAI_COMPATIBLE
    )
    base_url = db.Column(db.String(255), nullable=False)
    #: Fernet ciphertext.  ``None`` for a local runtime that needs no key.
    api_key_encrypted = db.Column(db.Text)
    #: Last four characters of the key, for display only.
    key_hint = db.Column(db.String(16))
    #: The provider offers a free quota; shown as a badge in the interface.
    free_tier = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False, index=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    models = db.relationship(
        "AIModel",
        backref="provider",
        cascade="all, delete-orphan",
        order_by="AIModel.sort_order, AIModel.id",
        lazy="select",
    )

    @property
    def needs_key(self) -> bool:
        return self.provider_type != AI_PROVIDER_LOCAL_RUNTIME

    @property
    def has_key(self) -> bool:
        return bool(self.api_key_encrypted)

    @property
    def active_model(self):
        """The model that would be used for this provider, if any."""
        for model in self.models:
            if model.is_active and model.is_default:
                return model
        for model in self.models:
            if model.is_active:
                return model
        return None

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<AIProvider {self.code} active={self.is_active}>"


class AIModel(db.Model):
    """One model offered by a provider.

    ``code`` is the identifier sent to the provider (``gpt-4o-mini``,
    ``meta-llama/llama-3.3-70b-instruct:free``, ...).  ``label`` is what the
    administrator sees.
    """

    __tablename__ = "ai_models"

    id = db.Column(db.Integer, primary_key=True)
    provider_id = db.Column(
        db.Integer, db.ForeignKey("ai_providers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    code = db.Column(db.String(120), nullable=False, index=True)
    label = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    context_window = db.Column(db.Integer)
    max_output_tokens = db.Column(db.Integer)
    supports_tools = db.Column(db.Boolean, nullable=False, default=True)
    free_tier = db.Column(db.Boolean, nullable=False, default=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    is_default = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<AIModel {self.provider_id}:{self.code}>"
