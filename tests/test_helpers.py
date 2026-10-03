"""Pure helpers from the automation and the window: dates, case numbers and
the colour of a progress line."""

import pytest

import sc_case_logger
import sc_case_reader
import sc_gui


@pytest.mark.parametrize(
    "iso_date, expected",
    [
        ("2026-07-13", "13. Jul 2026"),
        ("2026-07-03", "3. Jul 2026"),  # no leading zero: the portal rejects "03."
        ("2024-12-31", "31. Dec 2024"),
        ("", ""),
        ("13/07/2026", ""),  # the form the portal refused, per the notes
        ("2026-02-30", ""),
    ],
)
def test_dates_are_written_the_way_the_portal_accepts(iso_date, expected):
    assert sc_case_logger.to_portal_date(iso_date) == expected


def test_the_month_name_does_not_depend_on_the_locale(monkeypatch):
    # strftime("%b") would follow the process locale. The portal only accepts English.
    assert sc_case_logger.PORTAL_MONTH_ABBREVIATIONS[4] == "May"
    assert len(sc_case_logger.PORTAL_MONTH_ABBREVIATIONS) == 12


@pytest.mark.parametrize(
    "typed, expected",
    [("123456", "00123456"), (" 00123456 ", "00123456"), ("Case #123456", "00123456")],
)
def test_case_numbers_are_padded_to_eight_digits(typed, expected):
    assert sc_case_reader.normalise_case_number(typed) == expected


@pytest.mark.parametrize(
    "typed",
    ["", "   ", "no digits here", "https://supportportal.simcorp.com/s/case/500Tb00001XXXXXXXXX/slug"],
)
def test_input_that_is_not_a_case_number_is_refused_with_a_reason(typed):
    with pytest.raises(sc_case_reader.CaseReaderError):
        sc_case_reader.normalise_case_number(typed)


@pytest.mark.parametrize(
    "line, tag",
    [
        ("Case logged successfully.", "progress_success"),
        ("  comment posted", "progress_success"),
        # Failure words are checked first, so this is red even though it says "post".
        ("! could not post comment", "progress_error"),
        ("Refresh skipped: a read is already running", "progress_warning"),
        ("Opening the case list...", ""),
    ],
)
def test_progress_lines_are_coloured_by_what_they_report(line, tag):
    assert sc_gui.classify_progress_line(line) == tag
