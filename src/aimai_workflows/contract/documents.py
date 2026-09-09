"""Reading the document behind a URI, and pinning what was read.

The flow keeps only `document_uri` and `text_sha256` in its state, so any node
that needs the text calls `load_document` again. That trade is the interesting
part of the design and it has a cost worth naming: the document is read three
times over a run instead of once. It buys three things — a state small enough
that a checkpoint write stays under a millisecond, no contract text sitting in
a durable store for the days an approval can take, and a *verifiable* claim
that the document the reviewer approved is the document the model assessed.

`verify_unchanged` is where that last claim is cashed in. A human gate lasts
long enough for the file behind the URI to be replaced, and approving findings
computed from an older revision is the failure this whole stage would otherwise
have no way to notice.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

__all__ = [
    "DocumentChanged",
    "DocumentText",
    "load_document",
    "resolve_uri",
    "verify_unchanged",
]


class DocumentChanged(Exception):
    """The bytes behind the URI are not the bytes that were assessed."""

    def __init__(self, uri: str, expected: str, actual: str) -> None:
        super().__init__(
            f"{uri} changed since it was assessed "
            f"(expected sha256 {expected[:12]}, found {actual[:12]})"
        )
        self.uri = uri
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class DocumentText:
    """A document as it was read, with the hash that identifies that reading."""

    uri: str
    text: str
    sha256: str
    byte_size: int


def _document_root() -> Path:
    """The directory a `file:` URI is confined to.

    The approval screen posts a URI that arrives over HTTP, and joining
    attacker-controlled path segments onto a filesystem read is the oldest way
    to turn a review service into a file server. Confining the read to one root
    is two lines here and unavailable once the URI has been resolved.
    """
    return Path(os.getenv("CONTRACT_DOCUMENT_ROOT", "fixtures")).resolve()


def resolve_uri(uri: str) -> Path:
    """Turn a `file:` URI or a bare path into a path inside the document root."""
    parsed = urlparse(uri)
    if parsed.scheme not in ("", "file"):
        raise ValueError(
            f"unsupported document scheme {parsed.scheme!r}; "
            "this stage reads local fixtures only"
        )
    raw = unquote(parsed.path or parsed.netloc or uri)
    root = _document_root()
    candidate = (
        (root / raw.lstrip("/")).resolve()
        if not Path(raw).is_absolute()
        else Path(raw).resolve()
    )
    if not candidate.is_relative_to(root):
        raise ValueError(f"{uri} resolves outside the document root {root}")
    return candidate


def load_document(uri: str) -> DocumentText:
    """Read the document and hash exactly the bytes that were read.

    The hash is taken over the encoded text rather than the file on disk, so a
    document that later arrives from object storage instead of a file produces
    the same hash for the same content.
    """
    path = resolve_uri(uri)
    if not path.is_file():
        raise FileNotFoundError(f"no document at {uri} (resolved to {path})")
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    encoded = text.encode("utf-8")
    return DocumentText(
        uri=uri,
        text=text,
        sha256=hashlib.sha256(encoded).hexdigest(),
        byte_size=len(encoded),
    )


def verify_unchanged(uri: str, expected_sha256: str) -> DocumentText:
    """Re-read the document and refuse to continue if it moved under us."""
    document = load_document(uri)
    if document.sha256 != expected_sha256:
        raise DocumentChanged(uri, expected_sha256, document.sha256)
    return document
