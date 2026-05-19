"""SlideData model for representing a single slide extracted from a deck.

This module defines an immutable dataclass used to pass slide content between
the extraction layer (PowerPoint COM / python-pptx) and the vision analysis
layer. Validation errors raised from this module use the ``SlideData.<field>``
prefix so that the descriptive-message requirement from task 2.3 is satisfied
by this module directly.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

# PNG file signature (first 8 bytes of every PNG file).
_PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"

# JPEG file signature (first 3 bytes of every JPEG file).
_JPEG_SIGNATURE: bytes = b"\xFF\xD8\xFF"


@dataclass(frozen=True)
class SlideData:
    """Immutable representation of a single slide.

    Attributes:
        index: Zero-based position of the slide within the deck.
        image_base64: Base64-encoded PNG or JPEG rendering of the slide.
        speaker_notes: Slide speaker notes; empty string when no notes exist.

    Frozen to keep instances safe to share across threads (e.g. the keyboard
    hook thread and the vision-call worker).
    """

    index: int
    image_base64: str
    speaker_notes: str = ""

    def __post_init__(self) -> None:
        # Validate index: must be a non-negative integer.
        # Note: bool is a subclass of int in Python; reject it explicitly.
        if not isinstance(self.index, int) or isinstance(self.index, bool):
            raise ValueError(
                f"SlideData.index must be an int (got {type(self.index).__name__})"
            )
        if self.index < 0:
            raise ValueError(
                f"SlideData.index must be >= 0 (got {self.index})"
            )

        # Validate image_base64: must be a string that decodes as base64 and
        # whose decoded bytes begin with the PNG or JPEG signature.
        if not isinstance(self.image_base64, str):
            raise ValueError(
                "SlideData.image_base64 is not valid base64-encoded image data: "
                f"expected str, got {type(self.image_base64).__name__}"
            )

        try:
            decoded = base64.b64decode(self.image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                "SlideData.image_base64 is not valid base64-encoded image data: "
                f"base64 decode failed ({exc})"
            ) from exc

        if not (decoded.startswith(_PNG_SIGNATURE) or decoded.startswith(_JPEG_SIGNATURE)):
            raise ValueError(
                "SlideData.image_base64 is not valid base64-encoded image data: "
                "decoded bytes do not start with a PNG or JPEG signature"
            )

        # Validate speaker_notes: must be a string (default is "").
        if not isinstance(self.speaker_notes, str):
            raise ValueError(
                "SlideData.speaker_notes must be a str "
                f"(got {type(self.speaker_notes).__name__})"
            )

    @property
    def image_format(self) -> str:
        """Return ``"png"`` or ``"jpeg"`` based on the base64 prefix.

        Avoids a full base64 decode — PNG base64 always starts with
        ``iVBOR`` and JPEG with ``/9j/``.
        """
        if self.image_base64.startswith("iVBOR"):
            return "png"
        return "jpeg"
