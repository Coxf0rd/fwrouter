from __future__ import annotations

import http.server
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
