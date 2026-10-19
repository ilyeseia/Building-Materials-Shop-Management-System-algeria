"""AI workspace tests: permissions, read-only tools, audit and fallback.

The external provider is always mocked.  No test reaches the network, and no test
asserts that a write happened: the workspace must never write business data.
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import AIInteraction, AuditEvent, Product, Role
from app.services import ai as ai_service
from tests.conftest import give_stock


def test_ai_page_requires_ai_permission(client, make_user, login):
    driver = make_user("driver-ai", Role.DRIVER)
    db.session.commit()
    login(driver)
    response = client.get("/ai")
    assert response.status_code == 403


def test_ai_page_renders_for_admin(auth_client):
    response = auth_client.get("/ai")
    assert response.status_code == 200
    assert b'action="/ai/ask"' in response.data
    assert b"csrf_token" in response.data


def test_local_overview_uses_tools_and_returns_answer(app, admin_user, product, warehouse):
    give_stock(product, warehouse, "5")
    db.session.commit()
    with app.test_request_context("/ai"):
        result = ai_service.ask("اعرض ملخص النشاط", admin_user)
    assert result.status == "ok"
    assert result.provider == "local"
    assert result.tool_calls
    assert result.tool_calls[0]["tool"] == "business_overview"
    assert result.answer


def test_tool_permission_is_enforced_server_side(app, make_user):
    cashier = make_user("cashier-ai", Role.CASHIER)
    db.session.commit()
    with app.test_request_context("/ai"):
        tools = ai_service.available_tools(cashier)
        assert "low_stock" not in {tool["name"] for tool in tools}
        ok, payload, error = ai_service.execute_tool(cashier, "low_stock", {})
    assert ok is False
    assert payload is None
    assert error == "ai.tool_forbidden"


def test_unknown_write_tool_is_refused(app, admin_user):
    with app.test_request_context("/ai"):
        ok, payload, error = ai_service.execute_tool(
            admin_user, "delete_product", {"product_id": 1}
        )
    assert ok is False
    assert payload is None
    assert error == "ai.tool_unknown"
    # The registry itself contains only read-only tools.
    assert set(ai_service.TOOL_SPECS) == {
        "business_overview", "search_products", "product_stock", "low_stock",
        "sales_summary", "receivables", "payables", "recent_invoices",
    }


def test_ask_route_records_interaction_and_audit(auth_client, app, product):
    before_products = db.session.query(Product).count()
    response = auth_client.post(
        "/ai/ask",
        data={"question": "ما هو المخزون المنخفض؟"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        interaction = db.session.query(AIInteraction).one()
        assert interaction.question == "ما هو المخزون المنخفض؟"
        assert interaction.status == "ok"
        assert interaction.tool_calls
        assert db.session.query(AuditEvent).filter_by(action="ai_query").count() == 1
        assert db.session.query(Product).count() == before_products


def test_provider_failure_falls_back_to_local(app, admin_user, monkeypatch):
    app.config.update(
        AI_PROVIDER="openai",
        AI_API_KEY="test-key-not-real",
        AI_BASE_URL="https://provider.invalid/v1",
    )

    def fail(_messages):
        raise ai_service.ProviderError("ai.provider_unavailable")

    monkeypatch.setattr(ai_service, "_chat_completion", fail)
    with app.test_request_context("/ai"):
        result = ai_service.ask("ملخص النشاط", admin_user)
    assert result.status == "unavailable"
    assert result.provider == "local"
    assert result.error_key == "ai.provider_unavailable"
    assert result.answer


def test_provider_tool_call_and_untrusted_tool_output(app, admin_user, product, monkeypatch):
    app.config.update(AI_PROVIDER="openai", AI_API_KEY="test-key-not-real")
    seen: list[list[dict]] = []

    def fake_chat(messages):
        seen.append(list(messages))
        if len(seen) == 1:
            return json.dumps({"tool": "search_products", "args": {"query": "Sable"}})
        # A hostile provider might smuggle instructions into a tool result.  The
        # assistant must receive it as a ``tool`` role, never as a system message.
        return json.dumps({"answer": "تم العثور على المنتج."})

    monkeypatch.setattr(ai_service, "_chat_completion", fake_chat)
    with app.test_request_context("/ai"):
        result = ai_service.ask("ابحث عن Sable", admin_user)
    assert result.status == "ok"
    assert result.provider == "openai"
    assert result.tool_calls[0]["tool"] == "search_products"
    # The second provider call must include the untrusted tool output as a tool role.
    roles = [message["role"] for message in seen[1]]
    assert "system" in roles and "tool" in roles
    system_prompt = next(message["content"] for message in seen[0] if message["role"] == "system")
    assert "untrusted data" in system_prompt
    assert "never" in system_prompt.lower()


def test_disabled_workspace_redirects(auth_client, app):
    from app.services import settings as settings_service

    settings = settings_service.ensure_settings()
    settings.enable_ai = False
    db.session.commit()
    response = auth_client.get("/ai", follow_redirects=False)
    assert response.status_code == 302
    assert "/dashboard" in response.headers["Location"] or response.headers["Location"].endswith("/")


def test_settings_service_persists_ai_switch(app):
    """The settings whitelist must not silently drop the AI switch."""
    from app.services import settings as settings_service

    settings_service.ensure_settings()
    db.session.commit()
    settings_service.update_settings({"enable_ai": False})
    db.session.commit()
    db.session.expire_all()
    assert settings_service.get_settings().enable_ai is False

    settings_service.update_settings({"enable_ai": True})
    db.session.commit()
    db.session.expire_all()
    assert settings_service.get_settings().enable_ai is True


def test_empty_question_is_refused(app, admin_user):
    with app.test_request_context("/ai"):
        result = ai_service.ask("   ", admin_user)
    assert result.status == "refused"
    assert result.error_key == "ai.empty_question"


def test_long_question_is_truncated_not_rejected(app, admin_user):
    with app.test_request_context("/ai"):
        result = ai_service.ask("س" * 5000, admin_user)
    assert result.status in {"ok", "unavailable"}
    assert len(result.answer) <= 8000


def test_ai_interaction_model_is_append_only_shape(app, admin_user):
    interaction = AIInteraction(
        user_id=admin_user.id,
        question="q",
        answer="a",
        provider="local",
        status="ok",
        tool_calls="[]",
        latency_ms=1,
    )
    db.session.add(interaction)
    db.session.commit()
    assert interaction.id
    assert interaction.created_at
