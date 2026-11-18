from __future__ import annotations

import io
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.api.settings import PluginKeyIn, save_plugin_key
from app.custom.source_packages.routes import router
from app.data_providers.base import ProviderRequestError
from app.data_providers.custom import loader, packages
from app.data_providers.custom.managed import ManagedProvider
from app.data_providers.minute_stream import LegacyMinuteAdapter
from app.services import preferences

CODE = '''from types import SimpleNamespace
from .client import price
name = "demo_source"
def probe_api_key(key):
    return key == "valid-key", "ok" if key == "valid-key" else "invalid"
class Provider:
    name = "demo_source"
    config = SimpleNamespace(datasets={"realtime": {}})
    def get_realtime(self):
        return [{"symbol": "600000.SH", "last": price, "change_pct": 0.02}]
    def close(self):
        pass
'''


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(packages.settings, "data_dir", tmp_path)
    monkeypatch.setattr(loader, "plugins_dir", lambda: tmp_path / "builtins")
    monkeypatch.setattr(loader, "_PROVIDERS", {})
    monkeypatch.setattr(loader, "_PLUGIN_STATUS", {})
    monkeypatch.setattr(packages, "_started", set())
    monkeypatch.setattr(secrets_store, "_cache", None)
    monkeypatch.setattr(secrets_store, "_cache_sig", None)
    monkeypatch.setattr(preferences, "_cache", None)
    monkeypatch.setattr(preferences, "_cache_sig", None)
    return tmp_path


def bundle(version="1.0.0", *, code=CODE, extra=None, **changes):
    doc = dict(name="demo_source", display_name="演示数据源", version=version,
               plugin_api_version=1, entry="provider:Provider", datasets=["realtime"], runtime="none")
    doc.update(changes)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("plugin.yaml", yaml.safe_dump(doc))
        archive.writestr("provider.py", code)
        archive.writestr("client.py", f"price = {int(version.split('.')[0])}\n")
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return buf.getvalue()


def install(data):
    preview, _ = packages.inspect_archive(data)
    return packages.install(data, preview["digest"])


def cold_restart():
    loader._PROVIDERS = {}
    loader._PLUGIN_STATUS = {}
    packages._started.clear()
    packages.discover()


def test_inspection_does_not_execute_code(isolated):
    data = bundle(code="raise RuntimeError('must not execute during preview')")
    preview, _ = packages.inspect_archive(data)
    assert preview["manifest"]["name"] == "demo_source"
    assert not loader.names()
    with pytest.raises(ValueError, match="加载失败"):
        packages.install(data, preview["digest"])
    assert packages.read_state("demo_source") is None


def test_install_routes_and_reload_keeps_instance(isolated):
    install(bundle())
    provider = loader.get_provider("demo_source")
    assert provider.get_realtime()[0]["last"] == 1
    assert loader.provider_has_dataset("demo_source", "realtime")
    assert not loader.is_builtin("demo_source")
    assert loader.list_sources() == []
    loader.load_all()
    assert loader.get_provider("demo_source") is provider
    assert loader.list_plugins()[0]["origin"] == "external"


def test_plugin_link_labels_and_markdown_survive_install_and_restart(isolated):
    from app.api.settings import router as settings_router

    fields = {
        "homepage": "https://example.com/docs", "homepage_label": "使用文档",
        "key_manage_url": "https://example.com/account", "key_manage_label": "获取个人 Key",
        "description": "## 连接说明\n使用 **个人 Key**。",
        "usage_notes": ["阅读 [文档](https://example.com/docs)。"],
    }
    data = bundle(**fields)
    preview, _ = packages.inspect_archive(data)
    assert all(preview["manifest"][key] == value for key, value in fields.items())
    install(data)
    cold_restart()
    app = FastAPI()
    app.include_router(settings_router)
    with TestClient(app) as client:
        response = client.get("/api/settings/data-sources")
    assert response.status_code == 200
    plugin = next(p for p in response.json()["plugins"] if p["name"] == "demo_source")
    assert all(plugin[key] == value for key, value in fields.items())


@pytest.mark.parametrize("field", ["homepage_label", "key_manage_label"])
@pytest.mark.parametrize("value", [123, [], "x" * 201])
def test_plugin_link_labels_require_short_text(isolated, field, value):
    with pytest.raises(ValueError, match=field):
        packages.inspect_archive(bundle(**{field: value}))


def test_key_first_probe_then_save_and_only_refresh_target(isolated):
    other = SimpleNamespace(close=lambda: pytest.fail("unrelated provider closed"))
    loader._PROVIDERS["other"] = other
    install(bundle(api_key_env="DEMO_KEY"))
    handle = loader.get_provider("demo_source")
    with pytest.raises(ProviderRequestError):
        handle.get_realtime()
    assert not save_plugin_key(PluginKeyIn(plugin="demo_source", api_key="bad"))["ok"]
    assert not secrets_store.load()
    result = save_plugin_key(PluginKeyIn(plugin="demo_source", api_key="valid-key"))
    assert result["ok"] and result["plugin_available"]
    assert handle.get_realtime()[0]["last"] == 1
    assert loader.get_provider("other") is other
    assert secrets_store.load()["demo_source_api_key"] == "valid-key"


def test_upgrade_restart_rollback_and_failed_upgrade(isolated):
    install(bundle())
    install(bundle("2.0.0"))
    loader.load_all()
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 1
    assert loader.list_plugins()[0]["pending_version"] == "2.0.0"
    cold_restart()
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 2
    packages.manage("demo_source", "rollback")
    cold_restart()
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 1
    install(bundle("3.0.0", code="raise RuntimeError('broken')"))
    cold_restart()
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 1
    assert "失败" in loader.list_plugins()[0]["update_error"]


def test_same_version_collision_and_preview_digest(isolated):
    data = bundle()
    install(data)
    assert "已安装" in install(data)["message"]
    with pytest.raises(ValueError, match="相同版本"):
        install(bundle(extra={"readme.md": "different"}))
    with pytest.raises(ValueError, match="预览不一致"):
        packages.install(bundle("2.0.0"), "0" * 64)


def test_pending_replacement_preview_rejects_stale_install_state(isolated):
    install(bundle())
    next_data = bundle("3.0.0")
    preview, _ = packages.inspect_archive(next_data)
    install(bundle("2.0.0"))
    with pytest.raises(ValueError, match="状态已变化"):
        packages.install(next_data, preview["digest"], preview["revision"])
    current, _ = packages.inspect_archive(next_data)
    assert current["installed_version"] == "1.0.0"
    assert current["pending_version"] == "2.0.0"


@pytest.mark.parametrize("filename", ["../escape.py", "/escape.py", "a\\b.py", "a/../b.py", "CON.py", "client.PY", "x.dll", "a./b.py"])
def test_archive_paths_and_payload_limits(isolated, filename):
    with pytest.raises(ValueError):
        packages.inspect_archive(bundle(extra={filename: "pass"}))
    assert not (isolated.parent / "escape.py").exists()


def test_manifest_contract_and_dependency_compatibility(isolated):
    for changes in ({"plugin_api_version": 9}, {"entry": "app.other:Provider"}, {"datasets": ["invented"]}):
        with pytest.raises(ValueError):
            packages.inspect_archive(bundle(**changes))
    preview, _ = packages.inspect_archive(bundle(requires_python=">=99"))
    assert preview["errors"]
    with pytest.raises(ValueError, match="requires_python"):
        packages.install(bundle(requires_python=">=99"), preview["digest"])


def test_name_conflict_and_yaml_mutation_rejected(isolated):
    loader.plugins_dir().mkdir()
    (loader.plugins_dir() / "demo_source").mkdir()
    with pytest.raises(ValueError, match="冲突"):
        install(bundle())
    (loader.plugins_dir() / "demo_source").rmdir()
    install(bundle())
    with pytest.raises(ValueError):
        loader.save_config("demo_source", {})
    with pytest.raises(ValueError):
        loader.delete_config("demo_source")


def test_disable_uninstall_blocks_all_routing_references(isolated, monkeypatch):
    install(bundle())
    monkeypatch.setattr(preferences, "load", lambda: {"depth5_data_provider": "demo_source"})
    for action in ("disable", "remove"):
        with pytest.raises(ValueError, match="先切换"):
            packages.manage("demo_source", action)
    monkeypatch.setattr(preferences, "load", lambda: {})
    secrets_store.save({"demo_source_api_key": "keep-me"})
    packages.manage("demo_source", "disable")
    assert not loader.list_plugins()[0]["available"]
    with pytest.raises(ProviderRequestError):
        loader.get_provider("demo_source").get_realtime()
    packages.manage("demo_source", "enable")
    assert loader.list_plugins()[0]["available"]
    packages.manage("demo_source", "remove")
    cold_restart()
    assert not loader.names()
    assert not packages.directory("demo_source").exists()
    assert secrets_store.load()["demo_source_api_key"] == "keep-me"


def test_confirmed_uninstall_resets_only_its_routes_and_preserves_keys(isolated):
    from app.data_providers.capabilities import CAPABILITY_REGISTRY

    install(bundle())
    routes = {cap["field"]: "demo_source" for cap in CAPABILITY_REGISTRY if cap["field"]}
    routes["daily_data_provider"] = "other_source"
    routes["depth5_data_provider"] = "DEMO_SOURCE"
    preferences.save({**routes, "realtime_quotes_enabled": False})
    secrets_store.save({"demo_source_api_key": "keep-me"})

    result = packages.manage("demo_source", "remove", reset_routes=True)

    expected = {cap["field"]: cap["default"] for cap in CAPABILITY_REGISTRY if cap["field"]}
    expected["daily_data_provider"] = "other_source"
    assert preferences.load() == {**expected, "realtime_quotes_enabled": False}
    assert "恢复默认" in result["message"]
    assert packages.read_state("demo_source")["remove"]
    with pytest.raises(ProviderRequestError):
        loader.get_provider("demo_source").get_realtime()
    cold_restart()
    assert not loader.names()
    assert not packages.directory("demo_source").exists()
    assert secrets_store.load()["demo_source_api_key"] == "keep-me"


def test_uninstall_restores_installation_when_routing_write_fails(isolated, monkeypatch):
    install(bundle())
    preferences.save({"realtime_data_provider": "demo_source"})
    before = packages.read_state("demo_source")
    provider = loader.get_provider("demo_source")

    def fail_write(_updates):
        raise OSError("preferences are read-only")

    monkeypatch.setattr(preferences, "save", fail_write)
    with pytest.raises(OSError, match="read-only"):
        packages.manage("demo_source", "remove", reset_routes=True)
    assert packages.read_state("demo_source") == before
    assert preferences.load()["realtime_data_provider"] == "demo_source"
    assert provider.get_realtime()[0]["last"] == 1


def test_uninstall_keeps_routes_when_installation_write_fails(isolated, monkeypatch):
    install(bundle())
    preferences.save({"depth5_data_provider": "demo_source"})
    before = packages.read_state("demo_source")

    def fail_write(_name, _state):
        raise OSError("plugin state is read-only")

    monkeypatch.setattr(packages, "write_state", fail_write)
    with pytest.raises(OSError, match="read-only"):
        packages.manage("demo_source", "remove", reset_routes=True)
    assert preferences.load()["depth5_data_provider"] == "demo_source"
    assert packages.read_state("demo_source") == before
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 1


def test_uninstall_restores_routes_and_provider_when_refresh_fails(isolated, monkeypatch):
    install(bundle())
    preferences.save({"realtime_data_provider": "demo_source"})
    before = packages.read_state("demo_source")
    original_refresh = loader.refresh_plugin
    calls = 0

    def fail_once(name):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("cannot refresh plugin")
        return original_refresh(name)

    monkeypatch.setattr(loader, "refresh_plugin", fail_once)
    with pytest.raises(ValueError, match="cannot refresh"):
        packages.manage("demo_source", "remove", reset_routes=True)
    assert packages.read_state("demo_source") == before
    assert preferences.get_realtime_data_provider() == "demo_source"
    assert loader.get_provider("demo_source").get_realtime()[0]["last"] == 1


def test_management_api_accepts_confirmed_route_reset_and_refreshes_capabilities(isolated, monkeypatch):
    from app.tickflow import policy

    monkeypatch.setattr("app.services.auth.is_configured", lambda: False)
    sentinel = object()
    monkeypatch.setattr(policy, "detect_capabilities", lambda: sentinel)
    install(bundle())
    preferences.save({"realtime_data_provider": "demo_source"})
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        refused = client.post("/api/custom/source-packages/demo_source/manage", json={"action": "remove"})
        assert refused.status_code == 400
        result = client.post("/api/custom/source-packages/demo_source/manage", json={"action": "remove", "reset_routes": True})
        assert result.status_code == 200
        assert preferences.get_realtime_data_provider() == "tickflow"
        assert app.state.capabilities is sentinel


def test_cannot_route_back_to_a_plugin_pending_uninstall(isolated):
    from fastapi import HTTPException

    from app.api.settings import DataProvidersIn, update_data_providers

    install(bundle())
    packages.manage("demo_source", "remove")
    with pytest.raises(HTTPException, match="等待卸载"):
        update_data_providers(DataProvidersIn(realtime_data_provider="demo_source"), SimpleNamespace())
    assert "realtime_data_provider" not in preferences.load()


def test_uninstall_serializes_with_a_concurrent_routing_request(isolated, monkeypatch):
    from fastapi import HTTPException

    from app.api.settings import DataProvidersIn, update_data_providers

    install(bundle())
    preferences.save({"realtime_data_provider": "demo_source"})
    saving, finish = threading.Event(), threading.Event()
    original_save = preferences.save

    def delayed_save(updates):
        saving.set()
        assert finish.wait(3)
        return original_save(updates)

    monkeypatch.setattr(preferences, "save", delayed_save)
    with ThreadPoolExecutor(max_workers=2) as executor:
        removal = executor.submit(packages.manage, "demo_source", "remove", reset_routes=True)
        assert saving.wait(3)
        routing = executor.submit(
            update_data_providers,
            DataProvidersIn(realtime_data_provider="demo_source"),
            SimpleNamespace(),
        )
        finish.set()
        removal.result(timeout=3)
        with pytest.raises(HTTPException, match="等待卸载"):
            routing.result(timeout=3)
    assert preferences.get_realtime_data_provider() == "tickflow"


def test_handle_delays_close_until_stream_consumed():
    closed = []
    class Source:
        def iter_daily(self):
            yield 1
            yield 2
        def close(self):
            closed.append(self)
    old, new = Source(), Source()
    handle = ManagedProvider(old)
    stream = handle.iter_daily()
    handle.replace(new)
    assert not closed
    assert list(stream) == [1, 2]
    assert closed == [old]
    handle.close()
    assert closed == [old, new]


def test_handle_delays_close_during_concurrent_request():
    started, finish = threading.Event(), threading.Event()
    closed = []
    class Source:
        def get_realtime(self):
            started.set()
            assert finish.wait(3)
            return [1]
        def close(self):
            closed.append(self)
    old, new = Source(), Source()
    handle = ManagedProvider(old)
    worker = threading.Thread(target=handle.get_realtime)
    worker.start()
    assert started.wait(3)
    handle.replace(new)
    assert not closed
    finish.set()
    worker.join(3)
    assert closed == [old]


def test_handle_rebuild_client_keeps_handle_usable():
    """rebuild_client() 转发到内层, 不永久停用 handle (行情自愈重建路径)。"""
    rebuilt = []
    class Source:
        def get_realtime(self):
            return [1]
        def rebuild_client(self):
            rebuilt.append(self)
        def close(self):
            raise AssertionError("rebuild 不应调用内层 close()")
    source = Source()
    handle = ManagedProvider(source)
    handle.rebuild_client()
    assert rebuilt == [source]
    assert handle.get_realtime() == [1]


def test_handle_rebuild_client_falls_back_to_close():
    """内层没有 rebuild_client() 时退回 close() (FuyaoProvider 懒重建路径)。"""
    closed = []
    class Source:
        def get_realtime(self):
            return [1]
        def close(self):
            closed.append(self)
    source = Source()
    handle = ManagedProvider(source)
    handle.rebuild_client()
    assert closed == [source]
    assert handle.get_realtime() == [1]  # handle 仍可用


def test_handle_rebuild_client_raises_after_close():
    """handle 已被 close() 停用后, rebuild_client() 抛错而不是静默成功。"""
    class Source:
        def get_realtime(self):
            return [1]
        def close(self):
            pass
    handle = ManagedProvider(Source())
    handle.close()
    with pytest.raises(ProviderRequestError, match="数据源已停止"):
        handle.rebuild_client()


# ================================================================
# rebuild_client 并发回归
#
# rebuild 必须: ①计入在途引用, 不被 replace 提前关闭; ②异常时释放引用;
# ③不永久停用 handle; ④与 replace 并发时, 重建最终落在当前生效的源上
# (而不是只修了被换下的旧源); ⑤全程不持有 handle 锁去做外部关闭。
# 改坏任一条, 都会退回「自愈把源打死」「引用泄漏」或「自愈静默失效」。
# ================================================================


def test_rebuild_client_delays_close_until_rebuild_returns():
    """rebuild 进行中 replace: 旧源不能在 rebuild 返回前被关闭 (否则重建的是死连接)。"""
    started, finish = threading.Event(), threading.Event()
    closed, rebuilt = [], []

    class Source:
        def get_realtime(self):
            return [1]

        def rebuild_client(self):
            started.set()
            assert finish.wait(3)  # 卡住, 等主线程 replace
            rebuilt.append(self)

        def close(self):
            closed.append(self)

    old, new = Source(), Source()
    handle = ManagedProvider(old)
    worker = threading.Thread(target=handle.rebuild_client)
    worker.start()
    assert started.wait(3)

    handle.replace(new)
    assert not closed, "rebuild 在途时旧源不应被关闭"

    finish.set()
    worker.join(3)
    assert rebuilt == [old, new], "旧源先修, 发现已被换下后对新源补做一次"
    assert closed == [old], "rebuild 结束后旧源才关闭"
    assert handle.get_realtime() == [1], "handle 仍可用, 且指向新源"


def test_rebuild_client_releases_refcount_when_inner_close_raises():
    """内层 close() 抛异常时仍要释放在途引用, 否则该 entry 永远关不掉 (泄漏)。"""
    class Source:
        def get_realtime(self):
            return [1]

        def close(self):
            raise RuntimeError("close 炸了")

    handle = ManagedProvider(Source())
    with pytest.raises(RuntimeError, match="close 炸了"):
        handle.rebuild_client()

    assert handle._current[1] == 0, "在途引用计数必须归零"
    assert handle._current[2] is False, "handle 不应被停用"
    assert handle.get_realtime() == [1], "handle 仍可正常取数"


def test_rebuild_client_releases_refcount_when_inner_rebuild_raises():
    """内层 rebuild_client() 抛异常时同样要释放引用。"""
    class Source:
        def get_realtime(self):
            return [1]

        def rebuild_client(self):
            raise RuntimeError("rebuild 炸了")

        def close(self):
            raise AssertionError("有 rebuild_client 就不该退回 close()")

    handle = ManagedProvider(Source())
    with pytest.raises(RuntimeError, match="rebuild 炸了"):
        handle.rebuild_client()

    assert handle._current[1] == 0
    assert handle.get_realtime() == [1]


def test_rebuild_client_does_not_kill_handle_under_concurrency():
    """并发 rebuild + 取数: 无死锁, 且结束后 handle 仍能取数 (线上事故回归)。"""
    class Source:
        def get_realtime(self):
            return [1]

        def rebuild_client(self):
            pass

        def close(self):
            raise AssertionError("rebuild 不应永久停用源")

    handle = ManagedProvider(Source())
    errors: list[BaseException] = []

    def rebuild_many():
        try:
            for _ in range(50):
                handle.rebuild_client()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def fetch_many():
        try:
            for _ in range(50):
                assert handle.get_realtime() == [1]
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=rebuild_many), threading.Thread(target=fetch_many)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
        assert not t.is_alive(), "并发下出现死锁"

    assert errors == []
    assert handle.get_realtime() == [1]


def test_rebuild_client_rebuilds_inner_not_wrapper():
    """rebuild 必须作用在内层 provider 上, 而不是触发 handle 自己的 close()。

    这是线上事故的直接形态: 用 getattr(provider, "close") 取重建入口时,
    handle 的类方法会遮蔽 __getattr__, 于是重建 = 永久停用。
    """
    inner_closed, inner_rebuilt = [], []

    class Source:
        def get_realtime(self):
            return [1]

        def rebuild_client(self):
            inner_rebuilt.append(self)

        def close(self):
            inner_closed.append(self)

    source = Source()
    handle = ManagedProvider(source)
    handle.rebuild_client()

    assert inner_rebuilt == [source]
    assert inner_closed == [], "不应走到 close()"
    assert handle._current[2] is False, "handle 绝不能被标记为停止"


def test_rebuild_client_works_through_minute_stream_wrapper():
    """被 with_minute_stream 包一层后, rebuild_client 仍能透传到内层。"""
    rebuilt, closed = [], []

    class LegacySource:
        """只有 get_minute → 会被 LegacyMinuteAdapter 包装。"""
        def get_minute(self, *a, **kw):
            return None

        def get_realtime(self):
            return [1]

        def rebuild_client(self):
            rebuilt.append(self)

        def close(self):
            closed.append(self)

    source = LegacySource()
    handle = ManagedProvider(source)
    assert isinstance(handle._current[0], LegacyMinuteAdapter), "前提: 已被包装"

    handle.rebuild_client()

    assert rebuilt == [source], "经 __getattr__ 透传到内层"
    assert closed == []
    assert handle.get_realtime() == [1]


def test_rebuild_client_does_not_hold_lock_while_rebuilding():
    """rebuild 期间不能持锁: 否则一次慢速 HTTP 关闭会把整个源的取数全堵住。

    判据不是「取数会不会慢」而是「取数会不会被卡住」: 若 rebuild 持锁, 主线程的
    get_realtime() 会在锁上无限等待, 这里用短超时把它变成可失败的断言。
    """
    started, finish = threading.Event(), threading.Event()
    fetch_done = threading.Event()
    fetch_result = []

    class Source:
        def get_realtime(self):
            return [1]

        def rebuild_client(self):
            started.set()
            assert finish.wait(5)  # 卡住, 模拟慢速关闭 HTTP 池

        def close(self):
            pass

    handle = ManagedProvider(Source())
    worker = threading.Thread(target=handle.rebuild_client)
    worker.start()
    assert started.wait(3)

    def fetch():
        fetch_result.append(handle.get_realtime())
        fetch_done.set()

    fetcher = threading.Thread(target=fetch, daemon=True)
    fetcher.start()
    try:
        assert fetch_done.wait(1.0), "rebuild 期间取数被 handle 锁挡住了"
        assert fetch_result == [[1]]
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive()


def test_rebuild_client_gives_up_after_two_rounds():
    """连续被 replace 换下时最多追两轮, 不无限重试 (下一轮健康探测会再触发)。"""
    rebuilt = []

    class Source:
        def get_realtime(self):
            return [1]

        def rebuild_client(self):
            rebuilt.append(self)
            # 每轮重建都触发一次替换, 让 entry 永远不是当前源
            handle.replace(Source())

        def close(self):
            pass

    first = Source()
    handle = ManagedProvider(first)
    handle.rebuild_client()

    assert len(rebuilt) == 2, "最多追两轮, 不能无限重试"
    assert rebuilt[0] is first, "第一轮修被换下的旧源"
    assert handle.get_realtime() == [1], "handle 仍可用"



def test_quote_service_rebuild_keeps_wrapped_source_alive(isolated, monkeypatch):
    """行情自愈重建走 handle 后, 源仍能继续取数 (线上「数据源已停止」事故回归)。"""
    from app.services.quote_service import QuoteService

    rebuilt = []

    class Source:
        def get_realtime(self):
            return [1]

        def rebuild_client(self):
            rebuilt.append(self)

        def close(self):
            raise AssertionError("自愈重建不应永久停用源")

    source = Source()
    loader._PROVIDERS["fuyao"] = ManagedProvider(source)
    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "fuyao")

    QuoteService().rebuild_realtime_client()

    assert rebuilt == [source]
    assert loader._PROVIDERS["fuyao"].get_realtime() == [1]


def test_management_auth_and_upload_api(isolated, monkeypatch):
    from app.services import auth
    monkeypatch.setattr(auth, "is_configured", lambda: False)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/custom/source-packages/inspect", files={"file": ("source.zip", bundle())}, headers={"Origin": "https://evil.example"}).status_code == 403
        preview = client.post("/api/custom/source-packages/inspect", files={"file": ("source.zip", bundle())})
        assert preview.status_code == 200
        result = client.post("/api/custom/source-packages/install", files={"file": ("source.zip", bundle())}, data={"digest": preview.json()["digest"]})
        assert result.status_code == 200
        assert client.post("/api/custom/source-packages/demo_source/manage", json={"action": "disable"}).status_code == 200
    with TestClient(app, base_url="http://localhost", client=("192.168.1.3", 50000)) as client:
        assert client.post("/api/custom/source-packages/inspect", files={"file": ("s.zip", bundle())}).status_code == 403


def test_valid_key_with_failed_initialization_restores_original(isolated):
    code = CODE.replace('    def get_realtime(self):', '''    def __init__(self):
        from app import secrets_store
        if secrets_store.load().get("demo_source_api_key") == "valid-key":
            raise RuntimeError("constructor failed")
    def get_realtime(self):''')
    install(bundle(code=code, api_key_env="DEMO_KEY"))
    secrets_store.save({"demo_source_api_key": "old-key"})
    loader.refresh_plugin("demo_source")
    original = loader.get_provider("demo_source")
    assert original.get_realtime()
    result = save_plugin_key(PluginKeyIn(plugin="demo_source", api_key="valid-key"))
    assert not result["ok"]
    assert secrets_store.load()["demo_source_api_key"] == "old-key"
    assert original.get_realtime()
    assert loader.list_plugins()[0]["available"]


def test_disabled_plugin_does_not_execute_pending_version_on_start(isolated):
    install(bundle())
    packages.manage("demo_source", "disable")
    install(bundle("2.0.0", code="raise RuntimeError('must not run')"))
    cold_restart()
    assert loader.list_plugins()[0]["pending_version"] == "2.0.0"
    assert loader.list_plugins()[0]["version"] == "1.0.0"
    assert not loader.list_plugins()[0]["available"]


def test_bad_package_does_not_hide_healthy_provider(isolated):
    install(bundle())
    path = packages.root() / "broken"
    path.mkdir()
    (path / "state.json").write_text('{"schema": 999}')
    packages.discover()
    assert loader.get_provider("demo_source").get_realtime()
    assert not next(p for p in loader.list_plugins() if p["name"] == "broken")["available"]
    assert loader.provider_has_dataset("broken", "daily")
    with pytest.raises(ProviderRequestError):
        loader.get_provider("broken").get_daily([])


def test_zip_symlink_is_rejected(isolated):
    data = io.BytesIO(bundle())
    with zipfile.ZipFile(data, "a") as archive:
        info = zipfile.ZipInfo("link.py")
        info.create_system = 3
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "../outside")
    with pytest.raises(ValueError, match="符号链接"):
        packages.inspect_archive(data.getvalue())


def test_external_capability_matrix_marks_unconfigured_route_unusable(isolated):
    from app.data_providers.capabilities import build_capability_matrix
    install(bundle(api_key_env="DEMO_KEY"))
    matrix = build_capability_matrix({"realtime_data_provider": "demo_source"})
    realtime = next(row for row in matrix["capabilities"] if row["id"] == "realtime")
    assert realtime["current"] == "demo_source"
    assert not realtime["usable"]
    assert realtime["pending"][0]["name"] == "demo_source"


def test_reload_picks_up_manually_changed_credentials(isolated):
    install(bundle(api_key_env="DEMO_KEY"))
    handle = loader.get_provider("demo_source")
    assert not loader.list_plugins()[0]["available"]
    secrets_store.save({"demo_source_api_key": "valid-key"})
    loader.load_all()
    assert loader.get_provider("demo_source") is handle
    assert loader.list_plugins()[0]["available"]
    secrets_store.clear("demo_source_api_key")
    loader.load_all()
    assert not loader.list_plugins()[0]["available"]
    with pytest.raises(ProviderRequestError):
        handle.get_realtime()
