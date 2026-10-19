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

    user = db.relationship("User", lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<AIInteraction {self.id} {self.status}>"
