"""
Rendering a case thread into a Tk text widget.
==============================================

The thread is the part of the app people actually read, so it is laid out with
colour and indentation rather than printed as one block of plain text:

  - the case header carries the subject and its fields as labelled pairs
  - each post is numbered, with the author in bold and the timestamp greyed
  - system entries (case created, record updated) are dimmed, so the human
    conversation stands out from the audit trail
  - @mentions are picked out in blue, exactly as the portal shows them
  - attachments are listed in green under the post they belong to
  - comments are indented behind a gutter bar, under the post they answer

Everything is applied with Tk text tags. Tags are configured once per widget by
`configure_tags`, then `render_thread` writes the text and tags the ranges.
"""

from __future__ import annotations

import os
import re
import tkinter as tk
import tkinter.font as tkfont

try:
    from PIL import Image, ImageTk

    IMAGE_SUPPORT_AVAILABLE = True
except ImportError:  # Pillow missing: fall back to naming the attachment
    IMAGE_SUPPORT_AVAILABLE = False

#: The portal writes mentions as "@Firstname Surname (Organisation)". The
#: continuation words must be capitalised, otherwise the ordinary sentence after
#: a mention gets swallowed into it.
MENTION_PATTERN = re.compile(
    r"@[A-Za-z][\w'\-]*(?:\s+[A-Z][\w'\-.]*)*(?:\s*\([^)\n]{0,80}\))?"
)

#: Case numbers are eight digits and worth spotting inside a body of text.
CASE_NUMBER_PATTERN = re.compile(r"\b\d{8}\b")

URL_PATTERN = re.compile(r"https?://\S+")

# Colours chosen to stay legible on the default light Tk background.
COLOUR_HEADING = "#0f2b46"
COLOUR_LABEL = "#6b6b6b"
COLOUR_VALUE = "#1b1b1b"
COLOUR_TIMESTAMP = "#757575"
COLOUR_SYSTEM = "#8a8a8a"
COLOUR_MENTION = "#1a5fb4"
COLOUR_ATTACHMENT = "#0b6e4f"
COLOUR_RULE = "#b8c4ce"
COLOUR_NOTE = "#8a5a00"
COLOUR_COMMENT_GUTTER = "#3f7cac"
COLOUR_CASE_NUMBER = "#5a3e9c"
COLOUR_POST_RULE = "#dfe6ec"

POST_INDENT = 28
COMMENT_INDENT = 58

#: Screenshots are shown at readable size without letting one dominate the pane.
MAX_IMAGE_WIDTH = 620
MAX_IMAGE_HEIGHT = 460

RULE_WIDTH = 92


#: A proportional UI font, so the thread reads like the portal rather than like
#: a terminal. Tk text widgets default to a fixed-width font.
BODY_FONT_FAMILY = "Segoe UI"
BODY_FONT_SIZE = 10


def configure_tags(text_widget) -> None:
    """Set up every tag the renderer uses. Safe to call more than once."""
    family, size = BODY_FONT_FAMILY, BODY_FONT_SIZE
    if family not in tkfont.families():
        # Fall back to whatever the widget already uses, on a machine without it.
        try:
            base_font = tkfont.Font(font=text_widget.cget("font"))
            family = base_font.cget("family")
            size = abs(base_font.cget("size")) or BODY_FONT_SIZE
        except tk.TclError:
            family, size = "Arial", BODY_FONT_SIZE
    text_widget.configure(font=(family, size))

    bold = (family, size, "bold")
    italic = (family, size, "italic")
    heading = (family, size + 3, "bold")
    small = (family, max(size - 1, 7), "")
    small_bold = (family, max(size - 1, 7), "bold")

    text_widget.tag_configure("subject", font=heading, foreground=COLOUR_HEADING,
                              spacing1=2, spacing3=6)
    text_widget.tag_configure("case_number_heading", font=small_bold,
                              foreground=COLOUR_CASE_NUMBER)
    text_widget.tag_configure("field_label", font=small, foreground=COLOUR_LABEL)
    text_widget.tag_configure("field_value", font=small_bold, foreground=COLOUR_VALUE)
    text_widget.tag_configure("url", font=small, foreground=COLOUR_MENTION,
                              underline=True)
    text_widget.tag_configure("rule", foreground=COLOUR_RULE, spacing1=2, spacing3=4)
    text_widget.tag_configure(
        "post_rule", foreground=COLOUR_POST_RULE, font=(family, max(size - 3, 6)),
        spacing1=6, spacing3=8,
    )
    text_widget.tag_configure("note", font=italic, foreground=COLOUR_NOTE,
                              spacing1=4, spacing3=6, lmargin1=4, lmargin2=4)
    text_widget.tag_configure("section", font=bold, foreground=COLOUR_HEADING,
                              spacing1=10, spacing3=4)

    text_widget.tag_configure("post_number", font=small_bold,
                              foreground=COLOUR_COMMENT_GUTTER)
    text_widget.tag_configure("author", font=bold, foreground=COLOUR_VALUE)
    text_widget.tag_configure("author_system", font=italic, foreground=COLOUR_SYSTEM)
    text_widget.tag_configure("timestamp", font=small, foreground=COLOUR_TIMESTAMP)
    text_widget.tag_configure(
        "body", lmargin1=POST_INDENT, lmargin2=POST_INDENT, spacing1=1, spacing3=2
    )
    text_widget.tag_configure(
        "body_system",
        lmargin1=POST_INDENT,
        lmargin2=POST_INDENT,
        font=italic,
        foreground=COLOUR_SYSTEM,
    )
    text_widget.tag_configure(
        "attachment",
        lmargin1=POST_INDENT,
        lmargin2=POST_INDENT + 12,
        foreground=COLOUR_ATTACHMENT,
        font=small,
        spacing1=1,
    )
    text_widget.tag_configure(
        "comment_header",
        lmargin1=COMMENT_INDENT - 18,
        lmargin2=COMMENT_INDENT,
        font=small_bold,
        spacing1=5,
    )
    text_widget.tag_configure(
        "comment_body",
        lmargin1=COMMENT_INDENT,
        lmargin2=COMMENT_INDENT,
        font=small,
        spacing3=2,
    )
    text_widget.tag_configure("gutter", foreground=COLOUR_COMMENT_GUTTER, font=small_bold)
    text_widget.tag_configure(
        "image_hint",
        lmargin1=POST_INDENT + 8,
        lmargin2=POST_INDENT + 8,
        foreground=COLOUR_TIMESTAMP,
        font=(family, max(size - 2, 7), "italic"),
        spacing3=6,
    )

    # Inline tags must win over the block tags they sit inside.
    text_widget.tag_configure("mention", foreground=COLOUR_MENTION, font=small_bold)
    text_widget.tag_configure("case_reference", foreground=COLOUR_CASE_NUMBER)
    text_widget.tag_raise("mention")
    text_widget.tag_raise("case_reference")
    text_widget.tag_raise("url")


def render_thread(text_widget, details, portal_note: str = "") -> None:
    """Replace the widget's contents with the rendered case."""
    text_widget.config(state="normal")
    text_widget.delete("1.0", "end")
    # Tk keeps no reference to embedded images, so they must be held here or
    # they are garbage collected and the thread shows empty boxes.
    text_widget._embedded_images = []  # noqa: SLF001 - deliberate lifetime pin
    # Maps an embedded image's Tk name to its file, so a double-click can be
    # resolved back to something openable.
    text_widget._image_files = {}  # noqa: SLF001

    _write_header(text_widget, details, portal_note)
    _write_posts(text_widget, details)

    text_widget.config(state="disabled")
    text_widget.see("1.0")


# --------------------------------------------------------------------------- #
# Writing helpers
# --------------------------------------------------------------------------- #
def _append(text_widget, content: str, *tags) -> None:
    start = text_widget.index("end-1c")
    text_widget.insert("end", content)
    for tag in tags:
        if tag:
            text_widget.tag_add(tag, start, text_widget.index("end-1c"))


def _append_post_rule(text_widget) -> None:
    """A faint divider between posts, so the thread reads as separate entries."""
    _append(text_widget, "─" * (RULE_WIDTH + 20) + chr(10), "post_rule")


def _append_rule(text_widget) -> None:
    _append(text_widget, "─" * RULE_WIDTH + "\n", "rule")


def _append_inline(text_widget, content: str, block_tag: str) -> None:
    """Write body text, colouring mentions, case numbers and links inside it."""
    start_index = text_widget.index("end-1c")
    text_widget.insert("end", content)
    end_index = text_widget.index("end-1c")
    if block_tag:
        text_widget.tag_add(block_tag, start_index, end_index)

    for pattern, tag in (
        (MENTION_PATTERN, "mention"),
        (URL_PATTERN, "url"),
        (CASE_NUMBER_PATTERN, "case_reference"),
    ):
        for match in pattern.finditer(content):
            match_start = f"{start_index}+{match.start()}c"
            match_end = f"{start_index}+{match.end()}c"
            text_widget.tag_add(tag, match_start, match_end)


def _write_header(text_widget, details, portal_note: str) -> None:
    _append(text_widget, f"Case {details.case_number}\n", "case_number_heading")
    _append(text_widget, f"{details.subject or '(no subject)'}\n", "subject")

    fields = [
        ("Status", details.status),
        ("Priority", details.priority),
        ("Type", details.case_type),
        ("Contact", details.contact_name),
    ]
    for position, (label, value) in enumerate(fields):
        if position:
            _append(text_widget, "     ", "field_label")
        _append(text_widget, f"{label}  ", "field_label")
        _append(text_widget, value or "-", "field_value")
    _append(text_widget, "\n")

    if details.url:
        _append(text_widget, f"{details.url}\n", "url")
    _append_rule(text_widget)

    if details.description:
        _append(text_widget, "Description\n", "section")
        _append_inline(text_widget, details.description + "\n", "body")
    elif portal_note:
        _append(text_widget, portal_note + "\n", "note")


def _write_posts(text_widget, details) -> None:
    post_count = len(details.posts)
    _append(
        text_widget,
        f"Thread: {post_count} item{'s' if post_count != 1 else ''}, oldest first\n",
        "section",
    )
    _append_rule(text_widget)

    for position, post in enumerate(details.posts, start=1):
        if position > 1:
            _append_post_rule(text_widget)
        _write_post(text_widget, position, post)


def _write_post(text_widget, position: int, post) -> None:
    is_system = post.kind != "post"

    _append(text_widget, f"{position:>3}  ", "post_number")
    _append(text_widget, post.author or "(unknown)", "author_system" if is_system else "author")
    if is_system:
        label = "created this case" if post.kind == "case created" else "updated this record"
        _append(text_widget, f"  {label}", "author_system")
    _append(text_widget, f"    {post.timestamp}\n", "timestamp")

    body = (post.body or "").strip()
    if body:
        _append_inline(text_widget, body + "\n", "body_system" if is_system else "body")
    elif not is_system:
        _append(text_widget, "(no text)\n", "body_system")

    for attachment in post.attachments:
        _append(text_widget, f"attachment   {attachment}\n", "attachment")

    for image in post.images:
        _write_image(text_widget, image)

    for comment in post.comments:
        _write_comment(text_widget, comment)

    _append(text_widget, "\n")



def _write_image(text_widget, image) -> None:
    """Show a screenshot inline, or name it if it cannot be displayed."""
    if not image.local_path or not os.path.exists(image.local_path):
        _append(text_widget, "image   (not downloaded)" + "\n", "attachment")
        return
    if not IMAGE_SUPPORT_AVAILABLE:
        _append(
            text_widget,
            f"image   {os.path.basename(image.local_path)}" + "\n",
            "attachment",
        )
        return

    try:
        with Image.open(image.local_path) as picture:
            picture.load()
            rendered = picture.convert("RGB")
        rendered.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT), Image.LANCZOS)
        photo = ImageTk.PhotoImage(rendered)
    except (OSError, ValueError) as error:
        _append(
            text_widget,
            f"image   (could not be shown: {error})" + "\n",
            "attachment",
        )
        return

    text_widget._embedded_images.append(photo)  # noqa: SLF001 - lifetime pin
    _append(text_widget, " " * 4, "body")
    image_name = text_widget.image_create("end", image=photo, padx=6, pady=6)
    registry = getattr(text_widget, "_image_files", None)
    if registry is not None:
        registry[image_name] = image.local_path
    _append(text_widget, "\n", "body")
    _append(
        text_widget,
        "double-click the picture to open it full size" + "\n",
        "image_hint",
    )


def _write_comment(text_widget, comment) -> None:
    _append(text_widget, "│ ", "gutter")
    _append(text_widget, comment.author or "(unknown)", "comment_header")
    _append(text_widget, f"    {comment.timestamp}\n", "timestamp")
    body = (comment.body or "").strip() or "(no text)"
    _append_inline(text_widget, body + "\n", "comment_body")
