"""
On-disk cache of cases and their threads.
=========================================

The Respond tab is meant to be opened many times a day, so re-reading every case
from the portal would be unusable: a case load costs about half a minute. This
module keeps what has already been read in a small SQLite database beside the
app, and works out what actually needs re-reading.

The delta signal is the **Last Modified Date** column of the portal's case list
view (e.g. "18.08.2026 15.04"). Refreshing the list is one page load and yields
that value for every case at once. A cached thread is stale exactly when the
case's Last Modified Date has moved on from the one recorded when the thread was
read; anything else is served straight from the cache.

SQLite rather than a JSON file: writes are atomic and a half-written file cannot
lose the whole cache, which matters for a tool meant to run unattended for a
long time. It is in the standard library, so nothing is added to the build.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sc_case_reader

CACHE_FILE_NAME = "cases.sqlite3"
ATTACHMENT_DIRECTORY_NAME = "attachments"
SCHEMA_VERSION = 1


@dataclass
class CaseRow:
    """One row of the portal's case list view."""

    case_number: str
    subject: str = ""
    status: str = ""
    case_type: str = ""
    contact_name: str = ""
    url: str = ""
    created_date: str = ""
    last_modified: str = ""
    #: Set by the cache: True when no cached thread matches this last_modified.
    is_stale: bool = True
    #: Set by the cache: True when a thread has never been read for this case.
    is_new: bool = True


@dataclass
class RefreshOutcome:
    """What a list refresh changed, so the user can be told plainly."""

    total: int = 0
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    #: Cases that were listed before and are not any more - closed, or moved
    #: out of the view. Their downloaded screenshots are no longer worth
    #: keeping on disk.
    removed: list[str] = field(default_factory=list)
    unchanged: int = 0

    def summary(self) -> str:
        parts = [f"{self.total} case{'s' if self.total != 1 else ''}"]
        if self.added:
            parts.append(f"{len(self.added)} new")
        if self.updated:
            parts.append(f"{len(self.updated)} updated")
        if self.removed:
            parts.append(f"{len(self.removed)} closed or gone")
        parts.append(f"{self.unchanged} unchanged")
        return ", ".join(parts)


def utc_now_text() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class CaseCache:
    """Stores the case list and the threads already read.

    Every method opens and closes its own connection. The database is tiny and
    the calls are infrequent, so this avoids sharing a connection across the
    GUI thread and the browser thread - sqlite3 connections are not safe to use
    from several threads at once.
    """

    def __init__(self, cache_path: str):
        self.cache_path = cache_path
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        self.attachment_directory = os.path.join(
            os.path.dirname(cache_path), ATTACHMENT_DIRECTORY_NAME
        )
        # Several pool workers finish at once, so writes are serialised here as
        # well as by SQLite's own locking. Reads do not need it.
        self._write_lock = threading.Lock()
        self._create_schema()

    # -- schema ------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.cache_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _create_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    case_number   TEXT PRIMARY KEY,
                    subject       TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL DEFAULT '',
                    case_type     TEXT NOT NULL DEFAULT '',
                    contact_name  TEXT NOT NULL DEFAULT '',
                    url           TEXT NOT NULL DEFAULT '',
                    created_date  TEXT NOT NULL DEFAULT '',
                    last_modified TEXT NOT NULL DEFAULT '',
                    listed_at     TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS threads (
                    case_number   TEXT PRIMARY KEY,
                    last_modified TEXT NOT NULL DEFAULT '',
                    fetched_at    TEXT NOT NULL DEFAULT '',
                    payload       TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- meta -------------------------------------------------------------- #
    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value)
            )

    @property
    def last_list_refresh(self) -> str:
        return self.get_meta("last_list_refresh")

    # -- the case list ----------------------------------------------------- #
    def store_case_rows(self, rows: list[CaseRow]) -> RefreshOutcome:
        """Replace the stored list, reporting what changed since last time.

        Rows absent from the new list are reported in `removed` so the caller
        can drop their downloaded screenshots; the case itself stops being
        offered.
        """
        outcome = RefreshOutcome(total=len(rows))
        listed_at = utc_now_text()

        with self._write_lock, self._connect() as connection:
            existing = {
                row["case_number"]: row["last_modified"]
                for row in connection.execute("SELECT case_number, last_modified FROM cases")
            }
            for row in rows:
                previous = existing.get(row.case_number)
                if previous is None:
                    outcome.added.append(row.case_number)
                elif previous != row.last_modified:
                    outcome.updated.append(row.case_number)
                else:
                    outcome.unchanged += 1

                connection.execute(
                    """INSERT INTO cases(case_number, subject, status, case_type,
                                         contact_name, url, created_date, last_modified, listed_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(case_number) DO UPDATE SET
                            subject=excluded.subject, status=excluded.status,
                            case_type=excluded.case_type, contact_name=excluded.contact_name,
                            url=excluded.url, created_date=excluded.created_date,
                            last_modified=excluded.last_modified, listed_at=excluded.listed_at""",
                    (
                        row.case_number,
                        row.subject,
                        row.status,
                        row.case_type,
                        row.contact_name,
                        row.url,
                        row.created_date,
                        row.last_modified,
                        listed_at,
                    ),
                )

            listed_numbers = {row.case_number for row in rows}
            outcome.removed = sorted(set(existing) - listed_numbers)
            for case_number in outcome.removed:
                connection.execute("DELETE FROM cases WHERE case_number = ?", (case_number,))

        self.set_meta("last_list_refresh", listed_at)
        return outcome

    def get_case_rows(self) -> list[CaseRow]:
        """Every listed case, each flagged with whether its thread is current."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT c.*, t.last_modified AS thread_last_modified
                   FROM cases c LEFT JOIN threads t ON t.case_number = c.case_number
                   ORDER BY c.status, c.last_modified DESC"""
            ).fetchall()

        case_rows = []
        for row in rows:
            thread_last_modified = row["thread_last_modified"]
            case_rows.append(
                CaseRow(
                    case_number=row["case_number"],
                    subject=row["subject"],
                    status=row["status"],
                    case_type=row["case_type"],
                    contact_name=row["contact_name"],
                    url=row["url"],
                    created_date=row["created_date"],
                    last_modified=row["last_modified"],
                    is_new=thread_last_modified is None,
                    is_stale=thread_last_modified != row["last_modified"],
                )
            )
        return case_rows

    def get_case_row(self, case_number: str) -> CaseRow | None:
        for row in self.get_case_rows():
            if row.case_number == case_number:
                return row
        return None

    def stale_case_numbers(self) -> list[str]:
        """Listed cases whose cached thread is missing or out of date."""
        return [row.case_number for row in self.get_case_rows() if row.is_stale]

    # -- threads ------------------------------------------------------------ #
    def get_thread(self, case_number: str, last_modified: str = "") -> sc_case_reader.CaseDetails | None:
        """The cached thread, or None when absent or superseded.

        Passing the list's current `last_modified` makes this return None for a
        case that has moved on, which is what keeps stale text off the screen.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT last_modified, payload FROM threads WHERE case_number = ?",
                (case_number,),
            ).fetchone()
        if row is None:
            return None
        if last_modified and row["last_modified"] != last_modified:
            return None
        try:
            return thread_from_payload(json.loads(row["payload"]))
        except (ValueError, KeyError, TypeError):
            # A cache entry written by an older version is simply re-read.
            return None

    def thread_fetched_at(self, case_number: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT fetched_at FROM threads WHERE case_number = ?", (case_number,)
            ).fetchone()
        return row["fetched_at"] if row else ""

    def store_thread(
        self, details: sc_case_reader.CaseDetails, last_modified: str
    ) -> None:
        """Save a thread, unless it is obviously the result of a failed read.

        A case with neither posts nor a subject is what a login page looks like
        once it has been through the reader. Caching that would mark the case
        as current and hide the real thread until something forced a re-read.
        """
        if not details.posts and not details.subject:
            raise ValueError(
                f"refusing to cache an empty thread for {details.case_number}; "
                "the read probably landed on the login page"
            )
        payload = json.dumps(thread_to_payload(details))
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO threads(case_number, last_modified, fetched_at, payload)
                   VALUES(?,?,?,?)
                   ON CONFLICT(case_number) DO UPDATE SET
                        last_modified=excluded.last_modified,
                        fetched_at=excluded.fetched_at,
                        payload=excluded.payload""",
                (details.case_number, last_modified, utc_now_text(), payload),
            )

    def attachment_directory_for(self, case_number: str) -> str:
        """Where one case's screenshots live.

        A folder per case means a closed case's pictures can be dropped without
        working out which file belonged to which thread.
        """
        return os.path.join(self.attachment_directory, case_number)

    def purge_case_attachments(self, case_number: str) -> tuple[int, int]:
        """Delete a case's downloaded screenshots and forget its thread.

        Returns (files removed, bytes freed). The thread is dropped as well, so
        that if the case comes back the pictures are downloaded again rather
        than the cache pointing at files that are no longer there.
        """
        removed_files = 0
        freed_bytes = 0

        # Files referenced by the cached thread. This also catches anything
        # saved by an earlier version, which kept every case's images together
        # in one folder.
        details = self.get_thread(case_number)
        if details is not None:
            for post in details.posts:
                for image in post.images:
                    path = image.local_path
                    if path and os.path.exists(path):
                        try:
                            freed_bytes += os.path.getsize(path)
                            os.remove(path)
                            removed_files += 1
                        except OSError:
                            pass

        case_directory = self.attachment_directory_for(case_number)
        if os.path.isdir(case_directory):
            for entry in os.listdir(case_directory):
                path = os.path.join(case_directory, entry)
                try:
                    if os.path.isfile(path):
                        freed_bytes += os.path.getsize(path)
                        os.remove(path)
                        removed_files += 1
                except OSError:
                    pass
            try:
                os.rmdir(case_directory)
            except OSError:
                pass

        with self._write_lock, self._connect() as connection:
            connection.execute("DELETE FROM threads WHERE case_number = ?", (case_number,))
        return removed_files, freed_bytes

    def purge_unusable_threads(self) -> list[str]:
        """Drop cached threads that are not really threads, and orphans.

        Two kinds get swept:

        - **Empty ones.** Before `store_thread` refused them, a read that landed
          on the login page was cached as a case with no posts and no subject.
          Those look current, so nothing would ever re-read them.
        - **Orphans.** Threads for cases that are no longer listed, left behind
          by versions that kept them when a case closed.

        Returns the case numbers swept, so the caller can say what it did.
        """
        swept = []
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT t.case_number, t.payload, c.case_number AS listed
                   FROM threads t LEFT JOIN cases c ON c.case_number = t.case_number"""
            ).fetchall()

        for row in rows:
            case_number = row["case_number"]
            if row["listed"] is None:
                swept.append(case_number)
                continue
            try:
                payload = json.loads(row["payload"])
            except ValueError:
                swept.append(case_number)
                continue
            if not payload.get("posts") and not payload.get("subject"):
                swept.append(case_number)

        for case_number in swept:
            self.purge_case_attachments(case_number)
        return swept

    def attachment_usage(self) -> tuple[int, int]:
        """(files, bytes) currently held under the attachment folder."""
        files = 0
        total = 0
        for root, _folders, names in os.walk(self.attachment_directory):
            for name in names:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                    files += 1
                except OSError:
                    pass
        return files, total

    def clear(self) -> None:
        with self._connect() as connection:
            connection.executescript("DELETE FROM cases; DELETE FROM threads; DELETE FROM meta;")
        self._create_schema()

    def statistics(self) -> tuple[int, int]:
        """(cases listed, threads cached)."""
        with self._connect() as connection:
            cases = connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
            threads = connection.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        return cases, threads


# --------------------------------------------------------------------------- #
# Turning a thread into something storable and back
# --------------------------------------------------------------------------- #
def thread_to_payload(details: sc_case_reader.CaseDetails) -> dict:
    return {
        "case_number": details.case_number,
        "url": details.url,
        "subject": details.subject,
        "status": details.status,
        "priority": details.priority,
        "case_type": details.case_type,
        "contact_name": details.contact_name,
        "description": details.description,
        "posts": [
            {
                "dom_index": post.dom_index,
                "author": post.author,
                "timestamp": post.timestamp,
                "body": post.body,
                "kind": post.kind,
                "attachments": list(post.attachments),
                "images": [
                    {
                        "url": image.url,
                        "version_id": image.version_id,
                        "width": image.width,
                        "height": image.height,
                        "local_path": image.local_path,
                    }
                    for image in post.images
                ],
                "comments": [
                    {
                        "author": comment.author,
                        "timestamp": comment.timestamp,
                        "body": comment.body,
                    }
                    for comment in post.comments
                ],
            }
            for post in details.posts
        ],
    }


def thread_from_payload(payload: dict) -> sc_case_reader.CaseDetails:
    return sc_case_reader.CaseDetails(
        case_number=payload["case_number"],
        url=payload.get("url", ""),
        subject=payload.get("subject", ""),
        status=payload.get("status", ""),
        priority=payload.get("priority", ""),
        case_type=payload.get("case_type", ""),
        contact_name=payload.get("contact_name", ""),
        description=payload.get("description", ""),
        posts=[
            sc_case_reader.CasePost(
                dom_index=post["dom_index"],
                author=post.get("author", ""),
                timestamp=post.get("timestamp", ""),
                body=post.get("body", ""),
                kind=post.get("kind", "post"),
                attachments=list(post.get("attachments", [])),
                images=[
                    sc_case_reader.CaseImage(
                        url=image.get("url", ""),
                        version_id=image.get("version_id", ""),
                        width=image.get("width", 0),
                        height=image.get("height", 0),
                        local_path=image.get("local_path", ""),
                    )
                    for image in post.get("images", [])
                ],
                comments=[
                    sc_case_reader.CaseComment(
                        author=comment.get("author", ""),
                        timestamp=comment.get("timestamp", ""),
                        body=comment.get("body", ""),
                    )
                    for comment in post.get("comments", [])
                ],
            )
            for post in payload.get("posts", [])
        ],
    )
