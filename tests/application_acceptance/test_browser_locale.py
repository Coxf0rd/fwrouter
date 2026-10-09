from __future__ import annotations

import http.server
import json
import threading
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

import pytest
from .http_support import http_json

class _UiAndApiBridge(http.server.SimpleHTTPRequestHandler):
    api_base = ""
    ui_root = Path("/workspace/ui")

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def translate_path(self, path: str) -> str:
        requested = Path(path.split("?", 1)[0].lstrip("/"))
        candidate = (self.ui_root / requested).resolve()
        if not candidate.is_relative_to(self.ui_root.resolve()):
            return str(self.ui_root / "index.html")
        return str(candidate)

    def do_GET(self):
        if self.path.startswith("/api/v2/"):
            self._proxy()
        else:
            super().do_GET()

    def do_POST(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def _proxy(self):
        if not self.path.startswith("/api/v2/"):
            self.send_error(404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if content_length < 0 or content_length > 1024 * 1024:
            self.send_error(413)
            return
        body = self.rfile.read(content_length) if self.command in {"POST", "PUT", "PATCH", "DELETE"} else None
        request = urllib.request.Request(self.api_base + self.path, data=body, method=self.command,
                                         headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                content = response.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    self.send_error(502)
                    return
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
        except urllib.error.HTTPError as exc:
            content = exc.read(1024 * 1024 + 1)
            if len(content) > 1024 * 1024:
                self.send_error(502)
                return
            self.send_response(exc.code)
            self.send_header("Content-Type", exc.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    def log_message(self, fmt, *args):
        return


@pytest.mark.browser
def test_real_chromium_navigation_locale_and_settings_write(acceptance_stack):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        pytest.fail(f"profile-pinned Playwright Python package is unavailable: {exc}")

    profile = acceptance_stack["profile"]
    api_origin = acceptance_stack["api"].removesuffix("/api/v2")
    parsed_api = urllib.parse.urlparse(api_origin)
    if parsed_api.scheme != "http" or parsed_api.hostname != "127.0.0.1" or not parsed_api.port or parsed_api.path:
        pytest.fail("browser bridge upstream must be a fixed loopback origin")
    server_type = type("BoundUiApiBridge", (_UiAndApiBridge,), {
        "api_base": api_origin,
        "ui_root": Path("/workspace/ui"),
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server_type)
    server.daemon_threads = False
    server.block_on_close = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path=profile["chromium"]["path"],
                args=["--disable-dev-shm-usage", "--no-proxy-server"],
            )
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            browser_origin = f"http://127.0.0.1:{server.server_port}"
            page.route("**/*", lambda route: route.continue_() if urllib.parse.urlparse(route.request.url).netloc == f"127.0.0.1:{server.server_port}" else route.abort())
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(browser_origin + "/", wait_until="domcontentloaded", timeout=15000)
            critical = page.evaluate("""async () => {
              const response = await fetch('/api/v2/ui/settings/workspace');
              return {status: response.status, body: await response.json()};
            }""")
            assert critical["status"] == 200 and critical["body"].get("ok") is True, critical
            error_code, error_body = http_json(acceptance_stack["api"] + "/__acceptance_missing_route__")
            assert error_code == 404 and isinstance(error_body, dict), {"status": error_code, "body": error_body}
            toggle = page.locator("#localeToggle")
            assert toggle.count() == 1
            current_locale = "ru"
            for width, height in ((1440, 900), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                for locale in ("ru", "en"):
                    if current_locale != locale:
                        toggle.click()
                        page.wait_for_function(f"document.documentElement.lang === '{locale}'", timeout=5000)
                        current_locale = locale
                    assert page.locator("html").get_attribute("lang") == locale
                    assert page.locator("[data-i18n='html.view.admin']").inner_text().strip() == ("Admin" if locale == "en" else "Админ")
                    for view in ("user", "admin", "settings"):
                        page.locator(f".seg__btn[data-view='{view}']").click()
                        assert page.locator("html").get_attribute("data-view") == view
                        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                        assert overflow is False, {"viewport": width, "locale": locale, "view": view}
            if current_locale != "en":
                toggle.click()
                page.wait_for_function("document.documentElement.lang === 'en'", timeout=5000)

            page.locator(".seg__btn[data-view='settings']").click()
            toggle_system = page.locator("[data-settings-system-toggle]").first
            toggle_system.wait_for(state="visible", timeout=15000)
            system_id = str(toggle_system.get_attribute("data-settings-system-toggle") or "").strip()
            assert system_id
            before = page.evaluate("""async () => {
              const response = await fetch('/api/v2/ui/settings/display');
              return {status: response.status, body: await response.json()};
            }""")
            assert before["status"] == 200 and before["body"].get("ok") is True, before
            before_visibility = before["body"]["data"]["display_settings"]["system_visibility"][system_id]
            with page.expect_response(lambda response: response.url.endswith("/api/v2/ui/settings/display") and response.request.method == "PUT" and response.status == 200, timeout=10000):
                toggle_system.click()
            after = page.evaluate("""async () => {
              const response = await fetch('/api/v2/ui/settings/display');
              return {status: response.status, body: await response.json()};
            }""")
            assert after["status"] == 200 and after["body"].get("ok") is True, after
            after_visibility = after["body"]["data"]["display_settings"]["system_visibility"][system_id]
            assert after_visibility is (not before_visibility), {"before": before_visibility, "after": after_visibility}
            from fwrouter_api.services.ui_display_settings_store import _load_display_settings_raw
            stored = _load_display_settings_raw()
            assert stored.get("system_visibility", {}).get(system_id) is after_visibility

            assert not page_errors, page_errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.browser
def test_real_chromium_provider_exclusive_control_persists_and_excludes_auto_candidate(acceptance_stack):
    from .test_core_provider_mihomo import _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source
    from .joined_support import await_core_job

    stack = acceptance_stack
    api, bridge = stack["api"], stack["provider_bridge"]
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    code, enable = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                             payload={"action": "enable"})
    assert code == 200 and enable.get("ok") is True, enable
    assert await_core_job(api, enable).get("status") == "success"
    from fwrouter_api.services.provider_managed import binding_for
    logical_id = binding_for(source_ref)["logical_server_id"]

    # A separate ordinary server keeps configured Auto membership outside the
    # exclusive source so the UI can prove configured and effective sets differ.
    code, ordinary = http_json(
        f"{api}/servers/custom/proxy", method="POST",
        payload={"server_name": "Acceptance ordinary proxy", "proxy_type": "socks5",
                 "host": "192.0.2.55", "port": 1080, "vpn_auto": False, "global_list": False},
    )
    assert code == 200 and ordinary.get("ok") is True, ordinary
    ordinary_server = ordinary.get("data", {}).get("custom_server", {}).get("server", {})
    ordinary_id = str(ordinary_server.get("server_id") or "")
    assert ordinary_id
    code, configured = http_json(
        f"{api}/servers/{ordinary_id}/preferences", method="PATCH",
        payload={"vpn_auto": True, "vpn_auto_priority": 1, "global_list": False,
                 "reconcile_mihomo": False, "requested_by": "acceptance-setup"},
    )
    assert code == 200 and configured.get("ok") is True, configured

    api_origin = api.removesuffix("/api/v2")
    server_type = type("BoundUiApiBridge", (_UiAndApiBridge,), {
        "api_base": api_origin,
        "ui_root": Path("/workspace/ui"),
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server_type)
    server.daemon_threads = False
    server.block_on_close = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    browser = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=stack["profile"]["chromium"]["path"],
                                                  args=["--disable-dev-shm-usage", "--no-proxy-server"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            browser_origin = f"http://127.0.0.1:{server.server_port}"
            page.route("**/*", lambda route: route.continue_() if urllib.parse.urlparse(route.request.url).netloc == f"127.0.0.1:{server.server_port}" else route.abort())
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(browser_origin + "/", wait_until="domcontentloaded", timeout=15000)
            page.locator(".seg__btn[data-view='settings']").click()
            page.locator("#settingsSourceTabs [data-log-source='controls']").click()
            toggle = page.locator("[data-vpn-auto-exclusive]")
            toggle.wait_for(state="visible", timeout=15000)
            assert toggle.is_checked() is False
            with page.expect_response(lambda response: response.url.endswith(f"/subscription/sources/{source_ref}/vpn-auto-exclusive")
                                      and response.request.method == "POST", timeout=12000) as action:
                toggle.check()
            assert action.value.status == 200
            page.wait_for_function("document.querySelector('[data-vpn-auto-exclusive]')?.checked === true", timeout=10000)

            code, subscription = http_json(f"{api}/subscription")
            assert code == 200 and subscription.get("ok") is True, subscription
            assert subscription["data"]["subscription"]["vpn_auto_exclusive"]["source_ref"] == source_ref
            code, servers = http_json(f"{api}/servers?inventory_state=active&limit=1000&include_provider_legacy=true")
            assert code == 200 and servers.get("ok") is True, servers
            server_rows = servers.get("data", {}).get("servers", [])
            provider_row = next((item for item in server_rows if item.get("server_id") == logical_id), None)
            ordinary_row = next((item for item in server_rows if item.get("server_id") == ordinary_id), None)
            assert provider_row is not None and ordinary_row is not None, {
                "logical_id": logical_id, "ordinary_id": ordinary_id,
                "server_ids": [item.get("server_id") for item in server_rows],
            }
            assert provider_row.get("vpn_auto_excluded") is False, provider_row
            assert provider_row.get("preferences", {}).get("vpn_auto") is True, provider_row
            assert provider_row.get("auto_eligible") is True, provider_row
            assert ordinary_row.get("vpn_auto_excluded") is True, ordinary_row
            assert ordinary_row.get("preferences", {}).get("vpn_auto") is True, ordinary_row
            assert ordinary_row.get("auto_eligible") is False, ordinary_row
            effective_before = sorted(item["server_id"] for item in server_rows if item.get("auto_eligible") is True)
            with urllib.request.urlopen("http://127.0.0.1:5200/proxies/vpn-auto", timeout=3) as response:
                runtime_before = json.loads(response.read(256 * 1024))
            runtime_targets_before = sorted(str(item) for item in runtime_before.get("all", []))

            page.locator(".seg__btn[data-view='admin']").click()
            provider_candidate = page.locator(f'[data-auto-candidate="{logical_id}"]')
            ordinary_candidate = page.locator(f'[data-auto-candidate="{ordinary_id}"]')
            ordinary_candidate.wait_for(state="visible", timeout=15000)
            assert provider_candidate.is_checked(), logical_id
            assert ordinary_candidate.is_checked() and ordinary_candidate.is_enabled(), ordinary_id
            with page.expect_response(
                lambda response: response.url.endswith(f"/servers/{ordinary_id}/preferences")
                and response.request.method == "PATCH" and response.status == 200,
                timeout=12000,
            ) as save:
                ordinary_candidate.uncheck()
            assert save.value.status == 200
            code, after_servers = http_json(f"{api}/servers?inventory_state=active&limit=1000&include_provider_legacy=true")
            assert code == 200 and after_servers.get("ok") is True, after_servers
            after_rows = after_servers.get("data", {}).get("servers", [])
            provider_after = next(item for item in after_rows if item.get("server_id") == logical_id)
            ordinary_after = next(item for item in after_rows if item.get("server_id") == ordinary_id)
            assert provider_after.get("auto_eligible") is True, provider_after
            assert ordinary_after.get("preferences", {}).get("vpn_auto") is False, ordinary_after
            assert ordinary_after.get("vpn_auto_excluded") is True and ordinary_after.get("auto_eligible") is False, ordinary_after
            effective_after = sorted(item["server_id"] for item in after_rows if item.get("auto_eligible") is True)
            assert effective_after == effective_before, {"before": effective_before, "after": effective_after}
            with urllib.request.urlopen("http://127.0.0.1:5200/proxies/vpn-auto", timeout=3) as response:
                runtime_after = json.loads(response.read(256 * 1024))
            assert sorted(str(item) for item in runtime_after.get("all", [])) == runtime_targets_before
            from fwrouter_api.db.connection import db_session
            with db_session() as connection:
                persisted = connection.execute(
                    "SELECT vpn_auto FROM server_preferences WHERE server_id=?", (ordinary_id,)
                ).fetchone()
            assert persisted is not None and int(persisted["vpn_auto"]) == 0
            assert not errors, errors
            browser.close()
    finally:
        bridge.release_response()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.browser
def test_real_chromium_provider_controls_report_real_api_failure_without_success(acceptance_stack):
    from .test_core_provider_mihomo import _configure_provider, _seed_source

    stack = acceptance_stack
    source_ref = _seed_source(stack)
    _configure_provider(stack["api"], source_ref)
    stack["provider_bridge"].set_mode("slow_success")
    profile = stack["profile"]
    api_origin = stack["api"].removesuffix("/api/v2")
    server_type = type("BoundUiApiBridge", (_UiAndApiBridge,), {
        "api_base": api_origin,
        "ui_root": Path("/workspace/ui"),
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server_type)
    server.daemon_threads = False
    server.block_on_close = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=profile["chromium"]["path"],
                                                  args=["--disable-dev-shm-usage", "--no-proxy-server"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            browser_origin = f"http://127.0.0.1:{server.server_port}"
            page.route("**/*", lambda route: route.continue_() if urllib.parse.urlparse(route.request.url).netloc == f"127.0.0.1:{server.server_port}" else route.abort())
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(browser_origin + "/", wait_until="domcontentloaded", timeout=15000)
            page.locator(".seg__btn[data-view='settings']").click()
            page.locator("#settingsSourceTabs [data-log-source='controls']").click()
            root = page.locator("#providerManagedControls")
            root.wait_for(state="visible", timeout=15000)
            assert root.get_attribute("data-source-ref") == source_ref
            discover = root.locator('[data-provider-action="discover"]')
            with page.expect_response(lambda response: response.url.endswith("/provider/configs") and response.request.method == "POST", timeout=12000) as success_info:
                discover.click()
                assert stack["provider_bridge"].request_entered.wait(timeout=5), "provider call did not reach deterministic HTTP boundary"
                page.wait_for_function("document.querySelector('[data-provider-action=discover]')?.disabled === true", timeout=3000)
                assert root.get_attribute("data-action-state") == "RUNNING"
                stack["provider_bridge"].release_response()
            assert success_info.value.status == 200
            page.wait_for_function("document.querySelector('#providerManagedState')?.dataset.actionState === 'SUCCESS'", timeout=10000)

            stack["provider_bridge"].set_mode("429")
            discover = root.locator('[data-provider-action="discover"]')
            with page.expect_response(lambda response: response.url.endswith("/provider/configs") and response.request.method == "POST", timeout=12000) as response_info:
                discover.click()
            response = response_info.value
            assert response.status == 200
            page.wait_for_function("document.querySelector('#providerManagedState')?.classList.contains('is-error-scope')", timeout=5000)
            state = page.locator("#providerManagedState")
            assert state.inner_text().strip()
            assert state.get_attribute("data-action-state") == "FAILED"
            assert root.locator("[data-provider-action='discover']").is_visible()
            assert not errors, errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.browser
def test_real_chromium_xray_client_editor_uses_api_jobs_and_native_readback(acceptance_stack):
    stack = acceptance_stack
    api, profile = stack["api"], stack["profile"]
    code, accepted = http_json(
        f"{api}/xray/clients", method="POST",
        payload={"alias": "browser-client-before", "email": "browser-client", "requested_by": "hosted-browser",
                 "allow_blocked_egress": True},
    )
    assert code == 200 and accepted.get("ok") is True, accepted
    from .joined_support import await_core_job
    from .xray_support import loaded_identities
    creation = await_core_job(api, accepted)
    assert creation.get("status") == "success", creation
    result = creation.get("result") if isinstance(creation.get("result"), dict) else {}
    xray_result = result.get("xray_client") if isinstance(result.get("xray_client"), dict) else {}
    client = xray_result.get("client") if isinstance(xray_result.get("client"), dict) else {}
    client_id, email = str(client.get("client_id") or ""), str(client.get("email") or "")
    assert client_id and email == "browser-client"
    assert (client_id, email) in loaded_identities(stack["native"])

    api_origin = api.removesuffix("/api/v2")
    server_type = type("BoundUiApiBridge", (_UiAndApiBridge,), {
        "api_base": api_origin,
        "ui_root": Path("/workspace/ui"),
    })
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), server_type)
    server.daemon_threads = False
    server.block_on_close = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    browser = None
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=profile["chromium"]["path"],
                                                  args=["--disable-dev-shm-usage", "--no-proxy-server"])
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            browser_origin = f"http://127.0.0.1:{server.server_port}"
            page.route("**/*", lambda route: route.continue_() if urllib.parse.urlparse(route.request.url).netloc == f"127.0.0.1:{server.server_port}" else route.abort())
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(browser_origin + "/", wait_until="domcontentloaded", timeout=15000)
            page.locator(".seg__btn[data-view='settings']").click()
            page.locator("#settingsClientsTabVless").click()
            create_requests: list[str] = []
            page.on("request", lambda request: create_requests.append(request.url)
                    if request.method == "POST" and request.url.endswith("/api/v2/xray/clients") else None)
            page.locator("#settingsExternalClientCreateHeader").click()
            page.locator("#settingsExternalClientCreateSubmit").click()
            assert page.locator("#settingsExternalClientCreateState").inner_text().strip()
            assert not create_requests, "invalid UI form must be rejected before issuing an API mutation"
            page.locator("#settingsExternalClientAlias").fill("browser-ui-create-denied")
            page.locator("#settingsExternalClientEmail").fill("browser-ui-create-denied")
            with page.expect_response(lambda response: response.url.endswith("/api/v2/xray/clients")
                                      and response.request.method == "POST", timeout=12000) as create_response:
                page.locator("#settingsExternalClientCreateSubmit").click()
            create_body = create_response.value.json()
            assert create_response.value.status == 200, create_body
            from .xray_support import await_job
            failed_create = await_job(api, create_body)
            assert failed_create.get("status") == "failed", failed_create
            failure_result = failed_create.get("result") if isinstance(failed_create.get("result"), dict) else {}
            failure_client = failure_result.get("xray_client") if isinstance(failure_result.get("xray_client"), dict) else {}
            assert failure_client.get("ok") is False, failed_create
            page.wait_for_function(
                "document.querySelector('#settingsExternalClientCreateState')?.dataset.actionState === 'FAILED'",
                timeout=15000,
            )
            assert "browser-ui-create-denied" not in page.locator("#settingsClientsWrap").inner_text()
            assert page.locator("#settingsExternalClientCreateState").inner_text().strip()
            row = page.locator(f'[data-settings-client-row="{client_id}"]')
            row.wait_for(state="visible", timeout=15000)
            alias = row.locator("[data-settings-alias-for]")
            alias.fill("browser-client-edited")
            with page.expect_response(lambda response: response.url.endswith(f"/xray/clients/{client_id}") and response.request.method == "PATCH", timeout=12000) as patch_response:
                row.locator(f'[data-settings-save-item="{client_id}"]').click()
            assert patch_response.value.status == 200
            page.wait_for_function(
                "([id, value]) => document.querySelector(`[data-settings-client-row=\"${CSS.escape(id)}\"] [data-settings-alias-for]`)?.value === value",
                arg=[client_id, "browser-client-edited"], timeout=15000,
            )
            assert (client_id, email) in loaded_identities(stack["native"])

            delete = page.locator(f'[data-settings-delete-kind="xray_client"][data-settings-delete-id="{client_id}"]')
            with page.expect_response(lambda response: response.url.endswith(f"/xray/clients/{client_id}") and response.request.method == "DELETE", timeout=12000) as delete_response:
                delete.click()
            assert delete_response.value.status == 200
            page.wait_for_selector(f'[data-settings-client-row="{client_id}"]', state="detached", timeout=15000)
            assert (client_id, email) not in loaded_identities(stack["native"])
            assert not page_errors, page_errors
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
