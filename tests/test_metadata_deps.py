"""
Sanity checks for optional metadata dependency reporting.

Purpose:
    Ensures the dependency report contains expected keys.

Authorship (git history, mapped to real names):
    Uncommitted/Unknown.
"""

import scripts.metadata as metadata


def test_dependency_report_has_expected_keys() -> None:
    report = metadata.dependency_report()
    expected = {
        "pillow",
        "opencv-python",
        "numpy",
        "easyocr",
        "torch",
        "transformers",
        "geopy",
        "pillow-heif",
    }
    assert expected.issubset(set(report.keys()))
