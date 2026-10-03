"""
A small pool of portal browsers for reading many cases at once.
===============================================================

Reading a case takes roughly half a minute, nearly all of it waiting for
Salesforce to render. Reading them one at a time makes the first load of a full
case list take many minutes, so a bulk read is spread across a few browsers.

Two things make this cheap:

  - **One login for the whole pool.** The primary session exports its cookies
    with `context.storage_state()`, and each worker starts a context from that
    file. Workers never see the login page. Measured: 26.6 s for the one login,
    then workers start in seconds.
  - **Playwright, not a second browser stack.** Its sync API is thread-affine, so
    each worker owns its own Playwright instance, browser and page on its own
    thread, and never touches another worker's objects.

Measured on five cases: 49.9 s of wall clock for 209.3 s of work, with no
failures.

Workers are started **staggered** rather than all at once, so the portal sees a
gentle ramp instead of a burst. Once the backlog is read, ongoing delta refreshes
go back through the single shared session - one changed case does not justify a
pool.
"""

from __future__ import annotations

import queue
import threading
import time

import sc_portal

#: Browsers to run at once. Five is comfortably below anything the portal
#: objects to, and the gain flattens out beyond it because each case is mostly
#: waiting on Salesforce rather than on us.
DEFAULT_WORKER_COUNT = 5

#: Seconds between starting one worker and the next.
DEFAULT_STAGGER_SECONDS = 5

#: A pool only pays for itself on a real backlog; below this, use one session.
MINIMUM_CASES_FOR_POOL = 3


class PoolResult:
    """What one worker made of one case."""

    def __init__(self, case_number: str, details=None, error: BaseException | None = None):
        self.case_number = case_number
        self.details = details
        self.error = error

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.details is not None


class PortalPool:
    """Reads a list of cases across several browsers that share one login."""

    def __init__(
        self,
        storage_state_path: str,
        report_progress,
        worker_count: int = DEFAULT_WORKER_COUNT,
        stagger_seconds: float = DEFAULT_STAGGER_SECONDS,
    ):
        self._storage_state_path = storage_state_path
        self._report_progress = report_progress
        self._worker_count = max(1, worker_count)
        self._stagger_seconds = max(0.0, stagger_seconds)
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """Ask every worker to stop after the case it is currently reading."""
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def read_cases(self, case_numbers, read_one, on_result=None) -> list[PoolResult]:
        """Read every case, returning the results in completion order.

        `read_one(page, case_number)` does the actual work on a worker's page.
        `on_result(result)` is called from a worker thread as each case lands, so
        anything it touches must be thread-safe.
        """
        pending: "queue.Queue[str]" = queue.Queue()
        for case_number in case_numbers:
            pending.put(case_number)

        results: list[PoolResult] = []
        results_lock = threading.Lock()
        worker_count = min(self._worker_count, len(case_numbers))

        self._report_progress(
            f"  starting {worker_count} browser(s), one every "
            f"{self._stagger_seconds:g}s, sharing one login"
        )

        workers = [
            threading.Thread(
                target=self._serve,
                args=(index, pending, read_one, on_result, results, results_lock),
                name=f"portal-pool-{index}",
                daemon=True,
            )
            for index in range(worker_count)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        return results

    # -- one worker -------------------------------------------------------- #
    def _serve(self, index, pending, read_one, on_result, results, results_lock) -> None:
        """Own one browser for the life of the batch and drain the queue."""
        from playwright.sync_api import sync_playwright

        # Ramp up rather than opening every browser at the same instant.
        delay = index * self._stagger_seconds
        if delay and not self._cancel.wait(delay):
            pass
        if self._cancel.is_set():
            return

        playwright = browser = None
        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(
                headless=True, slow_mo=sc_portal.BROWSER_SLOW_MOTION_MILLISECONDS
            )
            # The stored cookies stand in for logging in again.
            context = browser.new_context(storage_state=self._storage_state_path)
            page = context.new_page()
            page.set_default_timeout(sc_portal.ACTION_TIMEOUT_MILLISECONDS)
        except BaseException as error:  # noqa: BLE001 - reported, pool carries on
            self._report_progress(f"  ! browser {index + 1} could not start: {error}")
            self._shutdown(browser, playwright)
            return

        try:
            while not self._cancel.is_set():
                try:
                    case_number = pending.get_nowait()
                except queue.Empty:
                    break
                started = time.monotonic()
                try:
                    details = read_one(page, case_number)
                    result = PoolResult(case_number, details=details)
                except BaseException as error:  # noqa: BLE001 - one case must not stop the batch
                    result = PoolResult(case_number, error=error)
                    self._report_progress(f"  ! {case_number} failed: {error}")

                took = time.monotonic() - started
                if result.succeeded:
                    self._report_progress(f"  {case_number} read in {took:.0f}s")
                with results_lock:
                    results.append(result)
                if on_result is not None:
                    try:
                        on_result(result)
                    except Exception as error:  # noqa: BLE001 - a bad callback must not kill a worker
                        self._report_progress(f"  ! handling {case_number}: {error}")
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
