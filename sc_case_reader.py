"""
Reading and replying to existing SimCorp support cases.
=======================================================

Drives the portal's case pages to pull a case and its discussion thread, and to
post a reply under a chosen post.

Facts about the portal that shape this module (see DEVELOPMENT_NOTES.md §15):

  - My Cases lives at /s/case-lists. The old /s/mycases URL renders
    "Invalid Page" and must not be used.
  - A case number is resolved through the global search route
    /s/search/All/Home/<case number>, which finds open AND closed cases. The
    list views only ever show a filtered subset.
  - The case feed is Aura, not LWC, so it sits in the light DOM and plain
    querySelectorAll works. This is the opposite of the Log-a-Case wizard.
  - The feed renders NEWEST FIRST, and its timestamps are not sortable (relative
    "1h ago" for recent items, absolute "13. July 2026 at 09.47" for old ones).
    Reading order therefore comes from reversing the DOM order, never from
    parsing timestamps.
  - Every feed item carries its own "Write a comment..." composer, so a reply
    must be scoped to one item or it lands on the wrong post.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

import sc_portal
from sc_portal import wait_for_network_idle, wait_until

PORTAL_ROOT = sc_portal.PORTAL_ROOT
CASE_LIST_URL = sc_portal.CASE_LIST_URL
SEARCH_URL_TEMPLATE = PORTAL_ROOT + "/s/search/All/Home/{term}"

CASE_NUMBER_PATTERN = re.compile(r"^\d{8}$")

#: The community case page exposes no Description field. Its only record tabs
#: are Feed and Related; Related holds Files, Articles, Error Logs and Contact
#: Roles. The "View more details" link on the creation post navigates to
#: /s/feed/<id> and shows only Subject, Case Number, Type, Priority, Contact
#: Name and Status. Clicking it also leaves the case page, which breaks the
#: reply composers, so it must not be clicked while reading. The opening
#: content of a case is in the thread instead.
PORTAL_HAS_NO_DESCRIPTION = (
    "The SimCorp portal does not publish the case Description on the case page, "
    "so it cannot be shown here. The conversation below is the full thread."
)

FEED_ITEM_SELECTOR = ".forceChatterFeedItem"
COMMENT_SELECTOR = ".forceChatterComment, .cuf-commentItem"
COMMENT_PLACEHOLDER = "Write a comment..."

ELEMENT_POLL_TIMEOUT_SECONDS = 30
CLICK_TIMEOUT_MILLISECONDS = 8_000


class CaseReaderError(Exception):
    """Raised when a case cannot be found, read, or replied to."""


@dataclass
class CaseImage:
    """A picture attached to a post - usually a screenshot.

    The portal renders these as <img> tags pointing at the Chatter rendition
    service, so the same URL that the browser displays is the one downloaded.
    """

    url: str
    version_id: str = ""
    width: int = 0
    height: int = 0
    #: Filled in once the file has been saved beside the cache.
    local_path: str = ""


@dataclass
class CaseComment:
    """One comment posted underneath a feed post."""

    author: str
    timestamp: str
    body: str


@dataclass
class CasePost:
    """One item in the case feed, with any comments hanging off it."""

    dom_index: int  # position in the DOM, needed to target the right composer
    author: str
    timestamp: str
    body: str
    kind: str  # "post", "record update" or "case created"
    attachments: list[str] = field(default_factory=list)
    images: list[CaseImage] = field(default_factory=list)
    comments: list[CaseComment] = field(default_factory=list)

    def is_system_entry(self) -> bool:
        return self.kind != "post"

    def summary_line(self) -> str:
        """A one-line label for the thread list in the GUI."""
        marker = {"post": "", "record update": "[update] ", "case created": "[created] "}
        first_line = (self.body or "").strip().splitlines()
        preview = first_line[0][:70] if first_line else "(no text)"
        comment_note = f"  ({len(self.comments)} comment{'s' if len(self.comments) != 1 else ''})" if self.comments else ""
        return f"{marker.get(self.kind, '')}{self.author} - {self.timestamp}: {preview}{comment_note}"


@dataclass
class CaseDetails:
    """Everything pulled for one case."""

    case_number: str
    url: str
    subject: str = ""
    status: str = ""
    priority: str = ""
    case_type: str = ""
    contact_name: str = ""
    #: The portal does not publish the case Description anywhere - see
    #: PORTAL_HAS_NO_DESCRIPTION below. Kept so callers have one shape.
    description: str = ""
    posts: list[CasePost] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Text handling
# --------------------------------------------------------------------------- #
def clean_portal_text(text: str) -> str:
    """Strip the invisible characters the portal embeds in @-mentions.

    Feed text contains zero-width spaces and non-breaking spaces, which break
    console output and look like stray whitespace when re-posted.
    """
    if not text:
        return ""
    without_zero_width = text.replace("​", "").replace("﻿", "")
    normalised = unicodedata.normalize("NFKC", without_zero_width).replace("\xa0", " ")
    lines = [line.rstrip() for line in normalised.splitlines()]
    # Collapse the runs of blank lines the rich-text editor leaves behind.
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()


# --------------------------------------------------------------------------- #
# Finding a case
# --------------------------------------------------------------------------- #
def normalise_case_number(text: str) -> str:
    """Accept '123456', '00123456' or 'Case #123456'; return the 8-digit number.

    A pasted case URL is refused with a message, because its id is not the
    case number.
    """
    text = (text or "").strip()
    if not text:
        raise CaseReaderError("Enter a case number.")
    url_match = re.search(r"/s/case/\w+/", text)
    if url_match:
        raise CaseReaderError("Enter the case number rather than its URL.")
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise CaseReaderError(f"'{text}' does not contain a case number.")
    return digits.zfill(8)


def find_case_url(page, case_number: str, report_progress) -> str:
    """Resolve a case number to its page URL via the global search route.

    Search is used rather than a list view because the list views are filtered
    (the default shows open cases only) and would silently miss closed cases.
    """
    report_progress(f"Searching for case {case_number}...")
    page.goto(SEARCH_URL_TEMPLATE.format(term=case_number), wait_until="domcontentloaded")
    wait_for_network_idle(page)

    def case_links():
        return page.evaluate(
            """() => [...document.querySelectorAll('a[href*="/s/case/"]')]
                  .map(a => ({text: (a.innerText||'').trim(), href: a.href}))"""
        )

    wait_until(page, lambda: len(case_links()) > 0, ELEMENT_POLL_TIMEOUT_SECONDS)
    links = case_links()

    for link in links:
        if link["text"] == case_number:
            report_progress(f"  found: {link['href']}")
            return link["href"]
    if links:
        report_progress(f"  using best match: {links[0]['href']}")
        return links[0]["href"]
    raise CaseReaderError(
        f"Case {case_number} was not found. Check the number, and that your "
        "portal account can see the case."
    )



# --------------------------------------------------------------------------- #
# Harvesting the case list
# --------------------------------------------------------------------------- #
#: Column headings of the portal's case list view. Read by name rather than by
#: position, because the view's columns can be reordered per user.
LIST_COLUMN_CASE_NUMBER = "Case Number"
LIST_COLUMN_SUBJECT = "Subject"
LIST_COLUMN_STATUS = "Status"
LIST_COLUMN_TYPE = "Type"
LIST_COLUMN_CONTACT = "Contact Name"
LIST_COLUMN_CREATED = "Created Date"
LIST_COLUMN_LAST_MODIFIED = "Last Modified Date"

_LIST_SCRIPT = """() => {
  const firstLine = (value) => (value || '').trim().split(String.fromCharCode(10))[0].trim();
  const table = [...document.querySelectorAll('table')]
      .find(candidate => candidate.querySelectorAll('tbody tr').length > 0);
  if (!table) return {headers: [], rows: []};

  const headers = [...table.querySelectorAll('thead th')].map(cell => {
    const label = cell.querySelector('span[title]') || cell.querySelector('.slds-truncate');
    const raw = label ? (label.getAttribute('title') || label.innerText) : cell.innerText;
    return firstLine(raw);
  });

  const rows = [...table.querySelectorAll('tbody tr')].map(row => {
    const cells = [...row.querySelectorAll('th,td')];
    const link = row.querySelector('a[href*="/s/case/"]');
    return {
      values: cells.map(cell => (cell.innerText || '').trim()),
      url: link ? link.href : ''
    };
  });
  return {headers, rows};
}"""

_ROW_COUNT_SCRIPT = """() => {
  const table = [...document.querySelectorAll('table')]
      .find(candidate => candidate.querySelectorAll('tbody tr').length > 0);
  return table ? table.querySelectorAll('tbody tr').length : 0;
}"""

_ITEM_LINE_SCRIPT = """() => {
  const body = document.body.innerText || '';
  const match = body.match(/[0-9,]+ items?.*/);
  return match ? match[0] : '';
}"""


def _load_every_list_row(page, report_progress) -> None:
    """Scroll until the list stops growing.

    Salesforce list views load further rows as they are scrolled. Stopping when
    the count holds still for two passes terminates cleanly on a short list,
    where nothing more ever loads.
    """
    previous_count = -1
    unchanged_passes = 0
    for _ in range(40):
        current_count = page.evaluate(_ROW_COUNT_SCRIPT)
        if current_count == previous_count:
            unchanged_passes += 1
            if unchanged_passes >= 2:
                break
        else:
            unchanged_passes = 0
            previous_count = current_count
        page.mouse.wheel(0, 6_000)
        page.wait_for_timeout(1_200)
    report_progress(f"  {page.evaluate(_ROW_COUNT_SCRIPT)} rows loaded")


#: A case list that has not rendered yet looks exactly like one with no rows, so
#: it is retried before being reported as a problem.
LIST_LOAD_ATTEMPTS = 3


def _case_list_has_rows(page) -> bool:
    return page.evaluate(_ROW_COUNT_SCRIPT) > 0


def _open_case_list(page, report_progress, attempt: int) -> bool:
    """Put the case list on screen and wait for its rows. True when they arrive."""
    if attempt == 1:
        page.goto(CASE_LIST_URL, wait_until="domcontentloaded")
    else:
        report_progress(f"  the list was empty; reloading (attempt {attempt})...")
        try:
            page.goto(CASE_LIST_URL, wait_until="domcontentloaded")
            page.reload(wait_until="domcontentloaded")
        except (PlaywrightError, PlaywrightTimeoutError) as error:
            report_progress(f"  (reload failed: {error})")
            return False

    wait_for_network_idle(page)
    if not wait_until(page, lambda: _case_list_has_rows(page), ELEMENT_POLL_TIMEOUT_SECONDS):
        return False
    page.wait_for_timeout(3_000)
    return True


def _describe_empty_case_list(page) -> str:
    """Work out WHY the list is empty, so the message is not a guess."""
    url = page.url or ""
    if not sc_portal.is_authenticated_page(url):
        return (
            "the portal signed us out while loading the case list "
            f"(ended at {url}). Try again, or use Close session and reload."
        )
    body = ""
    try:
        body = (page.evaluate("() => (document.body.innerText || '').slice(0, 400)") or "")
    except PlaywrightError:
        pass
    if "Invalid Page" in body:
        return f"the portal returned 'Invalid Page' for {CASE_LIST_URL}."
    if page.evaluate("() => document.querySelectorAll('table').length") == 0:
        return (
            "the case list never finished rendering. This is usually the portal "
            "being slow rather than anything being wrong; try again."
        )
    return "the case list rendered but held no rows."


def fetch_case_rows(page, report_progress):
    """Read every case in the portal's pinned list view.

    Returns `sc_case_cache.CaseRow` objects. Imported here rather than at module
    level so the reader does not depend on the cache.
    """
    import sc_case_cache

    report_progress("Opening the case list...")
    loaded = False
    for attempt in range(1, LIST_LOAD_ATTEMPTS + 1):
        if _open_case_list(page, report_progress, attempt):
            loaded = True
            break

    if not loaded:
        raise CaseReaderError(
            "The case list could not be read: " + _describe_empty_case_list(page)
        )

    item_line = page.evaluate(_ITEM_LINE_SCRIPT)
    if item_line:
        report_progress(f"  {clean_portal_text(item_line)}")
    _load_every_list_row(page, report_progress)

    table = page.evaluate(_LIST_SCRIPT)
    headers = [clean_portal_text(header) for header in table.get("headers", [])]

    def column_of(name):
        return headers.index(name) if name in headers else -1

    number_column = column_of(LIST_COLUMN_CASE_NUMBER)
    if number_column < 0:
        # Reaching here means rows rendered but the expected column is absent,
        # which really is a layout change rather than a slow page.
        raise CaseReaderError(
            "The case list rendered without a 'Case Number' column, so it could "
            f"not be read. Columns seen: {', '.join(h for h in headers if h) or '(none)'}."
        )

    columns = {
        "subject": column_of(LIST_COLUMN_SUBJECT),
        "status": column_of(LIST_COLUMN_STATUS),
        "case_type": column_of(LIST_COLUMN_TYPE),
        "contact_name": column_of(LIST_COLUMN_CONTACT),
        "created_date": column_of(LIST_COLUMN_CREATED),
        "last_modified": column_of(LIST_COLUMN_LAST_MODIFIED),
    }
    if columns["last_modified"] < 0:
        report_progress(
            "  ! no 'Last Modified Date' column: every case will be re-read each time."
        )

    def value_at(values, index):
        return clean_portal_text(values[index]) if 0 <= index < len(values) else ""

    case_rows = []
    for row in table.get("rows", []):
        values = row.get("values", [])
        case_number = value_at(values, number_column)
        if not CASE_NUMBER_PATTERN.match(case_number):
            continue
        case_rows.append(
            sc_case_cache.CaseRow(
                case_number=case_number,
                subject=value_at(values, columns["subject"]),
                status=value_at(values, columns["status"]) or "(no status)",
                case_type=value_at(values, columns["case_type"]),
                contact_name=value_at(values, columns["contact_name"]),
                url=row.get("url", ""),
                created_date=value_at(values, columns["created_date"]),
                last_modified=value_at(values, columns["last_modified"]),
            )
        )
    if not case_rows:
        raise CaseReaderError(
            "The case list was read but held no cases. " + _describe_empty_case_list(page)
        )
    report_progress(f"  {len(case_rows)} cases read from the list.")
    return case_rows


# --------------------------------------------------------------------------- #
# Reading a case
# --------------------------------------------------------------------------- #
_HIGHLIGHTS_SCRIPT = """() => {
    const panel = document.querySelector('.forceHighlightsPanel');
    return panel ? (panel.innerText || '').trim() : '';
}"""

_FEED_SCRIPT = """() => {
  const text = element => element ? (element.innerText || '').trim() : '';
  return [...document.querySelectorAll('.forceChatterFeedItem')].map((item, index) => {
    const header = item.querySelector('.forceChatterFeedItemHeader') || item;
    const headerText = text(header);
    const comments = [...item.querySelectorAll('.forceChatterComment, .cuf-commentItem')];
    const auxBodies = [...item.querySelectorAll('.forceChatterFeedAuxBody')];
    return {
      domIndex: index,
      headerText: headerText,
      author: text(item.querySelector('.cuf-entityLink, .forceChatterFeedItemHeader a')),
      timestamp: text(item.querySelector('.cuf-timestamp')),
      body: text(item.querySelector('.forceChatterMessageBody')),
      auxText: auxBodies.map(a => text(a)),
      fullText: text(item),
      images: [...item.querySelectorAll('img')]
        .filter(image => (image.src || '').indexOf('sfc/servlet.shepherd') >= 0)
        .map(image => ({
          url: image.src,
          width: image.naturalWidth || 0,
          height: image.naturalHeight || 0
        })),
      files: [...item.querySelectorAll('.cuf-contentAttachmentTitle')].map(title => {
        const holder = title.parentElement || title;
        const size = holder.querySelector('.cuf-contentAttachmentSize');
        return {
          name: (title.innerText || '').trim(),
          size: size ? (size.innerText || '').trim() : ''
        };
      }),
      comments: comments.map(comment => ({
        author: text(comment.querySelector('.cuf-commentNameLink, a')),
        timestamp: text(comment.querySelector('.cuf-commentAge, .cuf-timestamp')),
        body: text(comment.querySelector('.forceChatterMessageBody, .cuf-commentBody')) || text(comment)
      }))
    };
  });
}"""


def _classify(header_text: str) -> str:
    lowered = (header_text or "").lower()
    if "created this case" in lowered:
        return "case created"
    if "updated this record" in lowered or "changed" in lowered:
        return "record update"
    return "post"


#: Lines that mark the end of a post body inside a feed item's text.
_BODY_END_MARKERS = {
    "Like",
    "Liked",
    "Unlike",
    "Comment",
    "Expand Post",
    "Show more actions",
    "Download",
    "View more details",
}

#: Labels shown in the highlights panel, used to detect a missing value.
_HIGHLIGHT_LABELS = {
    "Case",
    "Case Number",
    "Type",
    "Priority",
    "Contact Name",
    "Status",
    "Follow",
    "Following",
}


def _body_from_full_text(full_text: str, header_text: str) -> str:
    """Recover a post body from the feed item's rendered text.

    The `.forceChatterMessageBody` element does not always exist by the time the
    feed settles, so the item text is the dependable source. Its shape is:

        <author>
        <timestamp>
        Actions for this Feed Item
        <body...>
        Like / Comment / <n> comment(s) / <n> view(s)
        <comments...>
        Write a comment...
    """
    lines = [line.strip() for line in clean_portal_text(full_text).splitlines()]
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if line.startswith("Actions for this Feed Item")
        ) + 1
    except StopIteration:
        return ""

    body_lines: list[str] = []
    for line in lines[start:]:
        if line in _BODY_END_MARKERS or line.startswith("Download file "):
            break
        if re.fullmatch(r"\d+ (comments?|views?)", line):
            break
        body_lines.append(line)
    return "\n".join(body_lines).strip()



def _strip_trailing_attachment_lines(body: str, attachments: list) -> str:
    """Drop the attachment caption that the item text repeats after the message.

    An attachment renders inside the same item text as the message, so the file
    type and name would otherwise appear twice: once at the end of the body and
    again in the attachment list.
    """
    if not body or not attachments:
        return body
    attachment_set = {line.strip() for line in attachments}
    lines = body.splitlines()
    while lines and (lines[-1].strip() in attachment_set or not lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def _author_from(raw_author: str, full_text: str) -> str:
    """Posts expose the author as a link; system entries only in the header text."""
    if raw_author:
        return raw_author
    first_line = (full_text or "").strip().splitlines()
    if not first_line:
        return "(unknown)"
    # e.g. "Jane Smith (Customer) created this case."
    return re.sub(r"\s+(created this case|updated this record)\.?$", "", first_line[0]).strip()


def _comment_body(raw_body: str, author: str) -> str:
    """Comment nodes often include the author, timestamp and action labels; keep
    only the message itself."""
    body = clean_portal_text(raw_body)
    noise = [
        "Actions for this Feed Item Comment",
        "Actions for this Feed Item",
        "Expand Post",
        "Unlike",
        "Liked",
        "Like",
    ]
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped in noise:
            continue
        if author and stripped.startswith(author):
            continue
        if re.fullmatch(r"\d+\s+(hours?|minutes?|days?|months?|years?)\s+ago", stripped):
            continue
        if re.fullmatch(r"\d+[hmd]\s+ago", stripped):
            continue
        lines.append(stripped)
    return "\n".join(lines).strip()


def read_case(page, case_number: str, case_url: str, report_progress) -> CaseDetails:
    """Open a case and pull its fields and full discussion thread."""
    report_progress("Opening the case...")
    page.goto(case_url, wait_until="domcontentloaded")
    wait_for_network_idle(page)
    wait_until(
        page,
        lambda: page.locator(FEED_ITEM_SELECTOR).count() > 0,
        ELEMENT_POLL_TIMEOUT_SECONDS,
    )
    page.wait_for_timeout(6_000)

    # An expired session redirects to /s/login/?ec=302, where there is no feed
    # and no subject. Reading on regardless produced a "case" with no posts and
    # the login URL, which then got cached as though it were real. Refuse it,
    # so the session layer can sign in again and retry.
    if not sc_portal.is_authenticated_page(page.url) or "/s/case/" not in page.url:
        raise CaseReaderError(
            f"the portal did not open case {case_number} - it ended at {page.url}. "
            "The session had probably expired."
        )

    details = CaseDetails(case_number=case_number, url=page.url)

    highlights = clean_portal_text(page.evaluate(_HIGHLIGHTS_SCRIPT))
    details.subject = _highlight_subject(highlights)
    for label, attribute in [
        ("Status", "status"),
        ("Priority", "priority"),
        ("Type", "case_type"),
        ("Contact Name", "contact_name"),
    ]:
        setattr(details, attribute, _highlight_field(highlights, label))

    report_progress("Reading the thread...")
    raw_items = page.evaluate(_FEED_SCRIPT)
    posts: list[CasePost] = []
    for raw in raw_items:
        kind = _classify(raw["headerText"] or raw["fullText"])
        author = _author_from(raw["author"], raw["fullText"])
        body = clean_portal_text(raw["body"])
        if not body:
            body = _body_from_full_text(raw["fullText"], raw["headerText"])
        if kind == "case created":
            # Its "body" is only the case number and the expand link.
            body = ""
        if not body and kind == "record update":
            body = clean_portal_text(" / ".join(raw["auxText"]))
        # Named files come from the attachment blocks; anything else in the aux
        # body is chrome ("Download", "Show more actions") and is dropped.
        attachments = [
            f"{attachment['name']}  ({attachment['size']})"
            if attachment.get("size")
            else attachment["name"]
            for attachment in raw.get("files", [])
            if attachment.get("name")
        ]
        images = [
            CaseImage(
                url=image["url"],
                version_id=_version_id_of(image["url"]),
                width=image.get("width", 0),
                height=image.get("height", 0),
            )
            for image in raw.get("images", [])
        ]
        body = _strip_trailing_attachment_lines(body, attachments)
        posts.append(
            CasePost(
                dom_index=raw["domIndex"],
                author=author,
                timestamp=clean_portal_text(raw["timestamp"]),
                body=body,
                kind=kind,
                attachments=attachments,
                images=images,
                comments=[
                    CaseComment(
                        author=clean_portal_text(comment["author"]),
                        timestamp=clean_portal_text(comment["timestamp"]),
                        body=_comment_body(comment["body"], clean_portal_text(comment["author"])),
                    )
                    for comment in raw["comments"]
                ],
            )
        )

    # The feed renders newest first; reverse so the thread reads top to bottom.
    posts.reverse()
    details.posts = posts
    report_progress(f"  {len(posts)} feed items read.")
    return details



def _version_id_of(url: str) -> str:
    """The Chatter version id inside a rendition URL, used as a stable filename."""
    match = re.search(r"versionId=(\w+)", url or "")
    return match.group(1) if match else ""


def download_post_images(page, details, directory: str, report_progress) -> int:
    """Save every image attached to the case, so the thread can show them.

    The rendition URLs need the portal session's cookies, so they are fetched
    through the browser context rather than with a fresh HTTP client. Files are
    named by their Chatter version id, which never changes, so a case that is
    re-read does not download its screenshots again.
    """
    # One folder per case, so a closed case's pictures can be dropped as a unit.
    directory = os.path.join(directory, details.case_number or "unfiled")
    os.makedirs(directory, exist_ok=True)
    downloaded = 0
    for post in details.posts:
        for image in post.images:
            file_name = f"{image.version_id or str(abs(hash(image.url)))}.img"
            file_path = os.path.join(directory, file_name)
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                image.local_path = file_path
                continue
            try:
                response = page.context.request.get(image.url, timeout=30_000)
                if not response.ok:
                    report_progress(f"  (image {image.version_id}: HTTP {response.status})")
                    continue
                with open(file_path, "wb") as image_file:
                    image_file.write(response.body())
            except (PlaywrightError, OSError) as error:
                report_progress(f"  (could not save an image: {error})")
                continue
            image.local_path = file_path
            downloaded += 1
    if downloaded:
        report_progress(f"  {downloaded} image(s) downloaded.")
    return downloaded


def _highlight_subject(highlights: str) -> str:
    """The highlights panel starts 'Case\\n<subject>\\nFollow\\n...'."""
    lines = [line for line in (highlights or "").splitlines() if line.strip()]
    if len(lines) >= 2 and lines[0].strip() == "Case":
        return lines[1].strip()
    return lines[1].strip() if len(lines) >= 2 else ""


def _highlight_field(highlights: str, label: str) -> str:
    """The panel is rendered as alternating label/value lines."""
    lines = [line.strip() for line in (highlights or "").splitlines() if line.strip()]
    for index, line in enumerate(lines[:-1]):
        if line == label:
            value = lines[index + 1]
            # An absent value means the next line is the following label.
            return "" if value in _HIGHLIGHT_LABELS else value
    return ""


# --------------------------------------------------------------------------- #
# Replying
# --------------------------------------------------------------------------- #
def post_reply(page, post: CasePost, reply_text: str, report_progress) -> None:
    """Post a comment underneath one specific feed item.

    Everything is scoped to that item's own element, because a case feed holds
    several composers and an unscoped locator lands the text on the wrong post.
    """
    if not reply_text.strip():
        raise CaseReaderError("The reply is empty.")

    item = page.locator(FEED_ITEM_SELECTOR).nth(post.dom_index)
    if not item.count():
        raise CaseReaderError("That post is no longer on the page; reload the case.")

    report_progress(f"Replying to {post.author}'s post...")
    composer = item.get_by_placeholder(COMMENT_PLACEHOLDER).first
    composer.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
    # Clicking swaps the placeholder for a rich editor, so type into the focused
    # element rather than filling the placeholder locator.
    page.wait_for_timeout(1_500)
    page.keyboard.type(reply_text)
    page.wait_for_timeout(800)

    _click_scoped_comment_submit(item)
    page.wait_for_timeout(4_000)
    report_progress("  reply posted.")


def _click_scoped_comment_submit(item) -> None:
    """Click the 'Comment' submit button belonging to one feed item.

    The brand-styled button is the submit; the small same-named action links are
    about 18px tall against the submit's 32px. Height and class are used rather
    than coordinates because the browser runs with no fixed viewport.
    """
    brand_buttons = item.locator("button.slds-button_brand", has_text="Comment")
    for index in range(brand_buttons.count()):
        button = brand_buttons.nth(index)
        if button.is_visible() and button.is_enabled():
            button.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
            return

    comment_buttons = item.get_by_role("button", name="Comment", exact=True)
    tallest_button = None
    tallest_height = 0
    for index in range(comment_buttons.count()):
        button = comment_buttons.nth(index)
        try:
            box = button.bounding_box()
            clickable = button.is_visible() and button.is_enabled()
        except PlaywrightError:
            continue
        if box and clickable and box["height"] > tallest_height:
            tallest_button = button
            tallest_height = box["height"]

    if tallest_button is not None and tallest_height >= 24:
        tallest_button.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
        return
    raise CaseReaderError("Could not find the Comment submit button for that post.")


# --------------------------------------------------------------------------- #
# Rendering the thread for display
# --------------------------------------------------------------------------- #
def render_thread(details: CaseDetails) -> str:
    """The whole case as readable text, oldest first."""
    lines = [
        f"Case {details.case_number}   {details.subject}",
        "=" * 78,
        f"Status   : {details.status}",
        f"Priority : {details.priority}",
        f"Type     : {details.case_type}",
        f"Contact  : {details.contact_name}",
        f"URL      : {details.url}",
        "",
    ]
    if details.description:
        lines += ["DESCRIPTION", "-" * 78, details.description, ""]
    else:
        lines += [PORTAL_HAS_NO_DESCRIPTION, ""]
    lines += [f"THREAD ({len(details.posts)} items, oldest first)", "=" * 78, ""]

    for position, post in enumerate(details.posts, start=1):
        marker = {"post": "", "record update": "  [record update]", "case created": "  [case created]"}
        lines.append(f"[{position}] {post.author}   {post.timestamp}{marker.get(post.kind, '')}")
        lines.append("-" * 78)
        lines.append(post.body or "(no text)")
        for attachment in post.attachments:
            lines.append(f"    attachment: {attachment}")
        for comment in post.comments:
            lines.append("")
            lines.append(f"    +-- {comment.author}   {comment.timestamp}")
            for comment_line in (comment.body or "(no text)").splitlines():
                lines.append(f"        {comment_line}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Posting with files, and closing a case
# --------------------------------------------------------------------------- #
#: The comment composer can take one attachment, but its Select File dialog
#: currently raises Salesforce's "Sorry to interrupt / CSS Error" and never
#: renders - verified headless and headed, scoped and page-wide. The feed's
#: top-level publisher has its own working control, labelled "Attach up to 10
#: files", so a reply carrying files is posted there instead of as a comment.
PUBLISHER_ATTACH_LABEL = "Attach up to 10 files"
PUBLISHER_SHARE_PLACEHOLDER = "Share an update..."
MAXIMUM_PUBLISHER_FILES = 10

CLOSE_CASE_LABEL = "CLOSE CASE"


def post_update_with_files(page, message, file_paths, report_progress) -> None:
    """Post a top-level update on the case, with files attached.

    Used instead of a comment when a reply carries files, because only the
    publisher can attach them. Up to ten files go on a single post, so unlike
    the one-file-per-comment rule there is no need to split them up.
    """
    file_paths = [path for path in (file_paths or []) if os.path.exists(path)]
    if len(file_paths) > MAXIMUM_PUBLISHER_FILES:
        raise CaseReaderError(
            f"The portal accepts at most {MAXIMUM_PUBLISHER_FILES} files on one "
            f"post; {len(file_paths)} were chosen."
        )

    report_progress("Opening the update box...")
    if not _open_publisher(page, report_progress):
        raise CaseReaderError("Could not open the case's update box.")

    if message:
        page.keyboard.type(message)
        page.wait_for_timeout(600)

    if file_paths:
        report_progress(f"Attaching {len(file_paths)} file(s)...")
        _attach_files_to_publisher(page, file_paths, report_progress)

    report_progress("Sharing the update...")
    share_button = page.locator("button.cuf-publisherShareButton")
    if not share_button.count():
        share_button = page.get_by_role("button", name="Share", exact=True)
    wait_until(page, lambda: share_button.first.is_enabled(), 15)
    share_button.first.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
    page.wait_for_timeout(5_000)
    report_progress("  update posted.")


def _open_publisher(page, report_progress) -> bool:
    """Expand the collapsed publisher into its rich editor."""
    strategies = [
        lambda: page.locator(
            f"button[title='{PUBLISHER_SHARE_PLACEHOLDER}']"
        ).first.click(timeout=CLICK_TIMEOUT_MILLISECONDS),
        lambda: page.get_by_placeholder(PUBLISHER_SHARE_PLACEHOLDER).first.click(
            timeout=CLICK_TIMEOUT_MILLISECONDS
        ),
    ]
    for strategy in strategies:
        try:
            strategy()
        except (PlaywrightError, PlaywrightTimeoutError):
            continue
        page.wait_for_timeout(3_000)
        return True
    report_progress("  ! could not expand the publisher")
    return False


def _attach_files_to_publisher(page, file_paths, report_progress) -> None:
    """Drive the publisher's attach control.

    The control is a button rather than a plain file input, so the file chooser
    it opens is captured with `expect_file_chooser` rather than by writing to an
    <input type=file> that does not exist until it is clicked.
    """
    attach_button = page.get_by_role("button", name=PUBLISHER_ATTACH_LABEL)
    if not attach_button.count():
        attach_button = page.locator(f"button[aria-label='{PUBLISHER_ATTACH_LABEL}']")
    if not attach_button.count():
        raise CaseReaderError("The publisher's attach button was not found.")

    try:
        with page.expect_file_chooser(timeout=15_000) as chooser_info:
            attach_button.first.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
        chooser_info.value.set_files(file_paths)
        page.wait_for_timeout(4_000)
        report_progress("  files handed to the publisher.")
        return
    except (PlaywrightError, PlaywrightTimeoutError) as error:
        report_progress(f"  (file chooser did not open: {error})")

    # Some builds expose a real input once the control has been clicked.
    file_input = page.locator("input[type=file]")
    if file_input.count():
        file_input.first.set_input_files(file_paths)
        page.wait_for_timeout(4_000)
        report_progress("  files attached through the file input.")
        return
    raise CaseReaderError("The portal did not offer anywhere to put the files.")


def close_case(page, report_progress) -> None:
    """Press the portal's CLOSE CASE button on the case currently open.

    The button sits on the case page itself, not in the highlights panel, and
    the portal may ask for confirmation afterwards.
    """
    report_progress("Looking for the Close Case button...")
    button = page.get_by_role("button", name=CLOSE_CASE_LABEL)
    if not button.count():
        button = page.locator("button", has_text=CLOSE_CASE_LABEL)
    if not button.count():
        raise CaseReaderError(
            "This case has no CLOSE CASE button. It may already be closed, or "
            "the portal may not allow you to close it."
        )

    visible = None
    for index in range(button.count()):
        candidate = button.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                visible = candidate
                break
        except PlaywrightError:
            continue
    if visible is None:
        raise CaseReaderError("The CLOSE CASE button is present but not usable.")

    visible.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
    page.wait_for_timeout(3_000)

    # A confirmation step appears on some cases; accept it when it does.
    for label in ("Confirm", "Yes", "OK", "Close Case"):
        confirm = page.get_by_role("button", name=label, exact=True)
        try:
            if confirm.count() and confirm.first.is_visible():
                confirm.first.click(timeout=CLICK_TIMEOUT_MILLISECONDS)
                report_progress(f"  confirmed with '{label}'.")
                break
        except (PlaywrightError, PlaywrightTimeoutError):
            continue
    page.wait_for_timeout(4_000)
    report_progress("  close requested.")
