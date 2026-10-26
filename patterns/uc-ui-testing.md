# Pattern: UC-Derived UI Flow Tests

> Used by: all instances that own use cases
> Related: `integration-testing.md`, `unit-testing.md`

## Context

Each use case (UC) has a main flow that a user executes through the UI. An instance that defines a UC is also responsible for writing a Playwright test that verifies that flow. This pattern describes how to do that.

UI flow tests live in `backend/tests/integration/` alongside the API tests. They use the shared Playwright helpers in `playwright_helpers.py` and the fixtures in `conftest.py`.

---

## File naming

```
backend/tests/integration/test_<project>_<tool>_ui.py
```

Examples:
- `test_p8_delegation_ui.py`   — delegation UC flows
- `test_p8_positions_ui.py`    — positions UC flows
- `test_p2_ui_walkthrough.py`  — visualizer UC flows

---

## Minimum boilerplate

```python
"""
<Tool> — UC flow tests.

Covers: UC-XXX-NNN <use case title>
UC precondition: <what must exist in the org>
UC main flow:    <numbered steps from the UC definition>
"""
import os
from pathlib import Path
import pytest
from dotenv import load_dotenv
from playwright_helpers import (
    create_authed_storage_state,
    assert_no_critical_errors,
    start_error_capture,
    screenshot,
)

_root = Path(__file__).parent.parent.parent.parent
load_dotenv(_root / ".env", override=False)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    PlaywrightTimeout = Exception

pytestmark = [
    pytest.mark.frontend,
    pytest.mark.ui_walkthrough,
    pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed"),
]

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
AUTH_TOKEN  = os.environ.get("AUTH_TEST_SERVICE_TOKEN", "")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture(scope="module")
def authed_storage_state(browser):
    """Module-scoped login — avoids the Auth0 /userinfo rate-limit (~7/window).
    See playwright_helpers.create_authed_storage_state for details."""
    return create_authed_storage_state(browser, auth_token=AUTH_TOKEN)


# ── Test class ────────────────────────────────────────────────────────────────

class TestDelegationUCCreate:
    """UC-MGT-004: Manager creates a bounded-authority delegation."""

    def test_open_delegation_tool(self, browser, authed_storage_state, loaded_org):
        ctx = browser.new_context(storage_state=authed_storage_state)
        page = ctx.new_page()
        errors = start_error_capture(page)
        try:
            page.goto(f"{GATEWAY_URL}/#/tools/delegation")
            page.wait_for_selector("[data-testid='delegation-panel']", timeout=10000)
            screenshot(page, "delegation-open")
            assert_no_critical_errors(errors)
        finally:
            ctx.close()

    def test_create_delegation(self, browser, authed_storage_state, loaded_org):
        ctx = browser.new_context(storage_state=authed_storage_state)
        page = ctx.new_page()
        errors = start_error_capture(page)
        try:
            page.goto(f"{GATEWAY_URL}/#/tools/delegation")
            page.click("[data-testid='create-delegation-btn']")
            page.wait_for_selector("[data-testid='delegation-form']")
            # Fill form
            page.fill("[data-testid='delegation-title']", "Test delegation")
            page.click("[data-testid='delegation-submit']")
            page.wait_for_selector("[data-testid='delegation-success']", timeout=8000)
            screenshot(page, "delegation-created")
            assert_no_critical_errors(errors)
        finally:
            ctx.close()
```

---

## Wizard UCs — mandatory full-walk

If the UC is wizard-based (multi-step sequence with Next/Back navigation), you **must** follow `wizard-ui-standard.md` in addition to this pattern.

The single most important rule: **a UC test that does not walk the entire wizard is not a real test.** A test that only opens step 1, or submits without traversing every step, does not verify the UC — it verifies that the wizard renders. See `wizard-ui-standard.md` for the canonical full-walk template, the required `data-testid` convention, and the explicit anti-patterns to avoid.

---

## Key rules

### 1. Module-scoped auth — mandatory
Always use `authed_storage_state` at `scope="module"`. Per-test login hits the Auth0 `/userinfo` rate-limit (~7 per window) and causes all tests after the 7th to land on the demo-login picker and hang. This is the most common failure mode.

```python
# RIGHT — module scope
@pytest.fixture(scope="module")
def authed_storage_state(browser):
    return create_authed_storage_state(browser, auth_token=AUTH_TOKEN)

# WRONG — function scope causes rate-limit failures
@pytest.fixture(scope="function")
def authed_storage_state(browser):
    ...
```

### 2. Always capture console errors
```python
errors = start_error_capture(page)
# ... test actions ...
assert_no_critical_errors(errors)
```
This catches React rendering crashes and uncaught promise rejections that don't fail the page load.

### 3. Use `data-testid` attributes for selectors
Never select by CSS class, text content, or position. If the element you need doesn't have a `data-testid`, add one to the frontend component first. The testid lives in the compiled `dist/` — rebuild before adding tests:
```bash
cd frontend/app && npm run build
```

### 4. Deep-link into the tool
Navigate directly to the tool's route rather than clicking through the shell nav. This makes tests faster and isolated to the UC:
```python
page.goto(f"{GATEWAY_URL}/#/tools/delegation")          # P8 delegation
page.goto(f"{GATEWAY_URL}/#/tools/functions")           # P8 functions planner
page.goto(f"{GATEWAY_URL}/#/editor")                    # P4 org editor
page.goto(f"{GATEWAY_URL}/#/visualizer")                # P2 visualizer
```
See `shared/patterns/routes.md` for the full route table.

### 5. Mock AI endpoints for deterministic tests
If the UC step calls an AI endpoint, mock it so the test doesn't depend on a live Claude API call:
```python
from playwright_helpers import mock_ai_endpoints
mock_ai_endpoints(page, response={"suggestions": [{"text": "Mock suggestion"}]})
```

### 5b. Mocking API responses with `page.route` — ordering constraint

When using `page.route()` to intercept and mock arbitrary API responses, the route must be registered **before** the navigation that triggers the request. If you need to mock a response on a page that's already open, reload after setting the route:

```python
def test_feature_with_mocked_api(self, browser, authed_storage_state, loaded_org):
    ctx = browser.new_context(storage_state=authed_storage_state)
    page = ctx.new_page()
    try:
        # 1. Navigate first to establish the page
        page.goto(f"{GATEWAY_URL}/#/tools/my-feature")
        page.wait_for_selector("[data-testid='my-panel']")

        # 2. Register route AFTER open, BEFORE the action that triggers the call
        page.route("**/api/my-endpoint", lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"result": "mocked"}'
        ))

        # 3. Trigger the action — the route intercepts it
        page.click("[data-testid='fetch-button']")
        page.wait_for_selector("[data-testid='result-panel']")

    finally:
        ctx.close()
```

If the call fires on page load (not on user action), register the route first, then navigate:
```python
page.route("**/api/on-load-endpoint", lambda route: route.fulfill(...))
page.goto(f"{GATEWAY_URL}/#/tools/my-feature")  # route already set — intercepts load call
```

### 6. Use `loaded_org` fixture for tests that need data
The `loaded_org` fixture (from `conftest.py`) provides a StartupX org with populated entities. Use it when the UC precondition requires existing org data:
```python
def test_view_delegation_list(self, browser, authed_storage_state, loaded_org):
    ...
```

---

## Linking the test to the graph

After writing the test, link it to the UC's features:
```bash
python3 scripts/sys_edge.py link-feature \
  --feature F-DLG-001 \
  --tests "test_p8_delegation_ui.py::TestDelegationUCCreate::test_create_delegation"
```

And record the test run:
```bash
python3 scripts/sys_edge.py record-run \
  --package "backend/tests/integration/test_p8_delegation_ui.py" \
  --passed 2 --failed 0 --skipped 0 --duration 8.4
```

---

## Running the tests

```bash
# Run all UI tests for your project
cd /path/to/management-system
python -m pytest backend/tests/integration/test_p8_delegation_ui.py -v -s -m ui_walkthrough

# Prerequisites
# - Caddy gateway running on :8000
# - Go backends running (core:8080, tools:8081, platform:8082)
# - Neo4j running with data loaded
# - Playwright browsers: python -m playwright install chromium
# - AUTH_TEST_SERVICE_TOKEN set in .env
```

---

## Used by

All instances that own use cases: `p1`, `core`, `manage`, `plan`, `platform`, `training`, `licmcp`, `framework`
