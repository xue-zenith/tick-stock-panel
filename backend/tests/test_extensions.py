from __future__ import annotations

import types
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.extensions.contracts import (
    BACKEND_EXTENSION_API_VERSION,
    ExtensionContext,
    NotificationFormatContext,
    NotificationFormatter,
)
from app.extensions.loader import configure_backend_extensions, stop_backend_extensions
from app.extensions.registry import BackendExtensionRegistrar, BackendExtensionRegistry
from app.services.quote_service import QuoteService


class PrefixFormatter(NotificationFormatter):
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix

    def format_message(self, event: dict, context: NotificationFormatContext) -> str:
        assert context.api_version == BACKEND_EXTENSION_API_VERSION
        return f"{self.prefix}{event['message']}"


class BrokenFormatter(NotificationFormatter):
    def format_message(self, event: dict, context: NotificationFormatContext) -> str:
        del event, context
        raise RuntimeError("broken formatter")


def _registrar(extension_id: str = "company.test") -> BackendExtensionRegistrar:
    return BackendExtensionRegistrar(
        extension_id,
        api_version=BACKEND_EXTENSION_API_VERSION,
    )


def test_empty_registry_preserves_existing_notification_objects() -> None:
    registry = BackendExtensionRegistry()
    registry.freeze()
    service = QuoteService()
    service._app_state = types.SimpleNamespace(extension_registry=registry)
    events = [{"message": "原始消息", "source": "strategy"}]

    result = service._format_extension_notifications(events)

    assert result is events
    assert result[0] is events[0]


def test_notification_formatters_are_ordered_and_do_not_mutate_input() -> None:
    registry = BackendExtensionRegistry()
    registrar = _registrar()
    registrar.register_notification_formatter("company.second", PrefixFormatter("B"), order=20)
    registrar.register_notification_formatter("company.first", PrefixFormatter("A"), order=10)
    registry.register(registrar)
    registry.freeze()
    service = QuoteService()
    service._app_state = types.SimpleNamespace(extension_registry=registry)
    events = [{"message": "原始消息", "source": "strategy"}]

    result = service._format_extension_notifications(events)

    assert result == [{"message": "BA原始消息", "source": "strategy"}]
    assert events == [{"message": "原始消息", "source": "strategy"}]
    assert result is not events
    assert result[0] is not events[0]


def test_broken_formatter_keeps_previous_message_and_later_formatters_run() -> None:
    registry = BackendExtensionRegistry()
    registrar = _registrar()
    registrar.register_notification_formatter("company.first", PrefixFormatter("A"), order=10)
    registrar.register_notification_formatter("company.broken", BrokenFormatter(), order=20)
    registrar.register_notification_formatter("company.last", PrefixFormatter("B"), order=30)
    registry.register(registrar)
    registry.freeze()
    service = QuoteService()
    service._app_state = types.SimpleNamespace(extension_registry=registry)

    result = service._format_extension_notifications([{"message": "原始消息"}])

    assert result[0]["message"] == "BA原始消息"


def test_registry_rejects_version_mismatch_without_partial_registration() -> None:
    registry = BackendExtensionRegistry()
    registrar = BackendExtensionRegistrar("company.future", api_version=999)
    registrar.register_notification_formatter("company.future", PrefixFormatter("x"))

    with pytest.raises(ValueError, match="requires backend API"):
        registry.register(registrar)

    registry.freeze()
    assert not registry.has_customizations
    assert not registry.has_notification_formatters


def test_registry_is_frozen_after_startup() -> None:
    registry = BackendExtensionRegistry()
    registry.freeze()

    with pytest.raises(RuntimeError, match="frozen"):
        registry.register(_registrar())


def test_loader_isolates_failed_setup_and_registers_valid_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = types.ModuleType("app.custom.broken")
    broken.EXTENSION_ID = "company.broken"
    broken.EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

    def broken_setup(registrar: BackendExtensionRegistrar) -> None:
        registrar.register_notification_formatter("company.partial", PrefixFormatter("x"))
        raise RuntimeError("setup failed")

    broken.setup = broken_setup

    valid = types.ModuleType("app.custom.valid")
    valid.EXTENSION_ID = "company.valid"
    valid.EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

    def valid_setup(registrar: BackendExtensionRegistrar) -> None:
        router = APIRouter(prefix="/api/custom/valid")

        @router.get("/status")
        def status() -> dict:
            return {"status": "ok"}

        registrar.include_router(router)

    valid.setup = valid_setup
    modules = {broken.__name__: broken, valid.__name__: valid}

    monkeypatch.setattr(
        "app.extensions.loader._custom_module_names",
        lambda: [broken.__name__, valid.__name__],
    )
    monkeypatch.setattr(
        "app.extensions.loader.importlib.import_module",
        lambda name: modules[name],
    )
    app = FastAPI()

    registry, errors = configure_backend_extensions(app)

    assert registry.frozen
    assert registry.extension_ids() == frozenset({"company.valid"})
    assert len(errors) == 1
    assert errors[0].module == broken.__name__
    client = TestClient(app)
    response = client.get("/api/custom/valid/status")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_shutdown_hooks_run_in_reverse_and_isolate_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    first = types.ModuleType("app.custom.first")
    first.EXTENSION_ID = "company.first"

    def first_shutdown(context: ExtensionContext) -> None:
        assert context.data_dir == tmp_path
        calls.append("first")

    first.shutdown = first_shutdown

    broken = types.ModuleType("app.custom.broken_shutdown")
    broken.EXTENSION_ID = "company.broken_shutdown"

    async def broken_shutdown(context: ExtensionContext) -> None:
        del context
        calls.append("broken")
        raise RuntimeError("shutdown failed")

    broken.shutdown = broken_shutdown
    modules = {first.__name__: first, broken.__name__: broken}
    monkeypatch.setattr(
        "app.extensions.loader._custom_module_names",
        lambda: [first.__name__, broken.__name__],
    )
    monkeypatch.setattr(
        "app.extensions.loader.importlib.import_module",
        lambda name: modules[name],
    )

    registry = BackendExtensionRegistry()
    registry.register(_registrar(first.EXTENSION_ID))
    registry.register(_registrar(broken.EXTENSION_ID))
    registry.freeze()
    context = ExtensionContext(
        api_version=BACKEND_EXTENSION_API_VERSION,
        data_dir=tmp_path,
        repository=types.SimpleNamespace(),
    )

    await stop_backend_extensions(context, registry)

    assert calls == ["broken", "first"]
