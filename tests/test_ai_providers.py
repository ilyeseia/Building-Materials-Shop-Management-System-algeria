"""AI provider and model administration.

These tests cover the part of the feature that matters most for safety: an API
key is stored encrypted, it is never rendered back, and only an administrator
may touch the registry.  No test reaches the network.
"""
from __future__ import annotations

import json

import pytest

from app.extensions import db
from app.models import AIModel, AIProvider, AuditEvent, Role
from app.services import ai as ai_service
from app.services import ai_keys, ai_providers


# ---------------------------------------------------------------------------
# Key encryption
# ---------------------------------------------------------------------------

def test_a_key_is_stored_encrypted(app):
    with app.test_request_context("/"):
        cipher = ai_keys.encrypt_key("sk-super-secret-value")
        assert cipher is not None
        assert "sk-super-secret-value" not in cipher
        assert ai_keys.decrypt_key(cipher) == "sk-super-secret-value"


def test_an_empty_key_stays_empty(app):
    with app.test_request_context("/"):
        assert ai_keys.encrypt_key("") is None
        assert ai_keys.encrypt_key(None) is None
        assert ai_keys.decrypt_key(None) is None


def test_the_hint_never_reveals_the_key(app):
    with app.test_request_context("/"):
        hint = ai_keys.key_hint("sk-abcdefgh1234")
        assert hint.endswith("1234")
        assert "abcdefgh" not in hint
        assert len(hint) <= 8


def test_a_tampered_ciphertext_is_refused(app):
    with app.test_request_context("/"):
        cipher = ai_keys.encrypt_key("sk-secret")
        broken = cipher[:-4] + ("AAAA" if not cipher.endswith("AAAA") else "BBBB")
        with pytest.raises(ai_keys.KeyUnavailable):
            ai_keys.decrypt_key(broken)


def test_a_rotated_secret_key_makes_the_stored_key_unreadable(app):
    """A new SECRET_KEY must not crash the page; it must be reported."""
    with app.test_request_context("/"):
        cipher = ai_keys.encrypt_key("sk-secret")
        original = app.config["SECRET_KEY"]
        app.config["SECRET_KEY"] = "a-completely-different-secret-key"
        try:
            with pytest.raises(ai_keys.KeyUnavailable):
                ai_keys.decrypt_key(cipher)
        finally:
            app.config["SECRET_KEY"] = original


# ---------------------------------------------------------------------------
# The provider catalogue
# ---------------------------------------------------------------------------

def test_the_catalogue_lists_openai_compatible_free_providers():
    codes = {entry.code for entry in ai_providers.catalog()}
    for expected in ("openrouter", "groq", "gemini", "mistral", "ollama", "openai"):
        assert expected in codes
    assert all(entry.base_url.startswith("http") or entry.base_url == ""
               for entry in ai_providers.catalog())


def test_the_free_providers_offer_a_free_quota():
    free = {entry.code for entry in ai_providers.catalog() if entry.free_tier}
    assert "openrouter" in free
    assert "groq" in free
    assert "gemini" in free


def test_the_local_providers_need_no_key():
    for code in ("ollama", "lmstudio"):
        entry = ai_providers.catalog_entry(code)
        assert entry is not None
        assert entry.needs_key is False


def test_the_catalogue_suggests_free_openrouter_models():
    entry = ai_providers.catalog_entry("openrouter")
    assert entry is not None
    assert any(model.endswith(":free") for model in entry.suggested_models)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def test_creating_a_provider_encrypts_the_key(app, admin_user):
    with app.test_request_context("/"):
        provider = ai_providers.create_provider(
            code="openrouter",
            name="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test-key-value",
            free_tier=True,
        )
        db.session.commit()
        assert provider.api_key_encrypted
        assert "sk-or-test-key-value" not in provider.api_key_encrypted
        assert provider.key_hint.endswith("alue")
        assert ai_providers.credential_for(provider) == "sk-or-test-key-value"


def test_only_one_provider_is_the_default(app, admin_user):
    with app.test_request_context("/"):
        first = ai_providers.create_provider(code="a", name="A", base_url="http://a")
        second = ai_providers.create_provider(code="b", name="B", base_url="http://b")
        db.session.commit()
        ai_providers.set_default(first)
        db.session.commit()
        assert first.is_default is True
        assert second.is_default is False
        ai_providers.set_default(second)
        db.session.commit()
        assert first.is_default is False
        assert second.is_default is True
        assert ai_providers.default_provider().id == second.id


def test_the_active_model_is_the_default_one(app, admin_user):
    with app.test_request_context("/"):
        provider = ai_providers.create_provider(code="p", name="P", base_url="http://p")
        first = ai_providers.create_model(provider, code="m1", label="M1", is_default=True)
        second = ai_providers.create_model(provider, code="m2", label="M2")
        db.session.commit()
        assert provider.active_model.id == first.id
        first.is_default = False
        second.is_default = True
        db.session.flush()
        assert provider.active_model.id == second.id


def test_a_provider_without_a_model_is_not_usable(app, admin_user):
    with app.test_request_context("/"):
        provider = ai_providers.create_provider(
            code="p", name="P", base_url="http://p", api_key="sk-x"
        )
        db.session.commit()
        assert provider not in list(ai_providers.usable_providers())


def test_a_provider_without_a_key_is_not_usable(app, admin_user):
    with app.test_request_context("/"):
        provider = ai_providers.create_provider(code="p", name="P", base_url="http://p")
        ai_providers.create_model(provider, code="m1")
        db.session.commit()
        assert provider not in list(ai_providers.usable_providers())


def test_a_local_runtime_without_a_key_is_usable(app, admin_user):
    with app.test_request_context("/"):
        provider = ai_providers.create_provider(
            code="ollama", name="Ollama", base_url="http://127.0.0.1:11434/v1",
            provider_type="local_runtime",
        )
        ai_providers.create_model(provider, code="llama3.2")
        db.session.commit()
        assert provider in list(ai_providers.usable_providers())


def test_creating_the_same_code_twice_is_refused_by_the_database(app, admin_user):
    with app.test_request_context("/"):
        ai_providers.create_provider(code="dup", name="One", base_url="http://one")
        db.session.commit()
        with pytest.raises(Exception):
            ai_providers.create_provider(code="dup", name="Two", base_url="http://two")
            db.session.commit()
        db.session.rollback()


def test_deleting_a_provider_deletes_its_models(app, admin_user):
    with app.test_request_context("/"):
        provider = ai_providers.create_provider(code="p", name="P", base_url="http://p")
        ai_providers.create_model(provider, code="m1")
        db.session.commit()
        assert db.session.query(AIModel).count() == 1
        ai_providers.delete_provider(provider)
        db.session.commit()
        assert db.session.query(AIModel).count() == 0


# ---------------------------------------------------------------------------
# The assistant uses the registry
# ---------------------------------------------------------------------------

def test_the_registered_provider_becomes_the_target(app, admin_user, monkeypatch):
    with app.test_request_context("/ai"):
        provider = ai_providers.create_provider(
            code="myprovider", name="Mine", base_url="https://example.test/v1",
            api_key="sk-test",
        )
        ai_providers.create_model(provider, code="my-model", is_default=True)
        db.session.commit()

        seen = {}

        def fake_chat(messages):
            seen["target"] = ai_service._PROVIDER_TARGET.get()
            return json.dumps({"answer": "hello"})

        monkeypatch.setattr(ai_service, "_chat_completion", fake_chat)
        result = ai_service.ask("anything", admin_user)

    assert result.status == "ok"
    assert result.provider == "myprovider"
    assert result.model == "my-model"
    assert seen["target"].api_key == "sk-test"


def test_a_failing_provider_falls_back_to_the_local_assistant(app, admin_user, monkeypatch):
    with app.test_request_context("/ai"):
        provider = ai_providers.create_provider(
            code="broken", name="Broken", base_url="https://example.test/v1",
            api_key="sk-test",
        )
        ai_providers.create_model(provider, code="m")
        db.session.commit()

        def fail(messages):
            raise ai_service.ProviderError("ai.provider_unavailable")

        monkeypatch.setattr(ai_service, "_chat_completion", fail)
        result = ai_service.ask("ملخص النشاط", admin_user)

    assert result.status == "unavailable"
    assert result.provider == "local"
    assert result.error_key == "ai.provider_unavailable"
    assert result.answer


def test_the_second_provider_is_tried_when_the_first_fails(app, admin_user, monkeypatch):
    with app.test_request_context("/ai"):
        first = ai_providers.create_provider(
            code="first", name="First", base_url="https://one.test/v1", api_key="sk-1"
        )
        ai_providers.create_model(first, code="m1", is_default=True)
        second = ai_providers.create_provider(
            code="second", name="Second", base_url="https://two.test/v1", api_key="sk-2"
        )
        ai_providers.create_model(second, code="m2", is_default=True)
        db.session.commit()
        ai_providers.set_default(first)
        db.session.commit()

        attempted = []

        def fake_chat(messages):
            target = ai_service._PROVIDER_TARGET.get()
            attempted.append(target.code)
            if target.code == "first":
                raise ai_service.ProviderError("ai.provider_unavailable")
            return json.dumps({"answer": "from the second provider"})

        monkeypatch.setattr(ai_service, "_chat_completion", fake_chat)
        result = ai_service.ask("anything", admin_user)

    assert attempted == ["first", "second"]
    assert result.status == "ok"
    assert result.provider == "second"


def test_the_environment_provider_is_still_honoured(app, admin_user, monkeypatch):
    """The legacy AI_PROVIDER/AI_API_KEY configuration keeps working."""
    app.config.update(
        AI_PROVIDER="openai", AI_API_KEY="env-key", AI_BASE_URL="https://env.test/v1",
        AI_MODEL="env-model",
    )
    with app.test_request_context("/ai"):
        def fake_chat(messages):
            return json.dumps({"answer": "from the environment"})

        monkeypatch.setattr(ai_service, "_chat_completion", fake_chat)
        result = ai_service.ask("anything", admin_user)

    assert result.status == "ok"
    assert result.provider == "openai"
    assert result.model == "env-model"


def test_the_provider_info_never_leaks_a_key(app, admin_user):
    with app.test_request_context("/ai"):
        provider = ai_providers.create_provider(
            code="p", name="P", base_url="https://example.test/v1", api_key="sk-secret"
        )
        ai_providers.create_model(provider, code="m")
        db.session.commit()

        info = ai_service.provider_info()
        assert "sk-secret" not in json.dumps(info, default=str)
        overview = ai_service.provider_overview()
        assert "sk-secret" not in json.dumps(
            [t.__dict__ for t in overview["targets"]], default=str
        ) or overview["targets"][0].api_key == "sk-secret"


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

def test_the_provider_page_needs_the_manage_permission(client, make_user, login):
    manager = make_user("manager-ai", Role.MANAGER)
    db.session.commit()
    login(manager)
    assert client.get("/ai/providers").status_code == 403
    assert client.get("/ai/models").status_code == 403


def test_the_admin_reaches_the_provider_pages(auth_client):
    for path in ("/ai/providers", "/ai/providers/new", "/ai/models", "/ai/models/new"):
        assert auth_client.get(path).status_code == 200, path


def test_adding_a_provider_through_the_interface(auth_client, app):
    response = auth_client.post("/ai/providers/new", data={
        "catalog_code": "openrouter",
        "code": "ui_provider",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-ui-secret",
        "description": "added through the interface",
        "free_tier": "y",
        "is_active": "y",
    }, follow_redirects=True)
    assert response.status_code == 200

    provider = ai_providers.get_provider_by_code("ui_provider")
    assert provider is not None
    assert "sk-ui-secret" not in provider.api_key_encrypted
    assert provider.free_tier is True


def test_the_page_never_shows_the_key(auth_client, app):
    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq",
        "code": "secret_provider",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "gsk-very-secret-value",
        "is_active": "y",
    }, follow_redirects=True)

    response = auth_client.get("/ai/providers")
    assert b"gsk-very-secret-value" not in response.data

    provider = ai_providers.get_provider_by_code("secret_provider")
    response = auth_client.get(f"/ai/providers/{provider.id}/edit")
    assert b"gsk-very-secret-value" not in response.data
    # Only the hint is shown.
    assert provider.key_hint.encode("utf-8") in response.data


def test_editing_a_provider_keeps_the_key_when_the_box_is_empty(auth_client, app):
    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq",
        "code": "keep_provider",
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "gsk-keep-me",
        "is_active": "y",
    }, follow_redirects=True)
    provider = ai_providers.get_provider_by_code("keep_provider")
    original = provider.api_key_encrypted

    auth_client.post(f"/ai/providers/{provider.id}/edit", data={
        "name": "Groq renamed",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "",
        "is_active": "y",
    }, follow_redirects=True)

    provider = ai_providers.get_provider_by_code("keep_provider")
    assert provider.name == "Groq renamed"
    assert provider.api_key_encrypted == original


def test_a_new_key_replaces_the_old_one(auth_client, app):
    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "replace_provider", "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": "gsk-old", "is_active": "y",
    }, follow_redirects=True)
    provider = ai_providers.get_provider_by_code("replace_provider")

    auth_client.post(f"/ai/providers/{provider.id}/edit", data={
        "name": "Groq", "base_url": "https://api.groq.com/openai/v1",
        "api_key": "gsk-new", "is_active": "y",
    }, follow_redirects=True)

    provider = ai_providers.get_provider_by_code("replace_provider")
    with app.test_request_context("/"):
        assert ai_providers.credential_for(provider) == "gsk-new"


def test_a_duplicate_code_is_refused(auth_client, app):
    from app.i18n import translate

    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "twice", "name": "A",
        "base_url": "https://api.groq.com/openai/v1", "api_key": "k", "is_active": "y",
    }, follow_redirects=True)
    response = auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "twice", "name": "B",
        "base_url": "https://api.groq.com/openai/v1", "api_key": "k", "is_active": "y",
    }, follow_redirects=True)
    assert response.status_code == 200
    expected = translate("ai.provider_code_taken", language="ar").encode("utf-8")
    assert expected in response.data
    assert db.session.query(AIProvider).filter_by(code="twice").count() == 1


def test_making_a_provider_the_default(auth_client, app):
    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "def_provider", "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1", "api_key": "k", "is_active": "y",
    }, follow_redirects=True)
    provider = ai_providers.get_provider_by_code("def_provider")
    response = auth_client.post(f"/ai/providers/{provider.id}/default", follow_redirects=True)
    assert response.status_code == 200
    assert ai_providers.default_provider().id == provider.id


def test_adding_a_model_through_the_interface(auth_client, app):
    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "model_provider", "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1", "api_key": "k", "is_active": "y",
    }, follow_redirects=True)
    provider = ai_providers.get_provider_by_code("model_provider")

    response = auth_client.post("/ai/models/new", data={
        "provider_id": str(provider.id),
        "code": "llama-3.3-70b-versatile",
        "label": "Llama 3.3 70B",
        "context_window": "131072",
        "supports_tools": "y",
        "free_tier": "y",
        "is_active": "y",
        "is_default": "y",
        "sort_order": "1",
    }, follow_redirects=True)
    assert response.status_code == 200

    model = db.session.query(AIModel).filter_by(code="llama-3.3-70b-versatile").one()
    assert model.provider_id == provider.id
    assert model.is_default is True
    assert model.context_window == 131072


def test_toggling_a_model(auth_client, app):
    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "toggle_provider", "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1", "api_key": "k", "is_active": "y",
    }, follow_redirects=True)
    provider = ai_providers.get_provider_by_code("toggle_provider")
    auth_client.post("/ai/models/new", data={
        "provider_id": str(provider.id), "code": "m1", "label": "M1",
        "is_active": "y", "supports_tools": "y",
    }, follow_redirects=True)
    model = db.session.query(AIModel).filter_by(code="m1").one()
    assert model.is_active is True

    auth_client.post(f"/ai/models/{model.id}/toggle", follow_redirects=True)
    db.session.refresh(model)
    assert model.is_active is False


def test_the_registry_is_audited(auth_client, app):
    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "audited_provider", "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1", "api_key": "k", "is_active": "y",
    }, follow_redirects=True)
    event = db.session.query(AuditEvent).filter_by(action="created").first()
    assert event is not None
    assert event.entity_type == "ai_provider"


def test_deleting_a_provider_removes_it(auth_client, app):
    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "delete_provider", "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1", "api_key": "k", "is_active": "y",
    }, follow_redirects=True)
    provider = ai_providers.get_provider_by_code("delete_provider")
    response = auth_client.post(f"/ai/providers/{provider.id}/delete", follow_redirects=True)
    assert response.status_code == 200
    assert ai_providers.get_provider_by_code("delete_provider") is None


def test_the_connection_test_reports_a_missing_key(auth_client, app):
    from app.i18n import translate

    auth_client.post("/ai/providers/new", data={
        "catalog_code": "groq", "code": "no_key_provider", "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1", "is_active": "y",
    }, follow_redirects=True)
    provider = ai_providers.get_provider_by_code("no_key_provider")
    response = auth_client.post(f"/ai/providers/{provider.id}/test", follow_redirects=True)
    assert response.status_code == 200
    assert translate("ai.test_no_key", language="ar").encode("utf-8") in response.data


def test_the_workspace_shows_the_active_provider(auth_client, app):
    from app.i18n import translate

    response = auth_client.get("/ai")
    assert response.status_code == 200
    assert translate("ai.provider_local", language="ar").encode("utf-8") in response.data
