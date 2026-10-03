"""
Shared SimCorp portal plumbing.
===============================

Login, waiting and locator-fallback helpers used by every part of the app that
drives the portal: the Log-a-Case wizard, the case reader, and the long-lived
browser session behind the Respond tab.

The portal is Salesforce. Two rules from DEVELOPMENT_NOTES.md apply everywhere:

  - Never select an element by absolute x/y. The browser runs with no fixed
    viewport, so coordinates move with the window size.
  - Expect a locator to miss. Most interactions need a short ladder of
    fallbacks, which is what `try_strategies` exists for.
"""

from __future__ import annotations

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
)

PORTAL_ROOT = "https://supportportal.simcorp.com"
LOGIN_URL = f"{PORTAL_ROOT}/s/login/"
PORTAL_HOME_URL = f"{PORTAL_ROOT}/s/"
SERVICE_CATALOG_URL = f"{PORTAL_ROOT}/s/service-catalog"
CASE_LIST_URL = f"{PORTAL_ROOT}/s/case-lists"

# A short per-action timeout lets a missed locator fall through to the next
# fallback quickly instead of stalling for Playwright's 30 s default.
ACTION_TIMEOUT_MILLISECONDS = 8_000
NAVIGATION_TIMEOUT_MILLISECONDS = 30_000
NETWORK_IDLE_TIMEOUT_MILLISECONDS = 25_000

# Slow the browser slightly so Lightning keeps up with scripted input.
BROWSER_SLOW_MOTION_MILLISECONDS = 150


def try_strategies(strategies, report_progress, description) -> bool:
    """Run each callable in `strategies` until one succeeds.

    The portal offers few stable ids, so most interactions need a ladder of
    fallback locators. Only the final failure is reported, because the earlier
    misses are expected rather than problems. Returns True if any succeeded.
    """
    last_strategy_index = len(strategies) - 1
    for strategy_index, strategy in enumerate(strategies):
        try:
            strategy()
            return True
        except Exception as error:  # noqa: BLE001 - fallbacks are the design
            if strategy_index == last_strategy_index:
                report_progress(f"  ! could not {description}: {error}")
    return False


def wait_until(page, condition, timeout_seconds: int) -> bool:
    """Poll `condition` once a second until it holds or the timeout expires.

    Lightning renders components asynchronously behind a spinner, so waiting for
    an element to *exist* is the reliable way to know a step is ready.
    """
    for _ in range(timeout_seconds):
        try:
            if condition():
                return True
        except PlaywrightError:
            pass
        page.wait_for_timeout(1_000)
    return False


def wait_for_network_idle(
    page, timeout_milliseconds: int = NETWORK_IDLE_TIMEOUT_MILLISECONDS
) -> None:
    """Wait for the network to quieten, tolerating the usual timeout.

    Lightning holds long-polling connections open, so 'networkidle' regularly
    times out even on a fully rendered page. That is not a failure.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_milliseconds)
    except PlaywrightTimeoutError:
        pass


def report_page_fields(page, report_progress) -> None:
    """Log the inputs and buttons present, to make a selector miss diagnosable
    from the progress log alone."""
    try:
        page_contents = page.evaluate(
            """() => {
                const all = (selector) => Array.from(document.querySelectorAll(selector));
                const describe = (element) => ({
                    tag: element.tagName.toLowerCase(),
                    type: element.getAttribute('type'),
                    id: element.id || null,
                    name: element.getAttribute('name'),
                    placeholder: element.getAttribute('placeholder'),
                    label: (element.getAttribute('aria-label') || '').slice(0, 40),
                    text: (element.innerText || element.value || '').trim().slice(0, 40),
                });
                return {
                    inputs: all('input, textarea, select').map(describe),
                    buttons: all('button, input[type=submit], input[type=button]').map(describe),
                };
            }"""
        )
    except PlaywrightError as error:
        report_progress(f"  (could not inspect the page: {error})")
        return

    report_progress("  --- fields on page (for selector tuning) ---")
    for input_description in page_contents.get("inputs", []):
        report_progress(f"    {input_description}")
    for button_description in page_contents.get("buttons", []):
        report_progress(f"    BTN {button_description}")
    report_progress("  --------------------------------------------")



def is_authenticated_page(url: str) -> bool:
    """True when the URL is a usable portal page rather than a staging step.

    Salesforce bounces through /secur/frontdoor.jsp while it establishes the
    session. Landing there means the login has not finished, and anything that
    navigates immediately afterwards gets a half-built page.
    """
    url = url or ""
    if "/s/" not in url:
        return False
    return "frontdoor" not in url and "/s/login" not in url


def log_in_to_portal(page, username, password, report_progress) -> None:
    """Log in and wait for the redirect into /s/.

    The login inputs carry no usable id or name - only placeholder text - so
    placeholder locators come first and everything else is a fallback.
    """
    report_progress("Opening login page...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    report_progress("Entering credentials...")
    username_was_filled = try_strategies(
        [
            lambda: page.get_by_placeholder("Username").first.fill(username),
            lambda: page.get_by_role("textbox", name="Username").first.fill(username),
            lambda: page.locator("input[placeholder='Username']").first.fill(username),
            lambda: page.locator("#username").first.fill(username),
            lambda: page.locator("input[name='username']").first.fill(username),
            lambda: page.locator("input[type='email']").first.fill(username),
            lambda: page.locator("input[type='text']").first.fill(username),
        ],
        report_progress,
        "fill username",
    )
    if not username_was_filled:
        report_page_fields(page, report_progress)

    try_strategies(
        [
            lambda: page.get_by_placeholder("Password").first.fill(password),
            lambda: page.locator("input[placeholder='Password']").first.fill(password),
            lambda: page.locator("#password").first.fill(password),
            lambda: page.locator("input[name='password']").first.fill(password),
            lambda: page.locator("input[type='password']").first.fill(password),
        ],
        report_progress,
        "fill password",
    )

    report_progress("Clicking Log in...")
    try_strategies(
        [
            lambda: page.get_by_role("button", name="Log in").click(),
            lambda: page.locator("#Login").click(),
            lambda: page.locator("input[value='Log in']").click(),
            lambda: page.get_by_text("Log in", exact=True).click(),
        ],
        report_progress,
        "click Log in",
    )

    try:
        page.wait_for_url("**/s/**", timeout=NAVIGATION_TIMEOUT_MILLISECONDS)
    except PlaywrightTimeoutError:
        report_progress("  (login redirect not detected - check credentials)")
    wait_for_network_idle(page, NAVIGATION_TIMEOUT_MILLISECONDS)
    page.wait_for_timeout(2_000)

    # The redirect can settle on /secur/frontdoor.jsp rather than a real page.
    # Navigating from there gives a half-built page, which used to surface much
    # later as an empty case list, so the session is confirmed here instead.
    for attempt in range(3):
        if is_authenticated_page(page.url):
            break
        report_progress("  session still settling, opening the portal home...")
        try:
            page.goto(PORTAL_HOME_URL, wait_until="domcontentloaded")
        except PlaywrightError as error:
            report_progress(f"  (could not open the portal home: {error})")
        wait_for_network_idle(page, NAVIGATION_TIMEOUT_MILLISECONDS)
        page.wait_for_timeout(3_000)

    report_progress(f"  logged in, now at: {page.url}")
    if not is_authenticated_page(page.url):
        report_progress("  ! the portal did not settle on a signed-in page")
