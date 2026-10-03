"""Build the real window, headless, and check what is on it.

This catches the class of mistake no unit test sees: a widget or attribute that
one part of the window still reaches for after another part stopped creating it.
"""

import tkinter as tk

import pytest

import sc_gui
import sc_settings


@pytest.fixture
def window(tmp_path):
    paths = sc_settings.ApplicationPaths(str(tmp_path))
    try:
        application = sc_gui.CaseLoggerApplication(
            run_automation=lambda *arguments, **keywords: None, settings_paths=paths
        )
    except tk.TclError as error:  # no display available
        pytest.skip(f"Tk could not open a window: {error}")
    application.withdraw()
    application.update_idletasks()
    yield application
    application.destroy()


def test_the_window_opens_with_the_application_title(window):
    assert window.title() == "Magpie - SimCorp case manager"


def test_a_fresh_install_offers_the_default_prefix_and_project(window):
    assert window.subject_prefix_entry.get() == sc_settings.DEFAULT_SUBJECT_PREFIX
    assert window.project_combobox.get() == sc_settings.DEFAULT_TRANSITION_PROJECT


def test_the_settings_the_window_would_save_are_complete(window):
    stored = window._current_settings()

    assert stored.priority == sc_settings.DEFAULT_PRIORITY
    assert stored.installation == sc_settings.DEFAULT_INSTALLATION
    assert stored.transition_project in sc_settings.project_labels()
    # Nothing remembered unless "Remember on this PC" is ticked.
    assert stored.username == "" and stored.password == ""


def test_the_project_picker_only_appears_for_transition(window):
    window.operations_combobox.set("Operation")
    window._update_project_visibility()
    assert not window.project_combobox.winfo_manager()

    window.operations_combobox.set("Transition")
    window._update_project_visibility()
    assert window.project_combobox.winfo_manager() == "grid"
