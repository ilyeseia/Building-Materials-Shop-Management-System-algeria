"""Audit trail viewer (read-only)."""
from __future__ import annotations

from flask import Blueprint, render_template, request
from sqlalchemy import select

from app.extensions import db
from app.models import AuditEvent, User
from app.blueprints.helpers import page_args, parse_date, parse_int
from app.security import requires
from app.services import reporting

bp = Blueprint("audit", __name__)


@bp.route("/audit")
@requires("audit.view")
def index():
    page, per_page = page_args(50)
    query = select(AuditEvent).order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    action = (request.args.get("action") or "").strip()
    entity_type = (request.args.get("entity") or "").strip()
    actor_id = parse_int(request.args.get("actor"))
    date_from = parse_date(request.args.get("from"))
    date_to = parse_date(request.args.get("to"))
    if action:
        query = query.where(AuditEvent.action == action)
    if entity_type:
        query = query.where(AuditEvent.entity_type == entity_type)
    if actor_id:
        query = query.where(AuditEvent.actor_id == actor_id)
    if date_from:
        query = query.where(AuditEvent.created_at >= date_from)
    result = reporting.paginate(query, page, per_page)

    rows = [
        {
            "when": event.created_at.strftime("%d/%m/%Y %H:%M:%S"),
            "actor": event.actor_name or "—",
            "action": event.action,
            "entity": f"{event.entity_type}#{event.entity_id}" if event.entity_id else event.entity_type,
            "summary": event.summary or "",
            "ip": event.ip_address or "",
        }
        for event in result["items"]
    ]
    users = db.session.execute(select(User).order_by(User.username)).scalars().all()
    entities = db.session.execute(
        select(AuditEvent.entity_type).distinct().order_by(AuditEvent.entity_type)
    ).scalars().all()
    actions = db.session.execute(
        select(AuditEvent.action).distinct().order_by(AuditEvent.action)
    ).scalars().all()
    return render_template(
        "audit/list.html", rows=rows, result=result,
        users=users, entities=entities, actions=actions,
        filters={"action": action, "entity": entity_type, "actor": actor_id},
    )
