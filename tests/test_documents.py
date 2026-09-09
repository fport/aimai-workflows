"""Reading documents: the hash that pins them, and the root that confines them."""

from __future__ import annotations

import pytest

from aimai_workflows.contract import load_document, verify_unchanged
from aimai_workflows.contract.documents import DocumentChanged, resolve_uri


def test_the_hash_is_over_the_text_not_the_file() -> None:
    """Same content, same hash — including when the bytes arrive another way."""
    document = load_document("low_risk.txt")
    again = load_document("file://low_risk.txt")

    assert document.sha256 == again.sha256
    assert document.byte_size == len(document.text.encode("utf-8"))


def test_a_changed_document_is_refused(document_root) -> None:
    """The gate can be open for days; the file behind the URI can move.

    Approving findings computed from a superseded revision is the failure this
    check exists to make impossible.
    """
    original = load_document("high_risk.txt")
    (document_root / "high_risk.txt").write_text(
        original.text + "\n10. LATE ADDITION\n\n10.1 Everything above is void.\n"
    )

    with pytest.raises(DocumentChanged) as error:
        verify_unchanged("high_risk.txt", original.sha256)

    assert error.value.expected == original.sha256
    assert error.value.actual != original.sha256


def test_paths_outside_the_root_are_rejected() -> None:
    """The URI arrives over HTTP from the approval screen."""
    with pytest.raises(ValueError, match="outside the document root"):
        resolve_uri("../../etc/passwd")


def test_remote_schemes_are_rejected() -> None:
    """A scheme this stage cannot verify is not silently downgraded to a read."""
    with pytest.raises(ValueError, match="unsupported document scheme"):
        resolve_uri("https://example.invalid/contract.pdf")


def test_a_missing_document_names_the_uri() -> None:
    with pytest.raises(FileNotFoundError, match="no document at"):
        load_document("does_not_exist.txt")
