"""
Magpie - SimCorp support portal case manager
============================================

A desktop app with two tabs:

  Log a Case         capture a Subject, Description and Files, then drive a
                     Chromium browser (hidden unless asked for) to log in to the
                     SimCorp support portal, create a Dimension > Error case,
                     submit it, upload the files and post them as case comments.
  Respond to a Case  browse your cases, read any thread in order, and reply
                     underneath a chosen post.

This module owns the Log-a-Case browser automation and starts the app. The rest
lives alongside it:

  sc_settings.py        paths, stored settings, the case submission record
  sc_portal.py          login and the shared waiting / locator-fallback helpers
  sc_portal_session.py  the long-lived headless browser behind the Respond tab
  sc_portal_pool.py     several browsers sharing one login, for reading a backlog
  sc_case_reader.py     reading a case thread and replying to a post
  sc_case_cache.py      the SQLite cache of the case list and the threads read
  sc_thread_view.py     rendering a thread into the Tk text widget
  sc_image_viewer.py    the zoomable screenshot window
  sc_gui.py             the window

Selector choices here are deliberate and hard-won; read DEVELOPMENT_NOTES.md
before changing them. In particular the wizard is a Lightning Web Component, so
locators must pierce shadow DOM; never select by absolute x/y, because the
browser runs with no fixed viewport; and a Salesforce Chatter comment accepts
exactly ONE attachment, hence one comment per file.

Run:
    python sc_case_logger.py
"""

from __future__ import annotations

import os
import traceback
from datetime import date

import sc_settings

APPLICATION_PATHS = sc_settings.ApplicationPaths(sc_settings.application_directory())

# Keep the Playwright browser in a 'browsers' folder beside the app so it lives
# inside the whitelisted tree and the folder stays portable. This MUST be set
# before Playwright is imported, hence before the imports below. An existing
# value (e.g. from a launcher) deliberately wins.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", APPLICATION_PATHS.browsers_directory)

from playwright.sync_api import (  # noqa: E402 - must follow the env var above
    Error as PlaywrightError,
    sync_playwright,
)

import sc_gui  # noqa: E402
import sc_portal  # noqa: E402
from sc_portal import try_strategies, wait_for_network_idle, wait_until  # noqa: E402

# Category / catalog item to select in the service catalog.
CATALOG_CATEGORY = "Dimension"
CATALOG_ITEM = "Error"

# How long to poll for asynchronously rendered Lightning components.
ELEMENT_POLL_TIMEOUT_SECONDS = 30

#: The dialog has two steps for Operation and three for Transition, so the
#: steps are walked until the upload step shows rather than counted.
MAXIMUM_DIALOG_STEPS = 6

#: The project radios carry a random suffix on their name, regenerated on
#: every load, so they are matched on their value instead.
PROJECT_RADIO_SELECTOR = "input[name^='selProjectSelection']"
FILE_LIST_POLL_TIMEOUT_SECONDS = 20
BUTTON_ENABLED_POLL_TIMEOUT_SECONDS = 5

CLICK_TIMEOUT_MILLISECONDS = 6_000
PAGE_RELOAD_TIMEOUT_MILLISECONDS = 25_000

# Environment toggles kept for troubleshooting.
HEADLESS_ENVIRONMENT_VARIABLE = "SCL_HEADLESS"
DEBUG_SCREENSHOTS_ENVIRONMENT_VARIABLE = "SCL_DEBUG_SHOTS"


# --------------------------------------------------------------------------- #
# Value formatting
# --------------------------------------------------------------------------- #
PORTAL_MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def to_portal_date(iso_date: str) -> str:
    """Convert an ISO date 'YYYY-MM-DD' to the portal's format '13. Jul 2026'.

    The portal rejects any other format; its own validation message states
    "allowed format 31. Dec 2024" (day without a leading zero). Returns "" for
    empty or invalid input so the field is simply left blank.
    """
    if not iso_date:
        return ""
    try:
        year, month, day = (int(part) for part in iso_date.split("-"))
        parsed_date = date(year, month, day)
    except ValueError:
        return ""
    # Not strftime("%b"): that follows the process locale, and the portal only
    # accepts the English abbreviation.
    month_name = PORTAL_MONTH_ABBREVIATIONS[parsed_date.month - 1]
    return f"{parsed_date.day}. {month_name} {parsed_date.year}"


# --------------------------------------------------------------------------- #
# Page helpers specific to the wizard
# --------------------------------------------------------------------------- #
def click_leftmost_match(page, text, report_progress) -> None:
    """Click the left-most (smallest x) exact-text match.

    On the service catalog a category name appears twice: as the left-hand
    navigation item and again in the right-hand instructions panel. The
    navigation item is always the left-most one.
    """

    def click_by_position():
        matches = page.get_by_text(text, exact=True)
        match_count = matches.count()
        if match_count == 0:
            raise RuntimeError(f"no exact match for '{text}'")

        leftmost_index = 0
        leftmost_x = float("inf")
        for match_index in range(match_count):
            bounding_box = matches.nth(match_index).bounding_box()
            if bounding_box and bounding_box["x"] < leftmost_x:
                leftmost_x = bounding_box["x"]
                leftmost_index = match_index
        matches.nth(leftmost_index).click()

    try_strategies(
        [click_by_position, lambda: page.get_by_text(text, exact=True).first.click()],
        report_progress,
        f"click '{text}'",
    )


def fill_field_after_label(page, label, value, tag_name) -> None:
    """Fill the first `tag_name` element that follows the given label text."""
    page.locator(
        f"xpath=//*[contains(normalize-space(.),'{label}')]/following::{tag_name}[1]"
    ).first.fill(value)


def set_dropdown(page, label, value, report_progress) -> None:
    """Set a native <select> found by its associated label text."""

    def select_by_label():
        page.get_by_label(label, exact=False).first.select_option(label=value)

    def select_by_proximity():
        page.locator(
            f"xpath=//*[contains(normalize-space(.),'{label}')]/following::select[1]"
        ).first.select_option(label=value)

    was_set = try_strategies(
        [select_by_label, select_by_proximity], report_progress, f"set {label}={value}"
    )
    if not was_set:
        report_progress(f"  (leave '{label}' for manual selection)")


def click_first_visible_button(page, accessible_name) -> None:
    """Click the first visible button with the given accessible name.

    A case feed can hold several composers at once and only the expanded one has
    visible toolbar buttons.
    """
    buttons = page.get_by_role("button", name=accessible_name)
    for button_index in range(buttons.count()):
        button = buttons.nth(button_index)
        if button.is_visible():
            button.click()
            return
    page.locator(f"button[title='{accessible_name}']").first.click()


def save_debug_screenshot(page, name) -> None:
    """Save a screenshot when the debug-screenshot environment toggle is set."""
    if not os.environ.get(DEBUG_SCREENSHOTS_ENVIRONMENT_VARIABLE):
        return
    try:
        os.makedirs(APPLICATION_PATHS.screenshot_directory, exist_ok=True)
        page.screenshot(
            path=os.path.join(APPLICATION_PATHS.screenshot_directory, f"{name}.png"),
            full_page=True,
        )
    except (OSError, PlaywrightError):
        # Diagnostics must never interrupt the run.
        pass


# --------------------------------------------------------------------------- #
# Automation steps
# --------------------------------------------------------------------------- #
def open_log_a_case_dialog(page, report_progress) -> None:
    """Navigate the service catalog to Dimension > Error and wait for the form."""
    report_progress("Opening Service Catalog...")
    page.goto(sc_portal.SERVICE_CATALOG_URL, wait_until="domcontentloaded")
    wait_for_network_idle(page, sc_portal.NAVIGATION_TIMEOUT_MILLISECONDS)
    page.wait_for_timeout(3_500)

    report_progress(f"Selecting category '{CATALOG_CATEGORY}'...")
    click_leftmost_match(page, CATALOG_CATEGORY, report_progress)
    page.wait_for_timeout(2_000)

    report_progress(f"Selecting catalog item '{CATALOG_ITEM}'...")
    try_strategies(
        [
            lambda: page.get_by_text(CATALOG_ITEM, exact=True).first.click(),
            lambda: page.get_by_role("link", name=CATALOG_ITEM, exact=True).first.click(),
            lambda: page.get_by_role("cell", name=CATALOG_ITEM, exact=True).first.click(),
        ],
        report_progress,
        f"click item {CATALOG_ITEM}",
    )

    report_progress("Waiting for 'Log a Case' dialog...")
    try_strategies(
        [
            lambda: page.get_by_text("Log a Case", exact=False).first.wait_for(
                timeout=sc_portal.NAVIGATION_TIMEOUT_MILLISECONDS
            )
        ],
        report_progress,
        "detect dialog",
    )

    # The dialog renders its fields behind a spinner; wait for Subject to exist.
    wait_until(
        page,
        lambda: page.get_by_label("Subject", exact=False).count() > 0,
        ELEMENT_POLL_TIMEOUT_SECONDS,
    )
    page.wait_for_timeout(1_500)


def fill_case_details_step(page, submission, report_progress) -> None:
    """Fill step 1 of the dialog: the required dropdowns, Subject, Description."""
    report_progress("Setting required dropdowns...")
    set_dropdown(page, "Priority", submission.priority, report_progress)
    set_dropdown(
        page, "Operations & Onboarding", submission.operations_and_onboarding, report_progress
    )
    set_dropdown(page, "Installation", submission.installation, report_progress)

    report_progress(f"Filling Subject: {submission.subject}")
    try_strategies(
        [
            lambda: page.get_by_label("Subject", exact=False).first.fill(submission.subject),
            lambda: fill_field_after_label(page, "Subject", submission.subject, "input"),
        ],
        report_progress,
        "fill Subject",
    )

    report_progress("Filling Description...")
    try_strategies(
        [
            lambda: page.get_by_label("Description", exact=False).first.fill(
                submission.description
            ),
            lambda: fill_field_after_label(
                page, "Description", submission.description, "textarea"
            ),
        ],
        report_progress,
        "fill Description",
    )
    page.wait_for_timeout(500)


def click_next_button(page, report_progress, description) -> None:
    report_progress(description)
    try_strategies(
        [
            lambda: page.get_by_role("button", name="Next", exact=True).click(),
            lambda: page.locator("button:has-text('Next')").first.click(),
        ],
        report_progress,
        "click Next",
    )


def fill_business_impact_step(page, submission, report_progress) -> None:
    """Fill step 2 of the dialog. Every field here is optional."""
    if submission.business_impact:
        report_progress("Filling Business Impact...")
        # get_by_label("Business Impact") also matches the info/help button that
        # shares the label text, so target the visible textarea directly.
        try_strategies(
            [
                lambda: page.locator("textarea:visible").first.fill(submission.business_impact),
                lambda: fill_field_after_label(
                    page, "Business Impact", submission.business_impact, "textarea"
                ),
            ],
            report_progress,
            "fill Business Impact",
        )

    happened_date = to_portal_date(submission.happened_date)
    if happened_date:
        report_progress(f"Filling 'When did it happen?' = {happened_date}")
        try_strategies(
            [lambda: page.locator("input[name='datWhenDidItHappen']").first.fill(happened_date)],
            report_progress,
            "fill when-happened",
        )

    last_worked_date = to_portal_date(submission.last_worked_date)
    if last_worked_date:
        report_progress(f"Filling 'When did it last work?' = {last_worked_date}")
        try_strategies(
            [
                lambda: page.locator("input[name='datWhenDidItLastWork']").first.fill(
                    last_worked_date
                )
            ],
            report_progress,
            "fill when-lastwork",
        )
    page.wait_for_timeout(500)



def project_step_is_showing(page) -> bool:
    """True when the dialog is asking which project the case belongs to."""
    try:
        return page.locator(PROJECT_RADIO_SELECTOR).count() > 0
    except PlaywrightError:
        return False


def business_impact_step_is_showing(page) -> bool:
    try:
        return page.get_by_text("Business Impact").count() > 0
    except PlaywrightError:
        return False


def upload_step_is_showing(page) -> bool:
    """True once the wizard has created the case and is asking for the files."""
    try:
        return (
            page.locator("input[type='file']").count() > 0
            or page.get_by_text("Upload Files").count() > 0
            or page.get_by_role("button", name="Finish").count() > 0
        )
    except PlaywrightError:
        return False


def select_transition_project(page, project_label, report_progress) -> None:
    """Tick the project radio that Transition insists on.

    The radios share a name whose suffix is regenerated on every load, so the
    value is the stable handle. Two of the seven carry the same visible label,
    which is the other reason the choice is held as a value rather than as text.
    """
    value = sc_settings.project_choice_value(project_label)
    if not value:
        report_progress(f"  ! '{project_label}' is not a known project; leaving it unset")
        return

    report_progress(f"Selecting project: {project_label}")
    radio = page.locator(f"input[value='{value}']").first

    def click_its_label():
        """Click the label, which is what actually takes the click.

        The radio itself is visually hidden and its <label> sits over the top,
        so clicking the input is refused with "label intercepts pointer events".
        """
        radio_id = radio.get_attribute("id")
        if not radio_id:
            raise RuntimeError("the project radio has no id to find its label by")
        page.locator(f"label[for='{radio_id}']").first.click(
            timeout=CLICK_TIMEOUT_MILLISECONDS
        )

    was_set = try_strategies(
        [
            click_its_label,
            lambda: radio.check(force=True),
            lambda: radio.click(force=True),
        ],
        report_progress,
        f"select project {project_label}",
    )
    if was_set:
        page.wait_for_timeout(1_000)
        if not _project_is_selected(page, value):
            report_progress("  ! the project did not stay selected")



def _project_is_selected(page, value: str) -> bool:
    """Confirm the radio actually took, rather than trusting the click."""
    try:
        return bool(page.locator(f"input[value='{value}']").first.is_checked())
    except PlaywrightError:
        return False

def advance_through_dialog(page, submission, report_progress) -> None:
    """Walk the wizard from the details step to the file-upload step.

    Operation and Transition present different steps - Transition swaps the
    Business Impact step for a required Project Selection - so each step is
    identified as it appears rather than assuming a fixed sequence. Without this
    a Transition case stalls on a step the flow did not expect.
    """
    for step_number in range(1, MAXIMUM_DIALOG_STEPS + 1):
        if upload_step_is_showing(page):
            report_progress("  reached the file-upload step.")
            return

        if project_step_is_showing(page):
            select_transition_project(page, submission.project, report_progress)
        elif business_impact_step_is_showing(page):
            fill_business_impact_step(page, submission, report_progress)
        else:
            report_progress(f"  (step {step_number}: nothing to fill in)")

        click_next_button(page, report_progress, f"Clicking Next (step {step_number})...")
        wait_until(
            page,
            lambda: upload_step_is_showing(page),
            ELEMENT_POLL_TIMEOUT_SECONDS,
        )
        page.wait_for_timeout(1_500)

    report_progress("  ! the dialog never reached the upload step; carrying on anyway")

def click_finish_button(page, report_progress, description) -> None:
    try_strategies(
        [
            lambda: page.get_by_role("button", name="Finish").click(),
            lambda: page.locator("button:has-text('Finish')").first.click(),
        ],
        report_progress,
        description,
    )


def complete_upload_wizard(page, file_paths, report_progress) -> None:
    """Handle the wizard's 'Upload Files' step, then click Finish.

    Files attached here land on the case's Related Files, which is what later
    makes them selectable under "Owned by Me" when posting comments.
    """
    if not file_paths:
        report_progress("No files to upload; clicking Finish.")
        click_finish_button(page, report_progress, "click Finish (no files)")
        return

    report_progress(f"Uploading {len(file_paths)} file(s)...")
    wait_until(
        page,
        lambda: (
            page.locator("input[type='file']").count() > 0
            or page.get_by_text("Upload Files").count() > 0
        ),
        ELEMENT_POLL_TIMEOUT_SECONDS,
    )

    files_were_attached = try_strategies(
        [lambda: page.locator("input[type='file']").first.set_input_files(file_paths)],
        report_progress,
        "attach files",
    )
    if files_were_attached:
        page.wait_for_timeout(3_000)  # let the upload complete
        # The upload sub-dialog shows 'Done' before the wizard's 'Finish'.
        try_strategies(
            [
                lambda: page.get_by_role("button", name="Done").click(),
                lambda: page.locator("button:has-text('Done')").first.click(),
            ],
            report_progress,
            "click Done",
        )
        page.wait_for_timeout(1_000)

    click_finish_button(page, report_progress, "click Finish")


def open_created_case(page, subject, report_progress) -> str:
    """Land on the newly created case page and return its URL.

    The wizard normally navigates straight to /s/case/<id>/. If it has not, fall
    back to My Cases at /s/case-lists and click the row matching the subject.
    Note the nav items are role="menuitem", not links, and the old /s/mycases
    URL renders "Invalid Page" - see DEVELOPMENT_NOTES.md §15.1.
    """
    page.wait_for_timeout(4_000)
    if "/s/case/" in page.url:
        report_progress(f"  landed on case page: {page.url}")
        return page.url

    report_progress("  opening 'My Cases' to find the new case...")
    try_strategies(
        [
            lambda: page.get_by_role("menuitem", name="My Cases").first.click(),
            lambda: page.goto(sc_portal.CASE_LIST_URL, wait_until="domcontentloaded"),
        ],
        report_progress,
        "open My Cases",
    )
    wait_for_network_idle(page)
    page.wait_for_timeout(4_000)

    try_strategies(
        [
            lambda: page.get_by_role("link", name=subject, exact=False).first.click(),
            lambda: page.get_by_text(subject, exact=False).first.click(),
        ],
        report_progress,
        "open case by subject",
    )
    page.wait_for_timeout(4_000)
    report_progress(f"  now at: {page.url}")
    return page.url


def reload_case_page(page, case_url, report_progress) -> None:
    """Reload the case and wait for its feed to be usable.

    A freshly created case's feed is still settling and its composer silently
    swallows input; a reload gives the clean, fully rendered feed that the
    comment flow depends on.
    """
    page.goto(case_url, wait_until="domcontentloaded")
    wait_for_network_idle(page, PAGE_RELOAD_TIMEOUT_MILLISECONDS)
    report_progress("  waiting for the case feed to be ready...")
    wait_until(
        page,
        lambda: page.get_by_placeholder("Write a comment...").count() > 0,
        ELEMENT_POLL_TIMEOUT_SECONDS,
    )
    page.wait_for_timeout(3_000)


def click_comment_submit_button(page) -> None:
    """Click the composer's 'Comment' submit button.

    The feed contains small 'Comment' action links (~18 px tall) as well as the
    real submit button (~32 px tall, class slds-button_brand). Selecting by
    brand class or height keeps this independent of the window size; an earlier
    x-coordinate rule worked in probes and failed in the real app.
    """
    brand_buttons = page.locator("button.slds-button_brand", has_text="Comment")
    for button_index in range(brand_buttons.count()):
        button = brand_buttons.nth(button_index)
        if button.is_visible() and button.is_enabled():
            button.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
            return

    comment_buttons = page.get_by_role("button", name="Comment", exact=True)
    tallest_button = None
    tallest_height = 0
    for button_index in range(comment_buttons.count()):
        button = comment_buttons.nth(button_index)
        try:
            bounding_box = button.bounding_box()
            is_clickable = button.is_visible() and button.is_enabled()
        except PlaywrightError:
            continue
        if bounding_box and is_clickable and bounding_box["height"] > tallest_height:
            tallest_button = button
            tallest_height = bounding_box["height"]

    # 24 px sits between the action links (~18 px) and the submit button (~32 px).
    if tallest_button is not None and tallest_height >= 24:
        tallest_button.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
        return

    comment_buttons.last.click(timeout=CLICK_TIMEOUT_MILLISECONDS)


def attach_uploaded_file_to_comment(page, display_name, report_progress) -> None:
    """Attach an already-uploaded file to the expanded comment composer.

    The file was uploaded by the wizard, so it is selected from "Owned by Me"
    rather than uploaded again - the dialog's upload input takes one file only.
    """
    # Only the expanded composer has a visible 'Attach file' button.
    try_strategies(
        [lambda: click_first_visible_button(page, "Attach file")],
        report_progress,
        "click 'Attach file'",
    )

    file_options = page.get_by_role("option")
    wait_until(page, lambda: file_options.count() >= 1, FILE_LIST_POLL_TIMEOUT_SECONDS)
    save_debug_screenshot(page, "05_select_file_dialog")

    # The filename span reports as not visible; the selectable element is the
    # wrapping <a role="option">.
    try_strategies(
        [lambda: page.get_by_role("option", name=display_name).first.click()],
        report_progress,
        f"select file '{display_name}'",
    )
    page.wait_for_timeout(700)

    add_button = page.get_by_role("button", name="Add", exact=True)
    wait_until(
        page,
        lambda: add_button.count() > 0 and add_button.first.is_enabled(),
        BUTTON_ENABLED_POLL_TIMEOUT_SECONDS,
    )
    try_strategies([lambda: add_button.first.click()], report_progress, "click Add")
    page.wait_for_timeout(3_000)
    save_debug_screenshot(page, "06_after_add")


def post_single_comment(page, comment_text, display_name, report_progress) -> None:
    """Post one feed comment, optionally with one attached file."""
    composer = page.get_by_placeholder("Write a comment...").first
    if not try_strategies([lambda: composer.click()], report_progress, "focus comment composer"):
        report_progress("  ! could not find the comment composer")
        return

    # Clicking expands the placeholder into a rich editor, after which the
    # placeholder locator no longer applies - type into whatever now has focus.
    page.wait_for_timeout(1_500)
    if comment_text:
        try_strategies([lambda: page.keyboard.type(comment_text)], report_progress, "type comment")
        page.wait_for_timeout(600)

    if display_name:
        attach_uploaded_file_to_comment(page, display_name, report_progress)

    try_strategies([lambda: click_comment_submit_button(page)], report_progress, "post comment")
    page.wait_for_timeout(3_000)
    report_progress("  comment posted" + (" with attachment." if display_name else "."))


def post_files_as_case_comments(page, file_paths, comment_text, report_progress) -> None:
    """Post the uploaded files as case feed comments, one file per comment.

    A Salesforce Chatter comment accepts exactly one attachment - selecting a
    second file replaces the first - so multiple files mean multiple comments.
    With no files, a single text comment is posted.
    """
    if not file_paths:
        # The default text promises attachments, so posting it with none is
        # misleading. Anything the user actually typed is still worth posting.
        if comment_text and comment_text.strip() != sc_settings.DEFAULT_FIRST_COMMENT:
            post_single_comment(page, comment_text, None, report_progress)
        else:
            report_progress(
                "No files attached, so the default first comment was skipped."
            )
        return

    case_url = page.url
    for file_index, file_path in enumerate(file_paths):
        if file_index > 0:
            # Start each comment from a clean, fully rendered feed.
            reload_case_page(page, case_url, report_progress)

        file_name = os.path.basename(file_path)
        # Salesforce lists uploaded files without their extension.
        display_name = os.path.splitext(file_name)[0]
        report_progress(
            f"Posting comment {file_index + 1}/{len(file_paths)} with '{file_name}'..."
        )
        post_single_comment(page, comment_text, display_name, report_progress)


# --------------------------------------------------------------------------- #
# Automation entry point (runs on a worker thread)
# --------------------------------------------------------------------------- #
def run_case_logging_automation(submission, report_progress, close_browser_event) -> None:
    """Drive the whole flow, then hold the browser open until asked to close."""
    os.makedirs(APPLICATION_PATHS.screenshot_directory, exist_ok=True)
    # Hidden by default. The environment toggle still forces headless for
    # troubleshooting, and the GUI tick box asks for a visible window.
    show_browser = bool(getattr(submission, "show_browser", False))
    if os.environ.get(HEADLESS_ENVIRONMENT_VARIABLE) == "1":
        show_browser = False
    file_paths = submission.existing_file_paths()

    with sync_playwright() as playwright:
        report_progress(
            "Launching Chromium..." if show_browser else "Launching Chromium (hidden)..."
        )
        browser = playwright.chromium.launch(
            headless=not show_browser, slow_mo=sc_portal.BROWSER_SLOW_MOTION_MILLISECONDS
        )
        # Visible: no_viewport keeps the real window size. Hidden: give it a
        # generous fixed viewport so everything the flow clicks is laid out and
        # on screen. Never select by coordinates either way.
        context = (
            browser.new_context(no_viewport=True)
            if show_browser
            else browser.new_context(viewport={"width": 1600, "height": 1200})
        )
        page = context.new_page()
        page.set_default_timeout(sc_portal.ACTION_TIMEOUT_MILLISECONDS)
        case_url = ""

        try:
            sc_portal.log_in_to_portal(
                page, submission.username, submission.password, report_progress
            )
            open_log_a_case_dialog(page, report_progress)
            fill_case_details_step(page, submission, report_progress)

            click_next_button(page, report_progress, "Clicking Next...")
            page.wait_for_timeout(2_000)
            advance_through_dialog(page, submission, report_progress)
            save_debug_screenshot(page, "01_after_final_next")

            complete_upload_wizard(page, file_paths, report_progress)
            page.wait_for_timeout(3_000)
            save_debug_screenshot(page, "02_after_wizard_finish")

            report_progress("Opening the newly-created case...")
            case_url = open_created_case(page, submission.subject, report_progress)
            reload_case_page(page, case_url, report_progress)
            page.wait_for_timeout(1_000)
            save_debug_screenshot(page, "03_case_page")

            post_files_as_case_comments(
                page, file_paths, submission.first_comment, report_progress
            )
            save_debug_screenshot(page, "04_after_attach")

            report_progress("")
            report_progress("Case logged successfully.")
            if case_url:
                report_progress(f"Case page: {case_url}")
            if show_browser:
                report_progress("Browser left open. Click 'Close Browser' when finished.")

        except Exception:  # noqa: BLE001 - report anything, keep the browser open
            report_progress("ERROR during automation:")
            report_progress(traceback.format_exc())
            try:
                error_screenshot_path = os.path.join(
                    APPLICATION_PATHS.screenshot_directory, "error.png"
                )
                page.screenshot(path=error_screenshot_path, full_page=True)
                report_progress(f"Screenshot saved: {error_screenshot_path}")
            except (OSError, PlaywrightError):
                pass

        # A visible run is left open so the case can be reviewed; a hidden one
        # has nothing to look at, so waiting would just hang on a button that
        # cannot be pressed.
        if show_browser:
            while not close_browser_event.is_set():
                close_browser_event.wait(0.5)

        report_progress("Closing browser...")
        try:
            browser.close()
        except PlaywrightError:
            pass


def main() -> None:
    sc_gui.CaseLoggerApplication(run_case_logging_automation, APPLICATION_PATHS).mainloop()


if __name__ == "__main__":
    main()
