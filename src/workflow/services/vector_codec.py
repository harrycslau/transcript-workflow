"""Portable deterministic float32 vector codec (Step 5B.2).

The versioned embedding store writes and reads vectors ONLY through this
pure-stdlib module. The wire/store format is deliberately simple and
portable:

- ``encode_vector`` packs values as raw little-endian IEEE-754 float32
  via ``struct.pack(f"<{dimensions}f", ...)`` — no header, no pickle, no
  JSON, no size prefix. The byte length alone implies the dimension
  (``dimensions * 4``).
- ``decode_vector``/``validate_vector_blob`` accept only the exact byte
  length for the declared dimension, so the stored BLOB is self-describing
  only through its length plus the generation's ``dimensions`` column.
- Round trips are deterministic and float32-quantized: a Python ``float``
  that is not exactly representable as float32 comes back as the nearest
  float32 value.

Every public function enforces the dimension contract (exact ``int``,
never ``bool``, in ``1..MAX_DIMENSION``) and validates values/blob content
before producing or accepting bytes. Errors are sanitized
:class:`VectorCodecError` (a ``ValueError``) carrying a stable ``code``;
messages are fixed static strings that never include input values, blob
content, or paths.

``MAX_DIMENSION`` is the ONE runtime home of the dimension cap:
``workflow.services.embedding_client`` imports it so
``embedding_client.MAX_DIMENSION`` stays available/compatible.
"""

from __future__ import annotations

import math
import struct

# The single runtime home of the hard dimension cap. Embedding clients,
# the vector codec and (later) generation writers/status all agree on
# this value.
MAX_DIMENSION = 16384

# Stable sanitized error codes (never raw exceptions, values or content).
INVALID_DIMENSION = "invalid_dimension"
INVALID_VALUES = "invalid_values"
INVALID_BLOB = "invalid_blob"


class VectorCodecError(ValueError):
    """Sanitized vector codec failure. ``code`` is stable and messages are
    fixed static strings that never include values or blob content."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _validate_dimensions(dimensions) -> int:
    """Validate a dimension argument: exact int (never bool), 1..MAX.

    Returns the validated dimension for packing arithmetic. Errors never
    echo the offending value.
    """
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise VectorCodecError(INVALID_DIMENSION, "dimensions must be an integer")
    if dimensions < 1 or dimensions > MAX_DIMENSION:
        raise VectorCodecError(
            INVALID_DIMENSION, "dimensions must be between 1 and the maximum"
        )
    return dimensions


def _is_finite(value) -> bool:
    # Exact floats may be NaN/inf. Exact ints are always finite, but a
    # huge int cannot be converted to float (OverflowError) and is left
    # for the pack step to reject with a sanitized error.
    if type(value) is float:
        return value == value and not math.isinf(value)
    return True


def encode_vector(values, *, dimensions) -> bytes:
    """Encode ``values`` as raw little-endian IEEE-754 float32 bytes.

    Accepts exactly a list or tuple of exact ``int``/``float`` values
    (``bool`` and subclasses rejected), nonempty, with cardinality exactly
    ``dimensions`` (1..:data:`MAX_DIMENSION`). Every value must be finite
    before packing; packing overflow (including huge ints and finite
    floats beyond the float32 range) and struct failures raise a sanitized
    :class:`VectorCodecError`; the packed result is verified to remain
    finite.
    """
    _validate_dimensions(dimensions)
    if not isinstance(values, (list, tuple)) or isinstance(values, str):
        raise VectorCodecError(INVALID_VALUES, "values must be a list or tuple")
    if len(values) == 0:
        raise VectorCodecError(INVALID_VALUES, "values must not be empty")
    if len(values) != dimensions:
        raise VectorCodecError(
            INVALID_VALUES, "values cardinality does not match the declared dimensions"
        )
    for value in values:
        if isinstance(value, bool) or type(value) not in (int, float):
            raise VectorCodecError(
                INVALID_VALUES, "every value must be an exact int or float"
            )
        if not _is_finite(value):
            raise VectorCodecError(INVALID_VALUES, "every value must be finite")
    try:
        blob = struct.pack(f"<{dimensions}f", *values)
    except (OverflowError, struct.error, TypeError, ValueError):
        raise VectorCodecError(
            INVALID_VALUES, "values could not be packed as float32"
        ) from None
    # Verify the packed float32 payload is finite (defensive: finite
    # inputs cannot overflow here, but the store format must never hold a
    # non-finite value).
    if not _all_finite_in_blob(blob):
        raise VectorCodecError(INVALID_VALUES, "values could not be packed as float32")
    return blob


def _all_finite_in_blob(blob: bytes) -> bool:
    values = struct.unpack(f"<{len(blob) // 4}f", blob)
    return all(_is_finite(value) for value in values)


def decode_vector(blob, *, dimensions) -> tuple[float, ...]:
    """Decode a raw little-endian float32 BLOB into a tuple of floats.

    Accepts exact ``bytes`` of exactly ``dimensions * 4`` bytes (which
    makes empty, short, long and trailing-byte payloads all invalid) and
    rejects any non-finite decoded value. Deterministic: the same BLOB
    always decodes to the same tuple.
    """
    _validate_dimensions(dimensions)
    values = _decode_blob(blob, dimensions)
    return tuple(values)


def validate_vector_blob(blob, *, dimensions) -> None:
    """Validate a stored vector BLOB for ``dimensions`` without decoding.

    Raises :class:`VectorCodecError` for a wrong container type, a length
    other than exactly ``dimensions * 4`` bytes (empty included), or any
    non-finite decoded value. Returns ``None`` when the blob is a valid
    stored vector.
    """
    _validate_dimensions(dimensions)
    _decode_blob(blob, dimensions)
    return None


def _decode_blob(blob, dimensions: int) -> list[float]:
    if type(blob) is not bytes:
        raise VectorCodecError(INVALID_BLOB, "vector blob must be bytes")
    expected = dimensions * 4
    if len(blob) == 0:
        raise VectorCodecError(INVALID_BLOB, "vector blob must not be empty")
    if len(blob) != expected:
        raise VectorCodecError(
            INVALID_BLOB, "vector blob length does not match the declared dimensions"
        )
    try:
        values = struct.unpack(f"<{dimensions}f", blob)
    except struct.error:
        raise VectorCodecError(
            INVALID_BLOB, "vector blob could not be unpacked as float32"
        ) from None
    for value in values:
        if not _is_finite(value):
            raise VectorCodecError(INVALID_BLOB, "vector blob contains a non-finite value")
    return list(values)
