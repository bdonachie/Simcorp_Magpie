"""
The SimCorp Case Logger window.
===============================

Two tabs:

  Log a Case        the original wizard - fill a form, create the case, upload
                    files and post them as comments.
  Respond to a Case load an existing case quietly, read its thread in order, and
                    reply underneath a chosen post.

Threading rule: Tk is single-threaded. Everything slow - above all the browser -
runs on a worker thread and reports back through a queue that the GUI drains on
a timer. Widgets are only ever touched from the Tk thread.
"""

from __future__ import annotations

import collections
import os
import queue
import threading
import time
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import sc_case_cache
import sc_case_reader
import sc_image_viewer
import sc_portal_pool
import sc_thread_view
import sc_settings
from sc_portal_session import PortalSession
from sc_settings import StoredSettings

try:
    from tkcalendar import Calendar

    CALENDAR_WIDGET_AVAILABLE = True
except ImportError:
    CALENDAR_WIDGET_AVAILABLE = False


SECTION_PADDING = {"padx": 8, "pady": 4}

#: How many Respond-tab log lines are kept for the pop-out window. Enough
#: to cover a long unattended session without growing without bound.
RESPOND_LOG_HISTORY_LINES = 5_000


def apply_window_icon(window, icon_path: str) -> None:
    """Set the window icon from a PNG, ignoring the failure if it is missing.

    A missing or unreadable icon must never stop the app from opening.
    """
    if not icon_path or not os.path.exists(icon_path):
        return
    try:
        icon_image = tk.PhotoImage(file=icon_path)
        window.iconphoto(True, icon_image)
        # Tk does not keep a reference, so the image would be garbage collected
        # and the icon would silently disappear.
        window._icon_image = icon_image  # noqa: SLF001 - deliberate lifetime pin
    except tk.TclError:
        pass


#: How a progress line is coloured. Checked in order, first match wins, so the
#: failure words are tested before the cheerful ones - "could not post comment"
#: must read as a failure even though it contains "post".
PROGRESS_STYLES = (
    ("progress_error", ("error", "failed", "fatal", "aborted", "abort", "traceback",
                        "! ", "cannot", "could not", "no rows", "not found")),
    ("progress_warning", ("warning", "skipp", "left for manual", "not detected",
                          "still settling", "timed out", "stopped")),
    ("progress_success", ("case logged successfully", "comment posted", "reply posted",
                          "close requested", "update posted", "logged in", "finished",
                          "case loaded", "cases read from the list", "read in",
                          "downloaded", "submitted")),
)


def classify_progress_line(message: str) -> str:
    """Pick the tag for one progress line.

    Plain lines get no tag, so only the things worth noticing carry colour.
    """
    lowered = (message or "").lower()
    for tag, needles in PROGRESS_STYLES:
        if any(needle in lowered for needle in needles):
            return tag
    return ""


def configure_progress_tags(text_widget) -> None:
    """Colour the progress panes: green for done, red for broken, amber between."""
    text_widget.tag_configure("progress_success", foreground="#0b6e4f")
    text_widget.tag_configure("progress_error", foreground="#b00020")
    text_widget.tag_configure("progress_warning", foreground="#8a5a00")


def _status_sort_key(status: str):
    """Order the status groups: the ones needing an answer come first."""
    order = sc_settings.STATUS_SORT_ORDER
    if status in order:
        return (0, order.index(status), "")
    return (1, 0, status.lower())


class CaseLoggerApplication(tk.Tk):
    """The main window: a notebook holding the two tabs."""

    def __init__(self, run_automation, settings_paths):
        super().__init__()
        self.title(f"{sc_settings.APPLICATION_NAME} - {sc_settings.APPLICATION_TAGLINE}")
        self.geometry("720x1000")
        self.resizable(True, True)

        self._run_automation = run_automation
        self._paths = settings_paths
        apply_window_icon(self, settings_paths.icon_path)

        self._progress_messages: "queue.Queue[str]" = queue.Queue()
        self._close_browser_event = threading.Event()
        self._worker_thread = None
        self._selected_file_paths: list[str] = []
        self._portal_session: PortalSession | None = None
        self._loaded_case: sc_case_reader.CaseDetails | None = None
        self._cache = sc_case_cache.CaseCache(settings_paths.cache_path)
        self._case_rows: dict[str, sc_case_cache.CaseRow] = {}
        self._selected_case_number = ""
        #: Cases a background read is working on right now, shown as such.
        self._reading_case_numbers: set[str] = set()
        #: Kept so the pop-out log can show what happened before it was
        #: opened. A ring buffer, so a long session cannot exhaust memory.
        self._respond_log_history = collections.deque(
            maxlen=RESPOND_LOG_HISTORY_LINES
        )
        self._respond_log_window = None
        self._respond_log_text = None
        self._auto_load_cancel = threading.Event()
        self._background_read_active = False
        self._active_pool: sc_portal_pool.PortalPool | None = None
        self._reply_file_paths: list[str] = []

        settings = sc_settings.load_settings(self._paths.settings_path)
        self._settings = settings
        # Clear out anything a previous version cached badly before the window
        # can show it: a login page stored as a case looks perfectly current.
        self._swept_on_start = self._cache.purge_unusable_threads()
        self._build_widgets(settings)
        self.after(100, self._drain_progress_messages)
        if self._swept_on_start:
            self._report_respond_progress(
                f"Cleared {len(self._swept_on_start)} unusable cached "
                f"thread(s): {', '.join(self._swept_on_start)}"
            )
        self._schedule_automatic_refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_quit)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def _build_widgets(self, settings: StoredSettings) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, **SECTION_PADDING)

        log_case_tab = ttk.Frame(notebook)
        respond_tab = ttk.Frame(notebook)
        notebook.add(log_case_tab, text="  Log a Case  ")
        notebook.add(respond_tab, text="  Respond to a Case  ")

        self._build_credentials_section(log_case_tab, settings)
        self._build_case_details_section(log_case_tab, settings)
        self._build_required_fields_section(log_case_tab, settings)
        self._build_business_impact_section(log_case_tab)
        self._build_action_buttons(log_case_tab)
        self._build_progress_log(log_case_tab)
        self._build_respond_tab(respond_tab)

    # -- Log a Case tab ---------------------------------------------------- #
    def _build_credentials_section(self, parent, settings: StoredSettings) -> None:
        credentials_frame = ttk.LabelFrame(parent, text="Portal credentials (kept masked)")
        credentials_frame.pack(fill="x", **SECTION_PADDING)

        ttk.Label(credentials_frame, text="Username / email").grid(
            row=0, column=0, sticky="w", padx=6, pady=4
        )
        self.username_entry = ttk.Entry(credentials_frame, width=48)
        self.username_entry.grid(row=0, column=1, sticky="we", padx=6, pady=4)
        self.username_entry.insert(0, settings.username)

        ttk.Label(credentials_frame, text="Password").grid(
            row=1, column=0, sticky="w", padx=6, pady=4
        )
        self.password_entry = ttk.Entry(credentials_frame, width=48, show="*")
        self.password_entry.grid(row=1, column=1, sticky="we", padx=6, pady=4)
        self.password_entry.insert(0, settings.password)

        self.show_password = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            credentials_frame,
            text="Show",
            variable=self.show_password,
            command=self._toggle_password_visibility,
        ).grid(row=1, column=2, padx=4)

        self.remember_credentials = tk.BooleanVar(value=bool(settings.username))
        ttk.Checkbutton(
            credentials_frame,
            text="Remember on this PC (obfuscated, not encrypted)",
            variable=self.remember_credentials,
        ).grid(row=2, column=1, sticky="w", padx=6)

        credentials_frame.columnconfigure(1, weight=1)

    def _build_case_details_section(self, parent, settings: StoredSettings) -> None:
        case_frame = ttk.LabelFrame(parent, text="Case details")
        case_frame.pack(fill="both", expand=True, **SECTION_PADDING)

        # Subject prefix: a tick box plus the prefix text, shown only while
        # ticked. Every subject then starts with this text.
        self.subject_prefix_enabled = tk.BooleanVar(value=settings.subject_prefix_enabled)
        prefix_row = ttk.Frame(case_frame)
        prefix_row.grid(row=0, column=1, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(
            prefix_row,
            text="Add subject prefix",
            variable=self.subject_prefix_enabled,
            command=self._update_subject_prefix_visibility,
        ).pack(side="left")
        self.subject_prefix_entry = ttk.Entry(prefix_row, width=24)
        self.subject_prefix_entry.insert(0, settings.subject_prefix)
        self._update_subject_prefix_visibility()

        ttk.Label(case_frame, text="Subject (name)").grid(
            row=1, column=0, sticky="w", padx=6, pady=4
        )
        self.subject_entry = ttk.Entry(case_frame, width=60)
        self.subject_entry.grid(row=1, column=1, sticky="we", padx=6, pady=4)

        ttk.Label(case_frame, text="Description").grid(
            row=2, column=0, sticky="nw", padx=6, pady=4
        )
        self.description_text = scrolledtext.ScrolledText(
            case_frame, width=50, height=8, wrap="word"
        )
        self.description_text.grid(row=2, column=1, sticky="we", padx=6, pady=4)

        ttk.Label(case_frame, text="Files").grid(row=3, column=0, sticky="nw", padx=6, pady=4)
        files_frame = ttk.Frame(case_frame)
        files_frame.grid(row=3, column=1, sticky="we", padx=6, pady=4)
        self.files_listbox = tk.Listbox(files_frame, height=4)
        self.files_listbox.pack(side="left", fill="both", expand=True)
        file_buttons_frame = ttk.Frame(files_frame)
        file_buttons_frame.pack(side="left", fill="y", padx=4)
        ttk.Button(file_buttons_frame, text="Add...", command=self._add_files).pack(
            fill="x", pady=1
        )
        ttk.Button(file_buttons_frame, text="Clear", command=self._clear_files).pack(
            fill="x", pady=1
        )

        ttk.Label(case_frame, text="First comment").grid(
            row=4, column=0, sticky="w", padx=6, pady=4
        )
        self.first_comment_entry = ttk.Entry(case_frame, width=60)
        self.first_comment_entry.grid(row=4, column=1, sticky="we", padx=6, pady=4)
        self.first_comment_entry.insert(0, sc_settings.DEFAULT_FIRST_COMMENT)

        case_frame.columnconfigure(1, weight=1)

    def _build_required_fields_section(self, parent, settings: StoredSettings) -> None:
        required_frame = ttk.LabelFrame(
            parent, text="Required dialog fields (needed for 'Next')"
        )
        required_frame.pack(fill="x", **SECTION_PADDING)

        ttk.Label(required_frame, text="Priority").grid(
            row=0, column=0, sticky="w", padx=6, pady=4
        )
        self.priority_combobox = ttk.Combobox(
            required_frame, width=22, state="readonly", values=sc_settings.PRIORITY_OPTIONS
        )
        self.priority_combobox.set(settings.priority)
        self.priority_combobox.grid(row=0, column=1, sticky="w", padx=6, pady=4)

        ttk.Label(required_frame, text="Operations & Onboarding").grid(
            row=0, column=2, sticky="w", padx=6, pady=4
        )
        self.operations_combobox = ttk.Combobox(
            required_frame, width=16, state="readonly", values=sc_settings.OPERATIONS_OPTIONS
        )
        self.operations_combobox.set(settings.operations_and_onboarding)
        self.operations_combobox.grid(row=0, column=3, sticky="w", padx=6, pady=4)

        ttk.Label(required_frame, text="Installation").grid(
            row=1, column=0, sticky="w", padx=6, pady=4
        )
        self.installation_combobox = ttk.Combobox(
            required_frame, width=22, state="readonly", values=sc_settings.INSTALLATION_OPTIONS
        )
        self.installation_combobox.set(settings.installation)
        self.installation_combobox.grid(row=1, column=1, sticky="w", padx=6, pady=4)

        # Choosing Transition makes the portal demand a project before it will
        # move on, so the choice is offered here rather than stalling mid-run.
        self.project_label = ttk.Label(required_frame, text="Project")
        self.project_combobox = ttk.Combobox(
            required_frame, width=34, state="readonly", values=sc_settings.project_labels()
        )
        self.project_combobox.set(settings.transition_project)
        self.operations_combobox.bind(
            "<<ComboboxSelected>>", lambda event: self._update_project_visibility()
        )
        self._update_project_visibility()

    def _build_business_impact_section(self, parent) -> None:
        business_impact_frame = ttk.LabelFrame(
            parent, text="Business Impact step (optional - blank fields are skipped)"
        )
        business_impact_frame.pack(fill="x", **SECTION_PADDING)

        ttk.Label(business_impact_frame, text="Business Impact").grid(
            row=0, column=0, sticky="nw", padx=6, pady=4
        )
        self.business_impact_text = scrolledtext.ScrolledText(
            business_impact_frame, width=50, height=4, wrap="word"
        )
        self.business_impact_text.grid(
            row=0, column=1, columnspan=3, sticky="we", padx=6, pady=4
        )

        ttk.Label(business_impact_frame, text="When did it happen?").grid(
            row=1, column=0, sticky="w", padx=6, pady=4
        )
        self.happened_date_entry = self._build_date_field(business_impact_frame, 1, 1)

        ttk.Label(business_impact_frame, text="When did it last work?").grid(
            row=2, column=0, sticky="w", padx=6, pady=4
        )
        self.last_worked_date_entry = self._build_date_field(business_impact_frame, 2, 1)

        business_impact_frame.columnconfigure(1, weight=1)

    def _build_action_buttons(self, parent) -> None:
        actions_frame = ttk.Frame(parent)
        actions_frame.pack(fill="x", **SECTION_PADDING)
        self.log_case_button = ttk.Button(
            actions_frame, text="Log Case", command=self._start_run
        )
        self.log_case_button.pack(side="left", padx=6)
        self.close_browser_button = ttk.Button(
            actions_frame, text="Close Browser", command=self._close_browser, state="disabled"
        )
        self.close_browser_button.pack(side="left", padx=6)

        # Hidden by default: logging a case needs no supervision, and a browser
        # window stealing focus mid-run is worse than useless.
        self.log_case_show_browser = tk.BooleanVar(value=self._settings.log_case_show_browser)
        ttk.Checkbutton(
            actions_frame,
            text="Show browser while logging",
            variable=self.log_case_show_browser,
        ).pack(side="left", padx=16)

    def _build_progress_log(self, parent) -> None:
        progress_frame = ttk.LabelFrame(parent, text="Progress")
        progress_frame.pack(fill="both", expand=True, **SECTION_PADDING)
        self.progress_text = scrolledtext.ScrolledText(
            progress_frame, height=8, wrap="word", state="disabled"
        )
        self.progress_text.pack(fill="both", expand=True, padx=4, pady=4)
        configure_progress_tags(self.progress_text)

    def _build_date_field(self, parent, row, column) -> ttk.Entry:
        """A blank-by-default date field storing ISO 'YYYY-MM-DD'.

        The dates are optional, so this uses a plain Entry rather than a
        tkcalendar DateEntry, which always holds a value and cannot be blank.
        """
        field_frame = ttk.Frame(parent)
        field_frame.grid(row=row, column=column, columnspan=3, sticky="w", padx=6, pady=4)

        date_entry = ttk.Entry(field_frame, width=16)
        date_entry.pack(side="left")
        ttk.Label(field_frame, text="(YYYY-MM-DD)").pack(side="left", padx=4)
        ttk.Button(
            field_frame, text="Pick...", width=7, command=lambda: self._pick_date(date_entry)
        ).pack(side="left", padx=2)
        ttk.Button(
            field_frame, text="Clear", width=6, command=lambda: date_entry.delete(0, "end")
        ).pack(side="left")
        return date_entry

    # -- Respond tab ------------------------------------------------------- #
    def _build_respond_tab(self, parent) -> None:
        """The case browser: load the list, pick a case, read it, reply to it."""
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", **SECTION_PADDING)

        self.load_cases_button = ttk.Button(
            toolbar, text="Load cases", command=self._load_case_list
        )
        self.load_cases_button.pack(side="left", padx=(6, 4))
        self.refresh_stale_button = ttk.Button(
            toolbar, text="Refresh changed", command=self._refresh_stale_cases, state="disabled"
        )
        self.refresh_stale_button.pack(side="left", padx=4)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(toolbar, text="Case number").pack(side="left")
        self.case_number_entry = ttk.Entry(toolbar, width=12)
        self.case_number_entry.pack(side="left", padx=4)
        self.case_number_entry.bind("<Return>", lambda event: self._find_case_by_number())
        ttk.Button(toolbar, text="Find", command=self._find_case_by_number).pack(side="left")

        ttk.Button(toolbar, text="Close session", command=self._close_portal_session).pack(
            side="right", padx=6
        )
        ttk.Button(toolbar, text="Logs", command=self._open_respond_log).pack(
            side="right", padx=4
        )
        self.stop_reading_button = ttk.Button(
            toolbar, text="Stop", command=self._stop_background_read, state="disabled"
        )
        self.stop_reading_button.pack(side="right", padx=4)

        self.auto_load_enabled = tk.BooleanVar(value=self._settings.auto_load_enabled)
        ttk.Checkbutton(
            toolbar,
            text="Auto-load active",
            variable=self.auto_load_enabled,
        ).pack(side="right", padx=8)

        self.cache_status_label = ttk.Label(parent, text="", foreground="#555555")
        self.cache_status_label.pack(fill="x", padx=14, pady=(0, 4))

        splitter = ttk.Panedwindow(parent, orient="vertical")
        self.respond_splitter = splitter

        # -- the case list, grouped by status ------------------------------ #
        list_frame = ttk.LabelFrame(splitter, text="Cases by status")
        self.case_tree = ttk.Treeview(
            list_frame,
            columns=("subject", "modified", "state"),
            displaycolumns=("subject", "modified", "state"),
            selectmode="browse",
            height=8,
        )
        self.case_tree.heading("#0", text="Case")
        self.case_tree.heading("subject", text="Subject")
        self.case_tree.heading("modified", text="Last modified")
        self.case_tree.heading("state", text="Cache")
        self.case_tree.column("#0", width=170, stretch=False)
        self.case_tree.column("subject", width=330)
        self.case_tree.column("modified", width=120, stretch=False, anchor="w")
        self.case_tree.column("state", width=70, stretch=False, anchor="w")

        tree_scroll = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.case_tree.yview
        )
        self.case_tree.configure(yscrollcommand=tree_scroll.set)
        self.case_tree.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        tree_scroll.pack(side="left", fill="y", pady=4)
        self.case_tree.bind("<<TreeviewSelect>>", self._on_case_selected)
        # A stale row is worth seeing at a glance without reading the column.
        self.case_tree.tag_configure("stale", foreground="#8a5a00")
        self.case_tree.tag_configure("reading", foreground="#1a5fb4")
        self.case_tree.tag_configure("status", font=("TkDefaultFont", 9, "bold"))
        splitter.add(list_frame, weight=1)

        # -- the selected case --------------------------------------------- #
        # Each of these is added to the splitter in its own right, so the sash
        # between any two sections moves only those two.
        thread_frame = ttk.LabelFrame(splitter, text="Case thread (oldest first)")
        self.case_summary_label = ttk.Label(
            thread_frame, text="No case loaded.", wraplength=660, justify="left"
        )
        self.case_summary_label.pack(fill="x", padx=6, pady=(4, 2))
        self.thread_text = scrolledtext.ScrolledText(
            thread_frame,
            height=18,
            wrap="word",
            state="disabled",
            background="#ffffff",
            relief="flat",
            padx=10,
            pady=8,
            spacing1=1,
            spacing3=2,
        )
        self.thread_text.pack(fill="both", expand=True, padx=4, pady=4)
        sc_thread_view.configure_tags(self.thread_text)
        self.thread_text.bind("<Double-Button-1>", self._open_image_under_pointer)

        reply_frame = ttk.LabelFrame(parent, text="Reply")
        ttk.Label(reply_frame, text="Reply under").grid(
            row=0, column=0, sticky="w", padx=6, pady=4
        )
        self.reply_target_combobox = ttk.Combobox(reply_frame, state="readonly", width=70)
        self.reply_target_combobox.grid(row=0, column=1, sticky="we", padx=6, pady=4)
        ttk.Label(reply_frame, text="Message").grid(row=1, column=0, sticky="nw", padx=6, pady=4)
        self.reply_text = scrolledtext.ScrolledText(reply_frame, height=5, wrap="word")
        self.reply_text.grid(row=1, column=1, sticky="we", padx=6, pady=4)
        ttk.Label(reply_frame, text="Files").grid(row=2, column=0, sticky="nw", padx=6, pady=4)
        reply_files_row = ttk.Frame(reply_frame)
        reply_files_row.grid(row=2, column=1, sticky="we", padx=6, pady=4)
        self.reply_files_label = ttk.Label(
            reply_files_row, text="none", foreground="#6b6b6b"
        )
        self.reply_files_label.pack(side="left")
        ttk.Button(reply_files_row, text="Add...", width=8, command=self._add_reply_files).pack(
            side="right", padx=2
        )
        ttk.Button(reply_files_row, text="Clear", width=8, command=self._clear_reply_files).pack(
            side="right", padx=2
        )

        buttons_row = ttk.Frame(reply_frame)
        buttons_row.grid(row=3, column=1, sticky="w", padx=6, pady=6)
        self.post_reply_button = ttk.Button(
            buttons_row, text="Post reply", command=self._post_reply, state="disabled"
        )
        self.post_reply_button.pack(side="left")
        self.close_case_button = ttk.Button(
            buttons_row, text="Close case", command=self._close_case, state="disabled"
        )
        self.close_case_button.pack(side="left", padx=10)
        reply_frame.columnconfigure(1, weight=1)

        progress_frame = ttk.LabelFrame(splitter, text="Progress")
        self.respond_progress_text = scrolledtext.ScrolledText(
            progress_frame, height=5, wrap="word", state="disabled"
        )
        self.respond_progress_text.pack(fill="both", expand=True, padx=4, pady=4)
        configure_progress_tags(self.respond_progress_text)

        # Reply is packed before the splitter and anchored to the bottom, so
        # it always keeps its space; the splitter takes whatever is left. Its
        # buttons therefore cannot be dragged out of view.
        reply_frame.pack(side="bottom", fill="x", padx=8, pady=(0, 6))
        splitter.add(thread_frame, weight=4)
        splitter.add(progress_frame, weight=1)
        list_frame.configure(height=140)
        thread_frame.configure(height=220)
        progress_frame.configure(height=90)

        splitter.pack(fill="both", expand=True, **SECTION_PADDING)
        self._show_cached_case_list()


    # ------------------------------------------------------------------ #
    # Widget behaviour
    # ------------------------------------------------------------------ #
    def _toggle_password_visibility(self) -> None:
        self.password_entry.config(show="" if self.show_password.get() else "*")

    def _update_subject_prefix_visibility(self) -> None:
        """Show the prefix text box only while its tick box is ticked."""
        if self.subject_prefix_enabled.get():
            self.subject_prefix_entry.pack(side="left", padx=6)
        else:
            self.subject_prefix_entry.pack_forget()

    def _update_project_visibility(self) -> None:
        """Show the project picker only for Transition, which is what needs it."""
        if self.operations_combobox.get().strip() == "Transition":
            self.project_label.grid(row=1, column=2, sticky="w", padx=6, pady=4)
            self.project_combobox.grid(row=1, column=3, sticky="w", padx=6, pady=4)
        else:
            self.project_label.grid_remove()
            self.project_combobox.grid_remove()

    def _add_files(self) -> None:
        for file_path in filedialog.askopenfilenames(title="Select files to attach"):
            if file_path not in self._selected_file_paths:
                self._selected_file_paths.append(file_path)
                self.files_listbox.insert("end", file_path)

    def _clear_files(self) -> None:
        self._selected_file_paths.clear()
        self.files_listbox.delete(0, "end")

    def _pick_date(self, date_entry: ttk.Entry) -> None:
        if not CALENDAR_WIDGET_AVAILABLE:
            messagebox.showinfo(
                "Type the date",
                "Calendar not available - please type the date as YYYY-MM-DD.",
            )
            date_entry.focus_set()
            return

        picker_window = tk.Toplevel(self)
        picker_window.title("Pick a date")
        picker_window.transient(self)
        picker_window.grab_set()
        calendar = Calendar(picker_window, date_pattern="yyyy-mm-dd")
        calendar.pack(padx=8, pady=8)

        def accept_selected_date():
            date_entry.delete(0, "end")
            date_entry.insert(0, calendar.get_date())
            picker_window.destroy()

        ttk.Button(picker_window, text="OK", command=accept_selected_date).pack(pady=(0, 8))

    # ------------------------------------------------------------------ #
    # Progress logs
    # ------------------------------------------------------------------ #
    def _report_progress(self, message: str) -> None:
        """Called from worker threads; the queue keeps Tk single-threaded."""
        self._progress_messages.put(("log", message))

    def _report_respond_progress(self, message: str) -> None:
        self._progress_messages.put(("respond", message))

    def _drain_progress_messages(self) -> None:
        while not self._progress_messages.empty():
            target, message = self._progress_messages.get()
            widget = self.progress_text if target == "log" else self.respond_progress_text
            tag = classify_progress_line(message)

            widget.config(state="normal")
            start = widget.index("end-1c")
            widget.insert("end", message + chr(10))
            if tag:
                widget.tag_add(tag, start, widget.index("end-1c"))
            widget.see("end")
            widget.config(state="disabled")

            if target == "respond":
                # Kept with a timestamp so the pop-out log can answer
                # "what happened while I was not looking".
                stamped = f"{time.strftime('%H:%M:%S')}  {message}"
                self._respond_log_history.append((stamped, tag))
                self._append_to_respond_log_window(stamped, tag)
        self.after(100, self._drain_progress_messages)

    # ------------------------------------------------------------------ #
    # Log a Case
    # ------------------------------------------------------------------ #
    def _composed_subject(self) -> str:
        """The subject as it will appear on the portal, including any prefix."""
        subject = self.subject_entry.get().strip()
        if not self.subject_prefix_enabled.get():
            return subject
        return sc_settings.apply_subject_prefix(self.subject_prefix_entry.get(), subject)

    def _current_settings(self) -> StoredSettings:
        remember = self.remember_credentials.get()
        return StoredSettings(
            username=self.username_entry.get().strip() if remember else "",
            password=self.password_entry.get() if remember else "",
            priority=self.priority_combobox.get().strip(),
            operations_and_onboarding=self.operations_combobox.get().strip(),
            installation=self.installation_combobox.get().strip(),
            subject_prefix=self.subject_prefix_entry.get(),
            subject_prefix_enabled=self.subject_prefix_enabled.get(),
            auto_load_enabled=self.auto_load_enabled.get(),
            log_case_show_browser=self.log_case_show_browser.get(),
            transition_project=self.project_combobox.get().strip(),
        )

    def _start_run(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            messagebox.showinfo("Busy", "Automation is already running.")
            return

        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        subject = self._composed_subject()
        description = self.description_text.get("1.0", "end").strip()

        if not username or not password:
            messagebox.showwarning("Missing", "Please enter username and password.")
            return
        if not self.subject_entry.get().strip() or not description:
            messagebox.showwarning("Missing", "Please enter a Subject and Description.")
            return

        file_paths = list(self._selected_file_paths)
        # Every run creates a real case, so confirm once - showing the exact
        # subject, prefix included.
        if not messagebox.askyesno(
            "Create a case?",
            "This will fill the form, click through, and CREATE a real case "
            "on the SimCorp portal (uploading files and posting comments).\n\n"
            f"Subject:\n{subject}\n\n"
            "Continue?",
        ):
            return

        sc_settings.save_settings(self._current_settings(), self._paths.settings_path)

        submission = sc_settings.CaseSubmission(
            username=username,
            password=password,
            subject=subject,
            description=description,
            priority=self.priority_combobox.get().strip(),
            operations_and_onboarding=self.operations_combobox.get().strip(),
            installation=self.installation_combobox.get().strip(),
            business_impact=self.business_impact_text.get("1.0", "end").strip(),
            happened_date=self.happened_date_entry.get().strip(),
            last_worked_date=self.last_worked_date_entry.get().strip(),
            first_comment=self.first_comment_entry.get().strip(),
            file_paths=file_paths,
            show_browser=self.log_case_show_browser.get(),
            project=self.project_combobox.get().strip(),
        )

        self._close_browser_event.clear()
        self.log_case_button.config(state="disabled")
        self.close_browser_button.config(
            state="normal" if submission.show_browser else "disabled"
        )
        self._worker_thread = threading.Thread(
            target=self._run_worker, args=(submission,), daemon=True
        )
        self._worker_thread.start()

    def _run_worker(self, submission) -> None:
        try:
            self._run_automation(submission, self._report_progress, self._close_browser_event)
        except Exception:  # noqa: BLE001 - a worker crash must reach the log
            self._report_progress("FATAL:")
            self._report_progress(traceback.format_exc())
        finally:
            self.after(0, self._on_worker_finished)

    def _on_worker_finished(self) -> None:
        self.log_case_button.config(state="normal")
        self.close_browser_button.config(state="disabled")

    def _close_browser(self) -> None:
        self._close_browser_event.set()

    # ------------------------------------------------------------------ #
    # Respond to a Case
    # ------------------------------------------------------------------ #
    def _run_in_background(self, work, on_success, title, report_progress, quiet=False) -> None:
        """Run `work()` off the Tk thread, then apply `on_success` on it.

        `quiet` suppresses the error dialog and reports to the progress log
        instead. Anything started by a timer rather than by a click uses it, so
        a background hiccup never interrupts what someone is doing.
        """

        def worker():
            try:
                result = work()
            except Exception as error:  # noqa: BLE001 - reported, never raised on
                message = str(error) or error.__class__.__name__
                report_progress(f"  ! {title} failed: {message}")
                if not quiet:
                    self.after(0, lambda: messagebox.showerror(title, message))
                self.after(0, self._release_background_read)
                self.after(0, self._enable_respond_controls)
                return
            self.after(0, lambda: on_success(result))

        threading.Thread(target=worker, daemon=True).start()

    def _portal_credentials(self) -> tuple[str, str]:
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            raise sc_case_reader.CaseReaderError(
                "Enter your portal username and password on the 'Log a Case' tab first."
            )
        return username, password

    def _ensure_portal_session(self) -> PortalSession:
        """The session logs in once and is then reused, so only the first action
        of a sitting pays the ~30 s login."""
        if self._portal_session is not None and self._portal_session.is_running():
            return self._portal_session
        username, password = self._portal_credentials()
        self._portal_session = PortalSession(
            username, password, self._report_respond_progress, headless=True
        )
        self._portal_session.ensure_started()
        return self._portal_session

    # -- the pop-out log ---------------------------------------------------- #
    def _open_respond_log(self) -> None:
        """Show everything the Respond tab has done, in its own window.

        The inline Progress pane is small and scrolls away; this keeps the last
        RESPOND_LOG_HISTORY_LINES lines, timestamped, so a question like "what
        happened when I closed that case" can actually be answered.
        """
        if self._respond_log_window is not None and self._respond_log_window.winfo_exists():
            self._respond_log_window.lift()
            self._respond_log_window.focus_force()
            return

        window = tk.Toplevel(self)
        window.title(f"{sc_settings.APPLICATION_NAME} - Respond to a Case log")
        window.geometry("1000x620")
        apply_window_icon(window, self._paths.icon_path)

        controls = ttk.Frame(window)
        controls.pack(side="bottom", fill="x")
        ttk.Button(controls, text="Clear logs", command=self._clear_respond_log).pack(
            side="left", padx=8, pady=6
        )
        ttk.Button(controls, text="Copy all", command=self._copy_respond_log).pack(
            side="left", padx=4, pady=6
        )
        self._respond_log_follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            controls, text="Follow new lines", variable=self._respond_log_follow
        ).pack(side="left", padx=12)
        self._respond_log_count_label = ttk.Label(controls, foreground="#6b6b6b")
        self._respond_log_count_label.pack(side="left", padx=12)
        ttk.Button(controls, text="Close", command=window.destroy).pack(
            side="right", padx=8, pady=6
        )

        log_text = scrolledtext.ScrolledText(
            window, wrap="word", state="disabled", background="#ffffff",
            font=("Consolas", 9), padx=8, pady=6,
        )
        log_text.pack(fill="both", expand=True, padx=6, pady=6)
        configure_progress_tags(log_text)

        self._respond_log_window = window
        self._respond_log_text = log_text
        window.protocol("WM_DELETE_WINDOW", self._on_respond_log_closed)
        window.bind("<Escape>", lambda event: self._on_respond_log_closed())

        # Fill in everything captured before the window was opened.
        log_text.config(state="normal")
        for line, tag in self._respond_log_history:
            start = log_text.index("end-1c")
            log_text.insert("end", line + chr(10))
            if tag:
                log_text.tag_add(tag, start, log_text.index("end-1c"))
        log_text.see("end")
        log_text.config(state="disabled")
        self._update_respond_log_count()

    def _on_respond_log_closed(self) -> None:
        window, self._respond_log_window = self._respond_log_window, None
        self._respond_log_text = None
        if window is not None and window.winfo_exists():
            window.destroy()

    def _append_to_respond_log_window(self, line: str, tag: str) -> None:
        """Mirror one line into the pop-out, if it happens to be open."""
        log_text = self._respond_log_text
        if log_text is None or not log_text.winfo_exists():
            return
        log_text.config(state="normal")
        start = log_text.index("end-1c")
        log_text.insert("end", line + chr(10))
        if tag:
            log_text.tag_add(tag, start, log_text.index("end-1c"))
        if getattr(self, "_respond_log_follow", None) is None or self._respond_log_follow.get():
            log_text.see("end")
        log_text.config(state="disabled")
        self._update_respond_log_count()

    def _update_respond_log_count(self) -> None:
        label = getattr(self, "_respond_log_count_label", None)
        if label is None or not label.winfo_exists():
            return
        kept = len(self._respond_log_history)
        label.config(text=f"{kept} line(s) kept (most recent {RESPOND_LOG_HISTORY_LINES})")

    def _clear_respond_log(self) -> None:
        self._respond_log_history.clear()
        log_text = self._respond_log_text
        if log_text is not None and log_text.winfo_exists():
            log_text.config(state="normal")
            log_text.delete("1.0", "end")
            log_text.config(state="disabled")
        self._update_respond_log_count()

    def _copy_respond_log(self) -> None:
        """Put the whole log on the clipboard, for pasting into a message."""
        self.clipboard_clear()
        self.clipboard_append(chr(10).join(line for line, _tag in self._respond_log_history))

    # -- the case list -------------------------------------------------- #
    def _schedule_automatic_refresh(self) -> None:
        """Re-check the case list on a timer for as long as the app is open.

        Only the list is refreshed, which is a single page load; whether any
        thread is then re-read is decided by the usual delta rules. It skips
        itself while a read is already running, and when no credentials have
        been entered, so it can never interrupt work or pop up a login error.
        """
        interval_milliseconds = max(1, sc_settings.AUTO_REFRESH_MINUTES) * 60_000
        self.after(interval_milliseconds, self._automatic_refresh_tick)

    def _automatic_refresh_tick(self) -> None:
        try:
            if self._background_read_active:
                self._report_respond_progress(
                    "Hourly check skipped: a read is already running."
                )
            elif not self.username_entry.get().strip() or not self.password_entry.get():
                pass  # nobody has signed in yet; stay quiet
            else:
                self._report_respond_progress("Hourly check: refreshing the case list...")
                self._load_case_list(quiet=True)
        finally:
            # Always queue the next one, even if this attempt failed.
            self._schedule_automatic_refresh()

    def _load_case_list(self, quiet: bool = False) -> None:
        """Refresh the list of cases from the portal and work out the deltas.

        `quiet` is set by the hourly timer: a failure then is logged rather
        than raised in a dialog over whatever the user is doing.
        """
        self.load_cases_button.config(state="disabled")
        self.refresh_stale_button.config(state="disabled")
        self._report_respond_progress("Refreshing the case list...")

        def work():
            session = self._ensure_portal_session()
            rows = session.run(
                lambda page: sc_case_reader.fetch_case_rows(
                    page, self._report_respond_progress
                )
            )
            return self._cache.store_case_rows(rows)

        def done(outcome):
            self._report_respond_progress(f"Case list: {outcome.summary()}.")
            for case_number in outcome.added:
                self._report_respond_progress(f"   new     {case_number}")
            for case_number in outcome.updated:
                self._report_respond_progress(f"   changed {case_number}")
            self._purge_closed_case_attachments(outcome.removed)
            self._show_cached_case_list()
            self._enable_respond_controls()
            self._auto_load_active_cases(quiet=quiet)

        self._run_in_background(
            work, done, "Load cases", self._report_respond_progress, quiet=quiet
        )

    def _purge_closed_case_attachments(self, case_numbers) -> None:
        """Drop the downloaded screenshots of cases that have left the list.

        A case leaves the view when it is closed, so its pictures stop being
        worth the disk. The cached thread goes with them, which means that if
        the case is reopened it is simply read again and the pictures come back.
        """
        if not case_numbers:
            return
        total_files = 0
        total_bytes = 0
        for case_number in case_numbers:
            files, freed = self._cache.purge_case_attachments(case_number)
            total_files += files
            total_bytes += freed
            self._report_respond_progress(
                f"   closed  {case_number} - removed {files} image(s)"
                if files
                else f"   closed  {case_number}"
            )
        if total_files:
            self._report_respond_progress(
                f"Freed {total_bytes / 1024 / 1024:.1f} MB from {total_files} image(s)."
            )

    def _show_cached_case_list(self) -> None:
        """Fill the tree from the cache, grouped by status. No portal access."""
        rows = self._cache.get_case_rows()
        self._case_rows = {row.case_number: row for row in rows}

        selected = self._selected_case_number
        self.case_tree.delete(*self.case_tree.get_children())

        grouped: dict[str, list] = {}
        for row in rows:
            grouped.setdefault(row.status or "(no status)", []).append(row)

        for status in sorted(grouped, key=_status_sort_key):
            group = grouped[status]
            stale_count = sum(1 for row in group if row.is_stale)
            label = f"{status}  ({len(group)})"
            if stale_count:
                label += f"  -  {stale_count} to read"
            parent = self.case_tree.insert(
                "", "end", iid=f"status::{status}", text=label, open=True, tags=("status",)
            )
            for row in group:
                if row.case_number in self._reading_case_numbers:
                    state = "reading..."
                elif row.is_new:
                    state = "new"
                elif row.is_stale:
                    state = "stale"
                else:
                    state = "cached"
                self.case_tree.insert(
                    parent,
                    "end",
                    iid=row.case_number,
                    text=row.case_number,
                    values=(row.subject, row.last_modified, state),
                    tags=("reading",)
                    if row.case_number in self._reading_case_numbers
                    else (("stale",) if row.is_stale else ()),
                )

        if selected and self.case_tree.exists(selected):
            self.case_tree.selection_set(selected)

        cases, threads = self._cache.statistics()
        refreshed = self._cache.last_list_refresh or "never"
        stale = len(self._cache.stale_case_numbers())
        image_files, image_bytes = self._cache.attachment_usage()
        self.cache_status_label.config(
            text=(
                f"{cases} cases listed  |  {threads} threads cached  |  "
                f"{stale} to read  |  {image_files} images "
                f"({image_bytes / 1024 / 1024:.1f} MB)  |  "
                f"list refreshed {refreshed} UTC"
            )
        )
        self.refresh_stale_button.config(state="normal" if stale else "disabled")

    def _on_case_selected(self, _event=None) -> None:
        """Open whichever case the user clicked, from cache when it is current."""
        selection = self.case_tree.selection()
        if not selection:
            return
        node = selection[0]
        if node.startswith("status::"):
            return

        case_number = node
        if (
            case_number == self._selected_case_number
            and self._loaded_case is not None
            and self._loaded_case.case_number == case_number
        ):
            # Rebuilding the tree restores the selection, which Tk reports as a
            # fresh <<TreeviewSelect>>. The case is already on screen, so there
            # is nothing to do; without this the thread is re-rendered on every
            # refresh, which floods the log and scrolls the reader's place away.
            return

        self._selected_case_number = case_number
        row = self._case_rows.get(case_number)
        if row is None:
            return

        cached = self._cache.get_thread(case_number, row.last_modified)
        if cached is not None:
            self._report_respond_progress(
                f"Case {case_number} shown from cache "
                f"(read {self._cache.thread_fetched_at(case_number)} UTC)."
            )
            self._show_case(cached)
            return

        self._begin_case_load(case_number, row)

    def _begin_case_load(self, case_number: str, row) -> None:
        """Read one case from the portal, showing that it is loading."""
        self.case_summary_label.config(text=f"Loading case {case_number}...")
        self.thread_text.config(state="normal")
        self.thread_text.delete("1.0", "end")
        self.thread_text.insert(
            "1.0", f"Loading {case_number} from the portal...", "note"
        )
        self.thread_text.config(state="disabled")
        self.post_reply_button.config(state="disabled")
        self._report_respond_progress(f"Reading case {case_number} from the portal...")

        def work():
            session = self._ensure_portal_session()

            def read(page):
                url = row.url or sc_case_reader.find_case_url(
                    page, case_number, self._report_respond_progress
                )
                return sc_case_reader.read_case(
                    page, case_number, url, self._report_respond_progress
                )

            details = session.run(read)
            session.run(
                lambda page: sc_case_reader.download_post_images(
                    page, details, self._cache.attachment_directory,
                    self._report_respond_progress,
                )
            )
            self._cache.store_thread(details, row.last_modified)
            return details

        def done(details):
            self._show_case(details)
            self._show_cached_case_list()

        self._run_in_background(work, done, "Load case", self._report_respond_progress)

    def _refresh_stale_cases(self) -> None:
        """Read every case whose cached thread is missing or out of date."""
        stale = self._cache.stale_case_numbers()
        if not stale:
            self._report_respond_progress("Nothing to refresh; every case is current.")
            return
        if not messagebox.askyesno(
            "Refresh changed cases?",
            f"{len(stale)} case(s) have changed or have never been read.\n\n"
            "Reading them takes roughly 20-30 seconds each and runs quietly in "
            "the background. You can press Stop at any time.\n\nGo ahead?",
        ):
            return
        self._read_cases_in_background(stale, "Refresh")

    def _auto_load_active_cases(self, quiet: bool = False) -> None:
        """Read the cases that are still being worked, without being asked.

        Only the statuses in AUTO_LOAD_STATUSES are pre-loaded: those are the
        ones someone is likely to open next. Closed or completed cases are left
        until they are actually clicked.
        """
        if not self.auto_load_enabled.get():
            return
        wanted = [
            row.case_number
            for row in self._cache.get_case_rows()
            if row.is_stale and row.status in sc_settings.AUTO_LOAD_STATUSES
        ]
        if not wanted:
            self._report_respond_progress("Auto-load: every active case is already current.")
            return
        statuses = " and ".join(sc_settings.AUTO_LOAD_STATUSES)
        self._report_respond_progress(
            f"Auto-load: reading {len(wanted)} case(s) in {statuses}..."
        )
        self._read_cases_in_background(wanted, "Auto-load", quiet=quiet)

    def _read_one_case(self, page, case_number: str):
        """Read a case and save its screenshots. Runs on a browser thread."""
        self._reading_case_numbers.add(case_number)
        self.after(0, self._show_cached_case_list)
        row = self._case_rows.get(case_number)
        url = (row.url if row else "") or sc_case_reader.find_case_url(
            page, case_number, self._report_respond_progress
        )
        details = sc_case_reader.read_case(
            page, case_number, url, self._report_respond_progress
        )
        sc_case_reader.download_post_images(
            page, details, self._cache.attachment_directory, self._report_respond_progress
        )
        return details

    def _store_read_case(self, case_number: str, details) -> None:
        row = self._case_rows.get(case_number)
        self._cache.store_thread(details, row.last_modified if row else "")
        self._reading_case_numbers.discard(case_number)

    def _read_cases_in_background(
        self, case_numbers: list[str], label: str, quiet: bool = False
    ) -> None:
        """Read a set of cases, in parallel when there are enough to justify it.

        A backlog is spread across a small pool of browsers sharing one login; a
        handful goes through the single session that is already open, because
        starting browsers would cost more than it saves.
        """
        if self._background_read_active:
            self._report_respond_progress("A background read is already running.")
            return

        self._background_read_active = True
        self._auto_load_cancel.clear()
        self.load_cases_button.config(state="disabled")
        self.refresh_stale_button.config(state="disabled")
        self.stop_reading_button.config(state="normal")
        use_pool = len(case_numbers) >= sc_portal_pool.MINIMUM_CASES_FOR_POOL

        def work():
            session = self._ensure_portal_session()
            if use_pool:
                return self._read_with_pool(session, case_numbers, label)
            return self._read_with_single_session(session, case_numbers, label)

        def done(read_count):
            self._active_pool = None
            self._background_read_active = False
            self.stop_reading_button.config(state="disabled")
            self._report_respond_progress(
                f"{label} finished: {read_count} of {len(case_numbers)} case(s) read."
            )
            self._show_cached_case_list()
            self._enable_respond_controls()

        self._run_in_background(
            work, done, label, self._report_respond_progress, quiet=quiet
        )

    def _read_with_pool(self, session, case_numbers: list[str], label: str) -> int:
        """Spread a backlog across several browsers that share one login."""
        state_path = os.path.join(
            os.path.dirname(self._cache.cache_path), "portal_state.json"
        )
        session.run(lambda page: page.context.storage_state(path=state_path))

        pool = sc_portal_pool.PortalPool(
            state_path, self._report_respond_progress, stagger_seconds=self._stagger_seconds()
        )
        self._active_pool = pool
        self._report_respond_progress(
            f"{label}: reading {len(case_numbers)} case(s) across up to "
            f"{sc_portal_pool.DEFAULT_WORKER_COUNT} browsers..."
        )

        read_count = 0
        counter_lock = threading.Lock()

        def handle(result):
            nonlocal read_count
            if not result.succeeded:
                return
            self._store_read_case(result.case_number, result.details)
            with counter_lock:
                read_count += 1
            self.after(0, self._show_cached_case_list)

        def watch_for_stop():
            while not self._auto_load_cancel.wait(0.5):
                if pool.cancelled:
                    return
            pool.cancel()

        stopper = threading.Thread(target=watch_for_stop, daemon=True)
        stopper.start()
        pool.read_cases(case_numbers, self._read_one_case, handle)
        self._auto_load_cancel.set()  # release the watcher
        return read_count

    def _read_with_single_session(self, session, case_numbers: list[str], label: str) -> int:
        """Read a handful of cases down the session that is already logged in."""
        read_count = 0
        for position, case_number in enumerate(case_numbers, start=1):
            if self._auto_load_cancel.is_set():
                self._report_respond_progress(f"{label}: stopped after {read_count}.")
                break
            self._report_respond_progress(
                f"{label} [{position}/{len(case_numbers)}] {case_number}..."
            )
            try:
                details = session.run(
                    lambda page, case_number=case_number: self._read_one_case(page, case_number)
                )
            except Exception as error:  # noqa: BLE001 - one bad case must not stop the rest
                self._report_respond_progress(f"   ! {case_number} failed: {error}")
                continue
            self._store_read_case(case_number, details)
            read_count += 1
            self.after(0, self._show_cached_case_list)
        return read_count

    def _stagger_seconds(self) -> float:
        return sc_portal_pool.DEFAULT_STAGGER_SECONDS

    def _release_background_read(self) -> None:
        """Clear the background-read flag after a failure, so the buttons work."""
        self._background_read_active = False
        self.stop_reading_button.config(state="disabled")

    def _stop_background_read(self) -> None:
        self._auto_load_cancel.set()
        if self._active_pool is not None:
            self._active_pool.cancel()
        self.stop_reading_button.config(state="disabled")
        self._report_respond_progress("Stopping after the case being read...")

    def _find_case_by_number(self) -> None:
        """Open a case by number, including one outside the listed view.

        The list view is filtered, so a closed case will not be in the tree;
        searching reaches it anyway.
        """
        try:
            case_number = sc_case_reader.normalise_case_number(
                self.case_number_entry.get()
            )
        except sc_case_reader.CaseReaderError as error:
            messagebox.showwarning("Case number", str(error))
            return

        if self.case_tree.exists(case_number):
            self.case_tree.selection_set(case_number)
            self.case_tree.see(case_number)
            return

        self._selected_case_number = case_number
        row = self._cache.get_case_row(case_number) or sc_case_cache.CaseRow(
            case_number=case_number
        )
        self._case_rows.setdefault(case_number, row)
        cached = self._cache.get_thread(case_number, row.last_modified)
        if cached is not None:
            self._show_case(cached)
            return
        self._begin_case_load(case_number, row)

    # -- one case -------------------------------------------------------- #
    def _show_case(self, details: sc_case_reader.CaseDetails) -> None:
        self._loaded_case = details
        self.case_summary_label.config(
            text=(
                f"{details.case_number}  |  {details.subject}\n"
                f"Status: {details.status or '-'}   Priority: {details.priority or '-'}   "
                f"Type: {details.case_type or '-'}   Contact: {details.contact_name or '-'}"
            )
        )
        sc_thread_view.render_thread(
            self.thread_text, details, sc_case_reader.PORTAL_HAS_NO_DESCRIPTION
        )

        choices = [
            f"[{position}] {post.summary_line()}"
            for position, post in enumerate(details.posts, start=1)
        ]
        self.reply_target_combobox.config(values=choices)
        if choices:
            # Default to the newest post, which is what a reply usually answers.
            self.reply_target_combobox.current(len(choices) - 1)
        self._enable_respond_controls()

    def _enable_respond_controls(self) -> None:
        self.load_cases_button.config(state="normal")
        self.refresh_stale_button.config(
            state="normal" if self._cache.stale_case_numbers() else "disabled"
        )
        has_case = bool(self._loaded_case)
        has_posts = bool(self._loaded_case and self._loaded_case.posts)
        self.post_reply_button.config(state="normal" if has_posts else "disabled")
        self.close_case_button.config(state="normal" if has_case else "disabled")

    def _open_image_under_pointer(self, event):
        """Open the screenshot that was double-clicked, at full size.

        Returning "break" stops the double-click also selecting the word behind
        the picture, which leaves a stray highlight on a read-only pane.
        """
        image_path = sc_image_viewer.image_at_click(self.thread_text, event)
        if not image_path:
            return None
        case_number = self._loaded_case.case_number if self._loaded_case else ""
        title = f"Case {case_number} - attachment" if case_number else "Attachment"
        sc_image_viewer.show_image(self, image_path, title, self._paths.icon_path)
        return "break"

    def _add_reply_files(self) -> None:
        chosen = filedialog.askopenfilenames(title="Attach files to this reply")
        for file_path in chosen:
            if file_path not in self._reply_file_paths:
                self._reply_file_paths.append(file_path)
        self._update_reply_files_label()

    def _clear_reply_files(self) -> None:
        self._reply_file_paths.clear()
        self._update_reply_files_label()

    def _update_reply_files_label(self) -> None:
        """Show what is attached, and warn once the portal's limit is passed."""
        count = len(self._reply_file_paths)
        if not count:
            self.reply_files_label.config(text="none", foreground="#6b6b6b")
            return
        names = ", ".join(os.path.basename(path) for path in self._reply_file_paths)
        over_limit = count > sc_case_reader.MAXIMUM_PUBLISHER_FILES
        self.reply_files_label.config(
            text=f"{count} file(s): {names[:90]}"
            + (f"  - the portal accepts {sc_case_reader.MAXIMUM_PUBLISHER_FILES}" if over_limit else ""),
            foreground="#b00020" if over_limit else "#0b6e4f",
        )

    def _close_case(self) -> None:
        """Press the portal's CLOSE CASE button for the case on screen."""
        if self._loaded_case is None:
            return
        case_number = self._loaded_case.case_number
        if not messagebox.askyesno(
            "Close this case?",
            "This CLOSES the case on the SimCorp portal.\n\n"
            f"Case {case_number}\n{self._loaded_case.subject}\n\n"
            "Continue?",
        ):
            return

        self.close_case_button.config(state="disabled")
        case_url = self._loaded_case.url

        def work():
            session = self._ensure_portal_session()

            def close_then_reread(page):
                page.goto(case_url, wait_until="domcontentloaded")
                sc_case_reader.wait_for_network_idle(page)
                page.wait_for_timeout(5_000)
                sc_case_reader.close_case(page, self._report_respond_progress)
                return sc_case_reader.read_case(
                    page, case_number, page.url, self._report_respond_progress
                )

            details = session.run(close_then_reread)
            # The case has changed, so the cached copy must not be trusted.
            self._cache.store_thread(details, "")
            return details

        def done(details):
            self._report_respond_progress(
                f"Case {case_number} is now {details.status or 'closed'}."
            )
            self._show_case(details)
            # Do NOT purge here. The row is still on screen and still selected,
            # and dropping its thread makes it read as "new" while the user is
            # looking at it. Refresh the list instead: the pinned view is
            # "All Open Cases", so a closed case leaves it, and the normal
            # removal path then purges its images.
            self._report_respond_progress(
                "Refreshing the case list so the closed case drops out..."
            )
            self._load_case_list()

        self._run_in_background(work, done, "Close case", self._report_respond_progress)

    def _post_reply(self) -> None:
        """Reply under the chosen post, or post an update when files are attached.

        A Chatter comment can only carry one attachment, and the dialog that
        would select it is currently broken on the portal. The feed's own
        publisher accepts up to ten files, so a reply with files is posted there
        as a case update instead. Without files, the reply stays a comment under
        the post it answers.
        """
        if self._loaded_case is None:
            messagebox.showwarning("Reply", "Load a case first.")
            return
        reply_body = self.reply_text.get("1.0", "end").strip()
        if not reply_body:
            messagebox.showwarning("Reply", "Type a reply first.")
            return

        file_paths = list(self._reply_file_paths)
        if len(file_paths) > sc_case_reader.MAXIMUM_PUBLISHER_FILES:
            messagebox.showwarning(
                "Reply",
                f"The portal accepts at most {sc_case_reader.MAXIMUM_PUBLISHER_FILES} "
                f"files on one update; {len(file_paths)} are attached.",
            )
            return

        case_number = self._loaded_case.case_number
        case_url = self._loaded_case.url
        post = None
        if not file_paths:
            selected = self.reply_target_combobox.current()
            if selected < 0:
                messagebox.showwarning("Reply", "Choose the post to reply under.")
                return
            post = self._loaded_case.posts[selected]

        if file_paths:
            where = (
                f"As a case update with {len(file_paths)} file(s), because the "
                "portal only accepts attachments there."
            )
        else:
            where = f"As a comment under: {post.author} - {post.timestamp}"
        if not messagebox.askyesno(
            "Post reply?",
            "This posts a REAL reply on the SimCorp portal.\n\n"
            f"Case {case_number}\n{where}\n\n{reply_body}\n\nContinue?",
        ):
            return

        self.post_reply_button.config(state="disabled")

        def work():
            session = self._ensure_portal_session()

            def send(page):
                if file_paths:
                    page.goto(case_url, wait_until="domcontentloaded")
                    sc_case_reader.wait_for_network_idle(page)
                    page.wait_for_timeout(5_000)
                    sc_case_reader.post_update_with_files(
                        page, reply_body, file_paths, self._report_respond_progress
                    )
                else:
                    sc_case_reader.post_reply(
                        page, post, reply_body, self._report_respond_progress
                    )
                details = sc_case_reader.read_case(
                    page, case_number, page.url, self._report_respond_progress
                )
                sc_case_reader.download_post_images(
                    page, details, self._cache.attachment_directory,
                    self._report_respond_progress,
                )
                return details

            details = session.run(send)
            # The thread has moved on, so the cached copy is marked for re-reading.
            self._cache.store_thread(details, "")
            return details

        def done(details):
            self.reply_text.delete("1.0", "end")
            self._clear_reply_files()
            self._report_respond_progress("Reply posted; thread re-read.")
            self._show_case(details)
            self._show_cached_case_list()

        self._run_in_background(work, done, "Post reply", self._report_respond_progress)

    def _close_portal_session(self) -> None:
        if self._portal_session is None:
            self._report_respond_progress("No portal session is open.")
            return
        self._report_respond_progress("Closing the portal session...")
        session, self._portal_session = self._portal_session, None
        threading.Thread(target=session.close, daemon=True).start()

    # ------------------------------------------------------------------ #

    def _on_quit(self) -> None:
        self._close_browser_event.set()
        if self._portal_session is not None:
            session, self._portal_session = self._portal_session, None
            threading.Thread(target=session.close, daemon=True).start()
        self.destroy()
