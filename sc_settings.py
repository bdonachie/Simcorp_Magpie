"""
Settings, paths and the values passed between the GUI and the automation.
=========================================================================

NOTE on the stored password: base64 is obfuscation, NOT encryption. It only
keeps the password out of plain sight on disk and on screen. The settings file
must not be treated as secure.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from dataclasses import dataclass, field

#: The app is called Magpie. The settings file keeps its old name so an
#: existing install does not lose its saved credentials on upgrade.
APPLICATION_NAME = "Magpie"
APPLICATION_TAGLINE = "SimCorp case manager"
SETTINGS_FILE_NAME = ".sc_case_logger.json"
SCREENSHOT_DIRECTORY_NAME = "_screenshots"
CACHE_DIRECTORY_NAME = "_cache"
BROWSERS_DIRECTORY_NAME = "browsers"
ICON_FILE_NAME = "icon.PNG"

# Dropdown options, exactly as offered on the portal, plus their defaults.
PRIORITY_OPTIONS = ["--None--", "1-Critical", "2-High", "3-Medium", "4-Low"]
OPERATIONS_OPTIONS = ["--None--", "Operation", "Transition"]
INSTALLATION_OPTIONS = [
    "--None--",
    "TEST / 26.04",
    "DEV / 26.04",
    "MIGR / 26.04",
    "GOLD / 26.04",
    "* PROD / 26.04 - MainProd",
]
DEFAULT_PRIORITY = "3-Medium"
DEFAULT_OPERATIONS = "Operation"
DEFAULT_INSTALLATION = "TEST / 26.04"

#: Choosing "Transition" adds a required Project Selection step to the wizard
#: (DEVELOPMENT_NOTES.md section 24.3). The projects are specific to the
#: customer account the portal is signed in as, so replace these labels with
#: the ones your portal shows, in its own order. The index is what the radio
#: carries, which is how two entries sharing the label "Client - Customer
#: Care" stay distinguishable.
TRANSITION_PROJECTS = (
    ("1", "Client - FO Implementation"),
    ("2", "Client - Customer Care"),
    ("3", "Client - PaaS Onboarding"),
    ("4", "Client - Business Service Onboarding"),
    ("5", "Client - Consulting General"),
    ("6", "Client - Customer Care (2)"),
    ("7", "Client - Care Onboarding"),
)
PROJECT_VALUE_PREFIX = "ChoiceListCollectionCIProjects."
DEFAULT_TRANSITION_PROJECT = "Client - FO Implementation"


def project_choice_value(label: str) -> str:
    """The radio value behind a project label, or "" when unknown."""
    for index, name in TRANSITION_PROJECTS:
        if name == label:
            return PROJECT_VALUE_PREFIX + index
    return ""


def project_labels() -> list[str]:
    return [name for _index, name in TRANSITION_PROJECTS]


DEFAULT_SUBJECT_PREFIX = "UAT: "
DEFAULT_FIRST_COMMENT = "Please find the attached files."

#: Statuses whose cases are read automatically after a list refresh. These
#: are the ones still being worked, so they are the ones worth having ready.
AUTO_LOAD_STATUSES = ("Waiting for Customer", "In Progress")

#: Order the status groups appear in. Cases waiting on us come first,
#: because those are the ones needing an answer. Anything not listed sorts
#: alphabetically after these.
STATUS_SORT_ORDER = (
    "Waiting for Customer",
    "In Progress",
    "Pending",
    "Completed by SimCorp",
)

#: How often the case list is re-checked for changes while the app is open.
AUTO_REFRESH_MINUTES = 60


def application_directory() -> str:
    """Folder the app runs from: the .exe's folder when frozen by PyInstaller,
    otherwise the folder holding this module."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


@dataclass
class ApplicationPaths:
    """Every file the app reads or writes, all resolved beside the app so the
    whole folder stays portable and inside the whitelisted tree."""

    root: str

    @property
    def settings_path(self) -> str:
        return os.path.join(self.root, SETTINGS_FILE_NAME)

    @property
    def screenshot_directory(self) -> str:
        return os.path.join(self.root, SCREENSHOT_DIRECTORY_NAME)

    @property
    def cache_path(self) -> str:
        return os.path.join(self.root, CACHE_DIRECTORY_NAME, "cases.sqlite3")

    @property
    def browsers_directory(self) -> str:
        return os.path.join(self.root, BROWSERS_DIRECTORY_NAME)

    @property
    def icon_path(self) -> str:
        return os.path.join(self.root, ICON_FILE_NAME)


@dataclass
class CaseSubmission:
    """Everything the automation needs for one case. A dataclass rather than a
    dict means a mistyped field name fails immediately and loudly."""

    username: str
    password: str
    subject: str
    description: str
    priority: str
    operations_and_onboarding: str
    installation: str
    business_impact: str = ""
    happened_date: str = ""
    last_worked_date: str = ""
    first_comment: str = ""
    file_paths: list[str] = field(default_factory=list)
    #: Normally the case is logged with the browser hidden. Turning this on
    #: shows the window so the run can be watched, and leaves it open at the
    #: end for review.
    show_browser: bool = False
    #: Only used when operations_and_onboarding is "Transition", which makes
    #: the portal demand a project before it will go on.
    project: str = ""

    def existing_file_paths(self) -> list[str]:
        """The selected files that are still on disk when the run starts."""
        return [file_path for file_path in self.file_paths if os.path.exists(file_path)]


@dataclass
class StoredSettings:
    """The subset of the GUI state remembered between runs."""

    username: str = ""
    password: str = ""
    priority: str = DEFAULT_PRIORITY
    operations_and_onboarding: str = DEFAULT_OPERATIONS
    installation: str = DEFAULT_INSTALLATION
    subject_prefix: str = DEFAULT_SUBJECT_PREFIX
    subject_prefix_enabled: bool = False
    auto_load_enabled: bool = True
    log_case_show_browser: bool = False
    transition_project: str = DEFAULT_TRANSITION_PROJECT


def obfuscate(plain_text: str) -> str:
    return base64.b64encode(plain_text.encode("utf-8")).decode("ascii")


def deobfuscate(obfuscated_text: str) -> str:
    try:
        return base64.b64decode(obfuscated_text.encode("ascii")).decode("utf-8")
    except ValueError:
        # Corrupt or hand-edited settings file: fall back to no password.
        return ""


def load_settings(settings_path: str) -> StoredSettings:
    try:
        with open(settings_path, "r", encoding="utf-8") as settings_file:
            stored_values = json.load(settings_file)
    except (OSError, ValueError):
        # Missing or unreadable settings are not an error: use the defaults.
        return StoredSettings()

    defaults = StoredSettings()
    return StoredSettings(
        username=stored_values.get("username", defaults.username),
        password=deobfuscate(stored_values.get("password", "")),
        priority=stored_values.get("priority", defaults.priority),
        # "ops" is the key written by earlier versions of this app.
        operations_and_onboarding=stored_values.get(
            "operations_and_onboarding",
            stored_values.get("ops", defaults.operations_and_onboarding),
        ),
        installation=stored_values.get("installation", defaults.installation),
        subject_prefix=stored_values.get("subject_prefix", defaults.subject_prefix),
        subject_prefix_enabled=bool(
            stored_values.get("subject_prefix_enabled", defaults.subject_prefix_enabled)
        ),
        auto_load_enabled=bool(
            stored_values.get("auto_load_enabled", defaults.auto_load_enabled)
        ),
        log_case_show_browser=bool(
            stored_values.get("log_case_show_browser", defaults.log_case_show_browser)
        ),
        transition_project=stored_values.get(
            "transition_project", defaults.transition_project
        ),
    )


def save_settings(settings: StoredSettings, settings_path: str) -> None:
    values_to_store = {
        "username": settings.username,
        "priority": settings.priority,
        "operations_and_onboarding": settings.operations_and_onboarding,
        "installation": settings.installation,
        "subject_prefix": settings.subject_prefix,
        "subject_prefix_enabled": settings.subject_prefix_enabled,
        "auto_load_enabled": settings.auto_load_enabled,
        "log_case_show_browser": settings.log_case_show_browser,
        "transition_project": settings.transition_project,
    }
    if settings.password:
        values_to_store["password"] = obfuscate(settings.password)

    try:
        with open(settings_path, "w", encoding="utf-8") as settings_file:
            json.dump(values_to_store, settings_file, indent=2)
    except OSError:
        # Remembering settings is a convenience, never a reason to fail a run.
        pass


def apply_subject_prefix(prefix: str, subject: str) -> str:
    """Prepend `prefix` to `subject`, ensuring one space separates them.

    The prefix is used verbatim apart from that separator, so "UAT: " and "UAT:"
    both produce "UAT: <subject>".
    """
    if not prefix.strip():
        return subject
    separator = "" if prefix.endswith(" ") else " "
    return f"{prefix}{separator}{subject}"
