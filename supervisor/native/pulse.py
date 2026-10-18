"""Stand-ins for pulsectl on macOS, which has no PulseAudio.

pulsectl loads libpulse when imported, so it cannot even be imported here.
Every use raises PulseError, which the sound module already handles.
"""


class PulseError(Exception):
    """PulseAudio error."""


class PulseIndexError(PulseError):
    """PulseAudio index error."""


class PulseOperationFailed(PulseError):
    """PulseAudio operation failed."""


class Pulse:
    """PulseAudio client that is never available."""

    def __init__(self, *args, **kwargs) -> None:
        """Refuse to connect."""
        raise PulseError("PulseAudio is not available on macOS")
