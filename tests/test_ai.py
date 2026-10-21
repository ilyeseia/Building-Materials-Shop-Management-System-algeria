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


# ---------------------------------------------------------------------------
# The flexible workspace: model selection, general mode, follow-ups, errors
# ---------------------------------------------------------------------------

def test_workspace_is_a_flexible_conversation(auth_client):
    response = auth_client.get("/ai")
    assert response.status_code == 200
    assert b'name="target"' in response.data
    assert b'name="general_mode"' in response.data
    assert b"data-fill-question" in response.data
    assert b"data-autogrow" in response.data
    # The old "read-only" framing is gone from the visible heading.
    assert "الأدوات المتاحة للقراءة فقط".encode("utf-8") not in response.data


def test_general_mode_uses_the_provider_without_any_tool(app, admin_user, monkeypatch):
    app.config.update(
        AI_PROVIDER="openai", AI_API_KEY="test-key", AI_BASE_URL="https://provider.test/v1",
        AI_MODEL="test-model",
    )
    seen: list[list[dict]] = []

    def fake_chat(messages):
        seen.append(list(messages))
        return "Bonjour, comment puis-je aider ?"

    monkeypatch.setattr(ai_service, "_chat_completion", fake_chat)
    with app.test_request_context("/ai"):
        result = ai_service.ask("Bonjour", admin_user, use_data=False)

    assert result.status == "ok"
    assert result.provider == "openai"
    assert result.tool_calls == []
    system_prompt = seen[0][0]["content"]
    assert "no access to the shop's data" in system_prompt
    assert "Available tools" not in system_prompt


def test_general_mode_without_a_provider_says_so(app, admin_user):
    with app.test_request_context("/ai"):
        result = ai_service.ask("Bonjour", admin_user, use_data=False)
    assert result.status == "unavailable"
    assert result.error_key == "ai.general_needs_provider"
    assert result.answer


def test_an_explicit_model_selection_is_honoured(app, admin_user, monkeypatch):
    from app.services import ai_providers

    with app.test_request_context("/ai"):
        provider = ai_providers.create_provider(
            code="chooser", name="Chooser", base_url="https://chooser.test/v1", api_key="sk-x"
        )
        first = ai_providers.create_model(provider, code="m1", is_default=True)
        second = ai_providers.create_model(provider, code="m2", sort_order=1)
        db.session.commit()

        attempted: list[str] = []

        def fake_chat(_messages):
            attempted.append(ai_service._PROVIDER_TARGET.get().model)
            return '{"answer": "ok"}'

        monkeypatch.setattr(ai_service, "_chat_completion", fake_chat)
        result = ai_service.ask(
            "anything", admin_user, target_key=f"p{provider.id}:m{second.id}"
        )

    assert attempted == ["m2"]
    assert result.model == "m2"
    assert result.status == "ok"
    assert first.id != second.id


def test_a_rejected_model_reports_the_provider_message(app, admin_user, monkeypatch):
    from app.services import ai_providers

    with app.test_request_context("/ai"):
        provider = ai_providers.create_provider(
            code="rejecting", name="Rejecting", base_url="https://reject.test/v1", api_key="sk-x"
        )
        ai_providers.create_model(provider, code="decisions-only")
        db.session.commit()

        def fail(_messages):
            raise ai_service.ProviderError(
                "ai.provider_rejected", "HTTP 400: this is a decisions model"
            )

        monkeypatch.setattr(ai_service, "_chat_completion", fail)
        result = ai_service.ask("ملخص النشاط", admin_user)

    assert result.status == "unavailable"
    assert result.error_key == "ai.provider_rejected"
    assert result.error_detail == "HTTP 400: this is a decisions model"
    assert result.error_provider == "Rejecting"
    assert result.answer
    interaction = ai_service.record(admin_user, "ملخص النشاط", result)
    db.session.commit()
    assert interaction.error_detail == "HTTP 400: this is a decisions model"


def test_the_last_turns_are_replayed_for_a_follow_up(app, admin_user, monkeypatch):
    app.config.update(
        AI_PROVIDER="openai", AI_API_KEY="test-key", AI_BASE_URL="https://provider.test/v1",
    )
    seen: list[list[dict]] = []

    def fake_chat(messages):
        seen.append(list(messages))
        return '{"answer": "suite"}'

    monkeypatch.setattr(ai_service, "_chat_completion", fake_chat)
    with app.test_request_context("/ai"):
        ai_service.ask(
            "et le mois dernier ?",
            admin_user,
            history=[("ventes de ce mois", "12 000 DZD")],
        )

    roles = [message["role"] for message in seen[0]]
    assert roles[0] == "system"
    assert roles[1] == "user" and seen[0][1]["content"] == "ventes de ce mois"
    assert roles[2] == "assistant" and seen[0][2]["content"] == "12 000 DZD"
    assert roles[-1] == "user" and seen[0][-1]["content"] == "et le mois dernier ?"


def test_a_provider_error_body_cannot_leak_a_key():
    detail = ai_service._safe_provider_detail(
        "invalid key sk-abcdefghijklmnop and Authorization: Bearer abcdefghijklmnop"
    )
    assert "sk-abcdefghijklmnop" not in detail
    assert "abcdefghijklmnop" not in detail
    assert "[redacted]" in detail
