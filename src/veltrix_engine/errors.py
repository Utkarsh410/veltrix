"""
The engine's errors. Each layer raises its own kind, all under VeltrixError,
so a caller can catch "anything Veltrix said is wrong with this input" in one
place and still tell a damaged file from a version mismatch.

They subclass ValueError too: code written before they existed catches them
as it always did.
"""

from __future__ import annotations


class VeltrixError(Exception):
    """Something about the input that Veltrix can explain to the user."""


class FileFormatError(VeltrixError, ValueError):
    """A file that cannot be read as the format it claims, or as any format."""


class MappingError(VeltrixError, ValueError):
    """A file whose columns cannot be made into the channels the analysis needs."""


class VersionError(VeltrixError, ValueError):
    """A saved file from a release this one cannot read, or must not overwrite."""
