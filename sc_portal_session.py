"""
A reusable, background portal browser session.
==============================================

The Respond tab loads a case and then may post a reply minutes later. Logging in
again for every action would cost half a minute each time, so the session keeps
one logged-in browser alive between actions.

Playwright's sync API is **thread-affine**: every call on a browser, context or
page must happen on the thread that created it. So this class owns a private
worker thread and everything reaches the browser as a task on a queue. Callers
submit a function of `page` and block on the reply, which keeps the awkward part
in one place instead of spread through the GUI.

The browser is headless: the Respond tab pulls case information quietly, with no
window appearing.
"""

from __future__ import annotations

import os
import queue
import threading
import time

import sc_portal


#: How long the session may sit unused before it is nudged. Salesforce expires
#: an idle session, and this app can go an hour between actions, so without a
#: nudge the next hourly check would find itself signed out every time.
KEEP_ALIVE_INTERVAL_SECONDS = 10 * 60

#: How long the keep-alive waits between checks of that idle clock.
KEEP_ALIVE_POLL_SECONDS = 30


class PortalSessionError(Exception):
    """Raised when the session cannot start, log in, or run a task."""


class _Task:
    """One unit of work for the browser thread, with somewhere to put the result."""

    def __init__(self, function):
        self.function = function
        self.completed = threading.Event()
        self.result = None
        self.error: BaseException | None = None


class PortalSession:
    """A logged-in headless browser, driven from any thread.

    Start it with `ensure_started()`, hand it work with `run(...)`, and close it
    with `close()`. It logs in once, on first use.
    """

    def __init__(self, username: str, password: str, report_progress, headless: bool = True):
        self._username = username
        self._password = password
        self._report_progress = report_progress
        self._headless = headless

        self._tasks: "queue.Queue[_Task | None]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._keep_alive_thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._last_activity = time.monotonic()
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------- #
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def ensure_started(self) -> None:
        """Start the browser and log in, once. Safe to call repeatedly."""
        with self._lock:
            if self.is_running():
                return
            self._ready.clear()
            self._start_error = None
            self._thread = threading.Thread(
                target=self._serve, name="portal-session", daemon=True
            )
            self._thread.start()

        self._ready.wait()
        if self._start_error is not None:
            raise PortalSessionError(f"could not start the portal session: {self._start_error}")

        if self._keep_alive_thread is None or not self._keep_alive_thread.is_alive():
            self._keep_alive_thread = threading.Thread(
                target=self._keep_alive, name="portal-keep-alive", daemon=True
            )
            self._keep_alive_thread.start()

    def close(self) -> None:
        """Shut the browser down and stop the worker thread."""
        with self._lock:
            if not self.is_running():
                self._thread = None
                return
            self._stopping.set()
            self._tasks.put(None)
            thread = self._thread
        thread.join(timeout=30)
        self._thread = None

    # -- running work ------------------------------------------------------ #
    def run(self, function, timeout_seconds: int = 300):
        """Run `function(page)` on the browser thread and return its result.

        Blocks the calling thread, so call it from a worker, never from the Tk
        event loop.
        """
        self.ensure_started()
        self._last_activity = time.monotonic()
        task = _Task(function)
        self._tasks.put(task)
        if not task.completed.wait(timeout=timeout_seconds):
            raise PortalSessionError("the portal session timed out.")
        if task.error is not None:
            raise task.error
        return task.result

    # -- staying signed in -------------------------------------------------- #
    def _execute(self, page, function):
        """Run one task, signing in again if the portal has expired the session.

        Salesforce redirects an expired session to /s/login/?ec=302. Detecting
        that and simply failing left every later action broken until someone
        closed the session by hand, so the sign-in is redone here and the task
        retried once.
        """
        try:
            return function(page)
        except BaseException:
            if not self._looks_signed_out(page):
                raise
            self._report_progress("  the portal signed us out; signing in again...")
            sc_portal.log_in_to_portal(
                page, self._username, self._password, self._report_progress
            )
            if self._looks_signed_out(page):
                raise
            self._report_progress("  signed back in; retrying...")
            return function(page)

    @staticmethod
    def _looks_signed_out(page) -> bool:
        try:
            return not sc_portal.is_authenticated_page(page.url)
        except Exception:  # noqa: BLE001 - a dead page counts as signed out
            return True

    def _keep_alive(self) -> None:
        """Nudge the portal when the session has been idle, so it does not expire.

        Only fires when nothing else has used the session recently, so it never
        competes with real work; the task queue serialises it either way.
        """
        while not self._stopping.wait(KEEP_ALIVE_POLL_SECONDS):
            if not self.is_running():
                return
            if time.monotonic() - self._last_activity < KEEP_ALIVE_INTERVAL_SECONDS:
                continue
            try:
                self.run(self._touch, timeout_seconds=120)
            except BaseException as error:  # noqa: BLE001 - never kill the timer
                self._report_progress(f"  (keep-alive could not reach the portal: {error})")

    def _touch(self, page):
        """One cheap page load, to keep the session from going idle."""
        page.goto(sc_portal.PORTAL_HOME_URL, wait_until="domcontentloaded")
        sc_portal.wait_for_network_idle(page, 15_000)
        return sc_portal.is_authenticated_page(page.url)

    # -- the browser thread ------------------------------------------------ #
    def _serve(self) -> None:
        """Own the Playwright objects for the whole life of the session."""
        # Imported here so the browser libraries load on this thread.
        from playwright.sync_api import sync_playwright

        playwright = browser = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=self._headless,
                slow_mo=sc_portal.BROWSER_SLOW_MOTION_MILLISECONDS,
            )
            context = browser.new_context(no_viewport=not self._headless)
            page = context.new_page()
            page.set_default_timeout(sc_portal.ACTION_TIMEOUT_MILLISECONDS)
            sc_portal.log_in_to_portal(
                page, self._username, self._password, self._report_progress
            )
        except BaseException as error:  # noqa: BLE001 - reported to the caller
            self._start_error = error
            self._ready.set()
            self._shutdown(browser, playwright)
            return

        self._ready.set()
        try:
            while True:
                task = self._tasks.get()
                if task is None:
                    break
                try:
                    task.result = self._execute(page, task.function)
                except BaseException as error:  # noqa: BLE001 - handed back
                    task.error = error
                finally:
                    self._last_activity = time.monotonic()
                    task.completed.set()
        finally:
            self._shutdown(browser, playwright)

    def _shutdown(self, browser, playwright) -> None:
        for close_call in (
            getattr(browser, "close", None),
            getattr(playwright, "stop", None),
        ):
            if close_call is None:
                continue
            try:
                close_call()
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass
        # Any task still queued must be released, or its caller waits forever.
        while True:
            try:
                pending = self._tasks.get_nowait()
            except queue.Empty:
                break
            if pending is not None:
                pending.error = PortalSessionError("the portal session closed.")
                pending.completed.set()


def browsers_path_is_configured() -> bool:
    """True when Playwright has been pointed at the bundled browser folder."""
    return bool(os.environ.get("PLAYWRIGHT_BROWSERS_PATH"))
