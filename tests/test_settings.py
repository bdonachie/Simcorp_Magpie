"""Stored settings, the subject prefix and the project picker.

These are the pieces that decide what reaches the portal and what survives an
upgrade, and none of them needs a browser.
"""

import json

import pytest

import sc_settings
from sc_settings import StoredSettings


@pytest.mark.parametrize(
    "prefix, subject, expected",
    [
        ("UAT: ", "Order stuck", "UAT: Order stuck"),
        ("UAT:", "Order stuck", "UAT: Order stuck"),
        ("", "Order stuck", "Order stuck"),
        ("   ", "Order stuck", "Order stuck"),
    ],
)
def test_prefix_is_separated_by_exactly_one_space(prefix, subject, expected):
    assert sc_settings.apply_subject_prefix(prefix, subject) == expected


def test_every_setting_survives_a_save_and_load(tmp_path):
    path = str(tmp_path / "settings.json")
    original = StoredSettings(
        username="someone@example.com",
        password="s3cret",
        priority="2-High",
        operations_and_onboarding="Transition",
        installation="DEV / 26.04",
        subject_prefix="UAT: ",
        subject_prefix_enabled=True,
        auto_load_enabled=False,
        log_case_show_browser=True,
        transition_project="Client - Customer Care (2)",
    )

    sc_settings.save_settings(original, path)

    assert sc_settings.load_settings(path) == original


def test_the_password_is_obfuscated_on_disk(tmp_path):
    path = tmp_path / "settings.json"

    sc_settings.save_settings(StoredSettings(username="u", password="s3cret"), str(path))

    raw = path.read_text(encoding="utf-8")
    assert "s3cret" not in raw
    assert json.loads(raw)["password"] == sc_settings.obfuscate("s3cret")


def test_no_password_key_is_written_when_none_is_remembered(tmp_path):
    path = tmp_path / "settings.json"

    sc_settings.save_settings(StoredSettings(), str(path))

    assert "password" not in json.loads(path.read_text(encoding="utf-8"))


def test_a_missing_file_gives_the_defaults(tmp_path):
    assert sc_settings.load_settings(str(tmp_path / "absent.json")) == StoredSettings()


def test_a_corrupt_file_gives_the_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")

    assert sc_settings.load_settings(str(path)) == StoredSettings()


def test_a_corrupt_password_falls_back_to_blank():
    assert sc_settings.deobfuscate("!!! not base64") == ""


def test_the_key_written_by_earlier_versions_is_still_read(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"ops": "Transition"}), encoding="utf-8")

    assert sc_settings.load_settings(str(path)).operations_and_onboarding == "Transition"


def test_keys_left_behind_by_removed_features_are_ignored(tmp_path):
    # An install upgraded from a build that had extra options must still load.
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"retired_option_enabled": True, "priority": "2-High"}), encoding="utf-8")

    assert sc_settings.load_settings(str(path)).priority == "2-High"


def test_a_project_is_selected_by_its_portal_index_not_its_label():
    # Two projects share a label on the portal, so only the index is unambiguous.
    assert sc_settings.project_choice_value("Client - Customer Care") == "ChoiceListCollectionCIProjects.2"
    assert sc_settings.project_choice_value("Client - Customer Care (2)") == "ChoiceListCollectionCIProjects.6"
    assert sc_settings.project_choice_value("not a project") == ""


def test_the_default_project_is_one_the_picker_offers():
    assert sc_settings.DEFAULT_TRANSITION_PROJECT in sc_settings.project_labels()
