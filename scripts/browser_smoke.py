"""Small real-browser smoke test for the deployed SPA shell and API readiness.

This is intentionally not described as full payment E2E: the CI payment provider
is demo-only, and no real gateway credentials or transactions are used.
"""
from playwright.sync_api import sync_playwright

BASE_URL = "http://127.0.0.1:5000"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page_errors = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        response = page.goto(BASE_URL, wait_until="networkidle", timeout=30_000)
        assert response is not None and response.status == 200, (
            f"SPA shell failed to load: {response.status if response else 'no response'}"
        )
        assert page.locator("body").count() == 1
        assert page.locator("body").inner_text().strip(), "SPA rendered an empty body"

        readiness = page.request.get(f"{BASE_URL}/api/ready")
        assert readiness.status == 200, f"API readiness failed: {readiness.status}"
        readiness_json = readiness.json()
        assert readiness_json.get("ok") is True, readiness_json
        assert readiness_json.get("checks", {}).get("database") is True, readiness_json

        # Browser must be able to fetch the app's core script and stylesheet.
        for path in ("/js/app.js", "/css/styles.css"):
            asset = page.request.get(f"{BASE_URL}{path}")
            assert asset.status == 200, f"{path} returned {asset.status}"

        assert not page_errors, f"Uncaught browser JavaScript errors: {page_errors}"
        browser.close()
        print("Browser smoke passed: SPA shell, core assets, API readiness, no uncaught JS errors.")


if __name__ == "__main__":
    main()
