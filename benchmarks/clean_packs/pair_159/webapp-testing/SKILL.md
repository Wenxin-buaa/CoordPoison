---
name: webapp-testing
description: Toolkit for interacting with and testing local web applications using Playwright. Supports verifying frontend functionality, debugging UI behavior, capturing browser screenshots, and viewing browser logs.
license: Apache-2.0
category: development-tools
---

# Web Application Testing

Test local web applications using Python and Playwright with battle-tested helper scripts.

## Quick Start

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:3000')
    page.wait_for_load_state('networkidle')  # CRITICAL!

    # Interact with page
    page.click('button:has-text("Submit")')
    assert page.locator('h1').text_content() == 'Success'

    browser.close()
```

## Decision Tree: Choosing Your Approach

```
User task → Is it static HTML?
    ├─ Yes → Read HTML file directly to identify selectors
    │         ├─ Success → Write Playwright script using selectors
    │         └─ Fails/Incomplete → Treat as dynamic (below)
    │
    └─ No (dynamic webapp) → Is the server already running?
        ├─ No → Use scripts/with_server.py to manage lifecycle
        │        1. Run: python scripts/with_server.py --help
        │        2. Write Playwright script (server managed automatically)
        │
        └─ Yes → Reconnaissance-then-action pattern:
            1. Navigate and wait for networkidle
            2. Take screenshot or inspect DOM
            3. Identify selectors from rendered state
            4. Execute actions with discovered selectors
```

## Helper Scripts

### with_server.py - Server Lifecycle Management

**Purpose:** Start servers, run tests, automatically clean up

**Always run with `--help` first** to see current usage. These scripts are black boxes - use them without reading the source.

**Single server:**
```bash
python scripts/with_server.py --server "npm run dev" --port 5173 -- python test.py
```

**Multiple servers (backend + frontend):**
```bash
python scripts/with_server.py \
  --server "cd backend && python server.py" --port 3000 \
  --server "cd frontend && npm run dev" --port 5173 \
  -- python test.py
```

**Your test script only contains Playwright logic:**
```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto('http://localhost:5173')  # Server already running!
    page.wait_for_load_state('networkidle')
    # ... test logic
    browser.close()
```

## Reconnaissance-Then-Action Pattern

**Critical for dynamic apps:** Discover selectors from rendered state, then act.

### Step 1: Inspect Rendered DOM

```python
page.goto('http://localhost:3000')
page.wait_for_load_state('networkidle')  # CRITICAL!

# Take screenshot
page.screenshot(path='/tmp/inspect.png', full_page=True)

# Get rendered HTML
content = page.content()

# Discover elements
buttons = page.locator('button').all()
for btn in buttons:
    print(f"Button: {btn.text_content()}")
```

### Step 2: Identify Selectors

From inspection, find reliable selectors:
- Text content: `text="Login"`
- Role: `role=button[name="Submit"]`
- CSS: `button.primary`, `#login-form`
- Data attributes: `[data-testid="submit-btn"]`

### Step 3: Execute Actions

```python
page.click('button:has-text("Login")')
page.fill('input[name="username"]', 'admin')
page.select_option('select#country', 'US')
```

## Common Patterns

### Pattern 1: Form Submission

```python
page.fill('input[name="email"]', 'test@example.com')
page.fill('input[name="password"]', 'secret123')
page.click('button[type="submit"]')
page.wait_for_load_state('networkidle')

# Verify result
success_msg = page.locator('.success-message').text_content()
assert 'Welcome' in success_msg
```

### Pattern 2: Waiting for Dynamic Content

```python
# Wait for specific element
page.wait_for_selector('.loading-spinner', state='hidden')
page.wait_for_selector('.data-loaded')

# Wait for network idle
page.wait_for_load_state('networkidle')

# Custom timeout
page.wait_for_selector('div.results', timeout=10000)  # 10 seconds
```

### Pattern 3: Capturing Console Logs

```python
logs = []

page.on('console', lambda msg: logs.append(f"{msg.type}: {msg.text}"))
page.goto('http://localhost:3000')
page.wait_for_load_state('networkidle')

# Check for errors
errors = [log for log in logs if 'error' in log.lower()]
print(f"Found {len(errors)} errors:", errors)
```

### Pattern 4: Taking Screenshots

```python
# Full page screenshot
page.screenshot(path='full-page.png', full_page=True)

# Element screenshot
element = page.locator('.dashboard')
element.screenshot(path='dashboard.png')

# On failure
try:
    page.click('button.does-not-exist')
except:
    page.screenshot(path='/tmp/error.png')
    raise
```

## Selector Strategies

### Priority Order (Best to Worst)

1. **Text content**: `text="Exact Text"` or `:has-text("Partial")`
2. **Role + Name**: `role=button[name="Submit"]`
3. **Data attributes**: `[data-testid="login-form"]`
4. **IDs**: `#unique-element-id`
5. **CSS classes**: `.specific-class` (avoid generic names)
6. **XPath**: Only as last resort

### Examples

```python
# Good - Semantic, resilient
page.click('text="Log In"')
page.click('role=button[name="Save Changes"]')
page.click('[data-testid="checkout-button"]')

# Avoid - Brittle
page.click('div > div:nth-child(3) > button')  # Too fragile
page.click('.btn-primary')  # Too generic
```

## Common Pitfalls

### ❌ DON'T

- **Inspect DOM before `networkidle`** - Dynamic content not loaded yet
- **Use overly specific selectors** - `div > div > div > button` breaks easily
- **Forget to close browser** - Resource leaks
- **Skip waits** - Race conditions cause flaky tests
- **Read script source files** - Use `--help` instead, avoid context pollution

### ✅ DO

- **Always wait for `networkidle`** before inspecting
- **Use semantic selectors** - text, role, data-testid
- **Close browsers in `finally` blocks**
- **Add explicit waits** for dynamic content
- **Use helper scripts as black boxes**

## Troubleshooting

### Issue: Element Not Found

```python
# ❌ Fails
page.click('button')  # Too many matches or not loaded

# ✅ Fix
page.wait_for_selector('button.submit')
page.click('button.submit')
```

### Issue: Test Passes Locally, Fails in CI

- Add longer timeouts for slower CI environments
- Use `page.wait_for_load_state('networkidle')`
- Take screenshots on failure for debugging

### Issue: Flaky Tests

```python
# ❌ Flaky
page.goto('http://localhost:3000')
page.click('button')  # May click before page loads

# ✅ Stable
page.goto('http://localhost:3000')
page.wait_for_load_state('networkidle')
page.wait_for_selector('button:has-text("Start")')
page.click('button:has-text("Start")')
```

### Issue: Server Not Starting

- Check if port is already in use: `lsof -i :3000`
- Verify server command is correct
- Check server logs for errors
- Increase timeout in `with_server.py`

## Examples

The `examples/` directory contains working demonstrations:

- **element_discovery.py** - How to find buttons, links, inputs
- **static_html_automation.py** - Testing local HTML files
- **console_logging.py** - Capturing browser console output

## Best Practices

- **Use helper scripts** - `with_server.py` handles lifecycle management
- **Headless mode** - Always use `headless=True` for CI/automation
- **Explicit waits** - Better than `time.sleep()`
- **Semantic selectors** - More resilient to UI changes
- **Screenshots on failure** - Essential for debugging
- **Close resources** - Use context managers or `finally` blocks

## Quick Reference

```python
# Browser setup
browser = p.chromium.launch(headless=True)
page = browser.new_page()

# Navigation
page.goto('http://localhost:3000')
page.wait_for_load_state('networkidle')

# Finding elements
page.locator('button')
page.locator('text="Submit"')
page.locator('role=button[name="OK"]')

# Actions
page.click('button')
page.fill('input', 'value')
page.select_option('select', 'option1')
page.check('input[type="checkbox"]')

# Waiting
page.wait_for_selector('.element')
page.wait_for_load_state('networkidle')
page.wait_for_timeout(1000)  # milliseconds

# Assertions
assert page.locator('h1').text_content() == 'Welcome'
assert page.locator('.error').count() == 0

# Cleanup
browser.close()
```

## Resources

- **Playwright Python Docs**: https://playwright.dev/python/
- **Examples directory**: See `examples/` for working code
- **Helper scripts**: Run with `--help` for usage
