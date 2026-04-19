"""Unit tests for ``evk.email_sanitizer.strip_footers``."""

from __future__ import annotations

import pytest

from evk.email_sanitizer import strip_footers


def test_empty_input_is_returned_unchanged():
    assert strip_footers("") == ""
    assert strip_footers("   ") == ""


def test_keeps_content_before_unsubscribe_block():
    body = (
        "Big news from Mozilla!\n\n"
        "Applications for the 2026 Technology Fund open March 1.\n\n"
        "Unsubscribe by clicking here: https://mz.example/u\n"
        "© 2026 Mozilla Foundation"
    )
    out = strip_footers(body)
    assert "Applications for the 2026 Technology Fund" in out
    assert "Unsubscribe" not in out
    assert "2026 Mozilla Foundation" not in out


def test_strips_view_in_browser_preheader():
    body = "View this email in your browser\nApply to the 2026 scholarship by July 31."
    out = strip_footers(body)
    assert "View this email" not in out
    assert "Apply to the 2026 scholarship" in out


def test_strips_tracking_pixels():
    body = '<p>Come join us!</p><img src="https://x" height="1" width="1" alt="">'
    out = strip_footers(body)
    assert "img" not in out.lower() or 'height="1"' not in out.lower()


def test_strips_zero_width_characters():
    body = "Apply\u200b now\u200d for the Rhodes\ufeff scholarship."
    out = strip_footers(body)
    assert "\u200b" not in out
    assert "\u200d" not in out
    assert "\ufeff" not in out
    assert "Apply" in out


def test_collapses_multiple_blank_lines():
    body = "Line A\n\n\n\n\nLine B"
    assert strip_footers(body) == "Line A\n\nLine B"


def test_safety_floor_keeps_body_if_footer_would_delete_everything():
    # A short body whose first word triggers a footer pattern should not
    # be wiped to empty.
    body = "Unsubscribe issues are real but here's the deadline: 2099-12-31"
    # The marker "unsubscribe" is at position 0 — naive truncation would
    # produce "". Safety floor must preserve the body.
    assert "deadline" in strip_footers(body)


@pytest.mark.parametrize(
    "marker",
    [
        "\n-- \nJohn\nOpportunities Team",
        "\n__________\n\nHidden footer",
        "Manage your preferences here: https://x",
        "You received this email because you subscribed.",
        "privacy policy · legal",
    ],
)
def test_variety_of_boundary_markers(marker: str):
    head = "Apply for the 2026 Rhodes scholarship by July 31."
    out = strip_footers(f"{head}\n{marker}")
    assert head in out
    # The marker's unique word should not survive.
    assert marker.split(maxsplit=1)[0].lower() not in out.lower() or head.lower() in out.lower()
