"""SessionConfig model for runtime session parameters.

This module defines the :class:`SessionConfig` dataclass, which captures the
per-session voice, language, and model selection for a presentation agent
session.

Validation satisfies requirements 8.3 (voice_id recognized or empty ->
"tiffany"), 8.4 (language_locale supported and compatible with the chosen
voice), and 8.5 (errors identify the invalid field and reason). All
``ValueError`` messages raised from here use the ``SessionConfig.<field>``
prefix so the descriptive-message convention is enforced at the model layer
(this also supports task 2.3).

Voice/locale compatibility follows the Nova 2 Sonic supported-voices table
from the design document:

* Polyglot voices (``tiffany``, ``matthew``) accept any supported locale.
* Locale-specific voices must pair with one of their supported locales.
  Notably ``kiara`` and ``arjun`` support both ``en-IN`` and ``hi-IN``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

# ---------------------------------------------------------------------------
# Supported voices / locales
# ---------------------------------------------------------------------------

#: Polyglot voices accept any supported locale.
POLYGLOT_VOICES: frozenset[str] = frozenset({"tiffany", "matthew"})

#: Mapping of locale-specific voice -> set of locales it supports.
#: ``kiara`` and ``arjun`` appear under both ``en-IN`` and ``hi-IN`` in the
#: Nova Sonic voices table, so both locales are valid for those voices.
VOICE_TO_LOCALES: Mapping[str, frozenset[str]] = {
    # English (UK)
    "amy": frozenset({"en-GB"}),
    # English (AU)
    "olivia": frozenset({"en-AU"}),
    # English (IN) + Hindi — shared voices.
    "kiara": frozenset({"en-IN", "hi-IN"}),
    "arjun": frozenset({"en-IN", "hi-IN"}),
    # French
    "ambre": frozenset({"fr-FR"}),
    "florian": frozenset({"fr-FR"}),
    # Italian
    "beatrice": frozenset({"it-IT"}),
    "lorenzo": frozenset({"it-IT"}),
    # German
    "tina": frozenset({"de-DE"}),
    "lennart": frozenset({"de-DE"}),
    # Spanish (US)
    "lupe": frozenset({"es-US"}),
    "carlos": frozenset({"es-US"}),
    # Portuguese (BR)
    "carolina": frozenset({"pt-BR"}),
    "leo": frozenset({"pt-BR"}),
}

#: Union of every locale that appears anywhere in the voices table. Polyglot
#: voices accept any of these; locale-specific voices must match the subset
#: listed in ``VOICE_TO_LOCALES``.
SUPPORTED_LOCALES: frozenset[str] = frozenset(
    {"en-US", "en-GB"}  # en-US is the polyglot default; en-GB from amy.
).union(*VOICE_TO_LOCALES.values())

#: All recognized voice identifiers (polyglot + locale-specific). Handy for
#: validation messages and for callers that want to enumerate options.
ALL_VOICES: frozenset[str] = POLYGLOT_VOICES | frozenset(VOICE_TO_LOCALES.keys())

# Default values (also surfaced as dataclass field defaults below).
_DEFAULT_VOICE_ID: str = "tiffany"
_DEFAULT_LANGUAGE_LOCALE: str = "en-US"
_DEFAULT_SONIC_MODEL_ID: str = "amazon.nova-2-sonic-v1:0"
_DEFAULT_VISION_MODEL_ID: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _sorted(values: frozenset[str]) -> list[str]:
    """Return a stable sorted list for deterministic error messages."""
    return sorted(values)


@dataclass(frozen=True)
class SessionConfig:
    """Runtime configuration for a presentation agent session.

    Attributes:
        voice_id: Nova Sonic voice identifier. An empty string is coerced
            to ``"tiffany"`` per requirement 8.3; any other value must be a
            recognized voice in :data:`ALL_VOICES`.
        language_locale: BCP-47 locale string. Must be in
            :data:`SUPPORTED_LOCALES`. Polyglot voices (``tiffany``,
            ``matthew``) accept any supported locale; locale-specific voices
            must pair with one of their supported locales.
        sonic_model_id: Bedrock model id for Nova Sonic speech-to-speech.
            Defaults to ``"amazon.nova-2-sonic-v1:0"``.
        vision_model_id: Bedrock model id for the vision analysis model.
            Defaults to ``"anthropic.claude-sonnet-4-20250514-v1:0"``.

    The dataclass is frozen so instances are safe to share across the
    keyboard-hook and audio-streaming threads. The empty-string coercion
    for ``voice_id`` uses :func:`object.__setattr__`, which is the standard
    way to mutate a frozen dataclass during ``__post_init__``.
    """

    voice_id: str = _DEFAULT_VOICE_ID
    language_locale: str = _DEFAULT_LANGUAGE_LOCALE
    sonic_model_id: str = _DEFAULT_SONIC_MODEL_ID
    vision_model_id: str = _DEFAULT_VISION_MODEL_ID

    def __post_init__(self) -> None:
        # --- voice_id -----------------------------------------------------
        if not isinstance(self.voice_id, str):
            raise ValueError(
                "SessionConfig.voice_id must be a str "
                f"(got {type(self.voice_id).__name__})"
            )
        # Empty voice_id defaults to "tiffany" per requirement 8.3.
        # Use object.__setattr__ to assign through the frozen dataclass.
        if self.voice_id == "":
            object.__setattr__(self, "voice_id", _DEFAULT_VOICE_ID)

        if self.voice_id not in ALL_VOICES:
            raise ValueError(
                f'SessionConfig.voice_id "{self.voice_id}" is not a '
                f"recognized voice. Supported: {_sorted(ALL_VOICES)}"
            )

        # --- language_locale ---------------------------------------------
        if not isinstance(self.language_locale, str):
            raise ValueError(
                "SessionConfig.language_locale must be a str "
                f"(got {type(self.language_locale).__name__})"
            )
        if self.language_locale not in SUPPORTED_LOCALES:
            raise ValueError(
                f'SessionConfig.language_locale "{self.language_locale}" is '
                f"not a supported locale. Supported: {_sorted(SUPPORTED_LOCALES)}"
            )

        # --- voice <-> locale compatibility ------------------------------
        # Polyglot voices accept any supported locale; the check above is
        # sufficient for them. Locale-specific voices must match one of
        # their supported locales.
        if self.voice_id not in POLYGLOT_VOICES:
            allowed = VOICE_TO_LOCALES[self.voice_id]
            if self.language_locale not in allowed:
                raise ValueError(
                    f'SessionConfig.language_locale "{self.language_locale}" '
                    f'does not match voice "{self.voice_id}". '
                    f'Voice "{self.voice_id}" supports: {_sorted(allowed)}'
                )

        # --- sonic_model_id ----------------------------------------------
        if not isinstance(self.sonic_model_id, str):
            raise ValueError(
                "SessionConfig.sonic_model_id must be a str "
                f"(got {type(self.sonic_model_id).__name__})"
            )
        if self.sonic_model_id == "":
            raise ValueError(
                "SessionConfig.sonic_model_id must be a non-empty string"
            )

        # --- vision_model_id ---------------------------------------------
        if not isinstance(self.vision_model_id, str):
            raise ValueError(
                "SessionConfig.vision_model_id must be a str "
                f"(got {type(self.vision_model_id).__name__})"
            )
        if self.vision_model_id == "":
            raise ValueError(
                "SessionConfig.vision_model_id must be a non-empty string"
            )
