"""Pure-stdlib vector codec tests (Step 5B.2).

Covers the whole public API of ``workflow.services.vector_codec``:
exact known little-endian IEEE-754 float32 bytes, deterministic
encoding, float32-quantized round trips, every dimension boundary,
container/cardinality/value validation, NaN/inf rejection, float32
overflow / huge-int handling, decode byte-length discipline and
non-finite rejection, validate_vector_blob behaviour, and sanitized
error canaries (values/blob content never appear in messages).

No Django database, network, or embedding-client use.
"""

from __future__ import annotations

import struct

import pytest

from workflow.services import vector_codec as vc


def _canary_values():
    return ["SECRET-CANARY-STRING-1234", float("nan"), 999.0]


def _canary_blob():
    return b"TOP-SECRET-BLOB-CANARY-9876"


class TestEncodeKnownBytes:
    def test_single_float_is_exact_little_endian_f32(self):
        assert vc.encode_vector([1.0], dimensions=1) == b"\x00\x00\x80?"

    def test_known_sequence_bytes(self):
        assert vc.encode_vector([0.0, -1.0, 0.5, 2.5], dimensions=4) == (
            b"\x00\x00\x00\x00\x00\x00\x80\xbf\x00\x00\x00?\x00\x00 @"
        )
        assert vc.encode_vector([1.0, 2.0], dimensions=2) == struct.pack("<2f", 1.0, 2.0)

    def test_int_values_packed_as_f32(self):
        assert vc.encode_vector([1, 2, 3], dimensions=3) == struct.pack("<3f", 1.0, 2.0, 3.0)

    def test_negative_zero_and_negative_values(self):
        assert vc.encode_vector([-0.0, -2.5], dimensions=2) == struct.pack(
            "<2f", -0.0, -2.5
        )


class TestEncodeDeterministicRoundtrip:
    def test_deterministic(self):
        values = [0.1, -0.2, 3.5, 100.25]
        assert vc.encode_vector(values, dimensions=4) == vc.encode_vector(
            values, dimensions=4
        )

    def test_float32_quantized_roundtrip(self):
        values = [0.1, 0.2, 1.0, -3.25, 7.0]
        blob = vc.encode_vector(values, dimensions=5)
        decoded = vc.decode_vector(blob, dimensions=5)
        expected = struct.unpack("<5f", struct.pack("<5f", *values))
        assert decoded == expected
        assert isinstance(decoded, tuple)
        assert all(isinstance(v, float) for v in decoded)

    def test_roundtrip_matches_encode_of_quantized_values(self):
        # Encoding then encoding the decoded values yields the same bytes
        # (encode is idempotent on float32-representable inputs).
        values = [0.3, 1.5, -8.75]
        blob = vc.encode_vector(values, dimensions=3)
        decoded = vc.decode_vector(blob, dimensions=3)
        assert vc.encode_vector(decoded, dimensions=3) == blob

    def test_decode_is_deterministic(self):
        blob = vc.encode_vector([1.5, 2.5, 3.5], dimensions=3)
        assert vc.decode_vector(blob, dimensions=3) == vc.decode_vector(blob, dimensions=3)


class TestEncodeContainers:
    def test_list_and_tuple_equivalent(self):
        assert vc.encode_vector([1.0, 2.0], dimensions=2) == vc.encode_vector(
            (1.0, 2.0), dimensions=2
        )

    @pytest.mark.parametrize(
        "values",
        [
            "ab",  # str is a container but never accepted
            {"a": 1.0},  # dict
            {1.0},  # set
            5,  # scalar int
            1.5,  # scalar float
            None,
            (x for x in [1.0]),  # generator, not a list/tuple
        ],
    )
    def test_wrong_container_rejected(self, values):
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector(values, dimensions=2)
        assert excinfo.value.code == vc.INVALID_VALUES

    def test_values_never_accepted_for_str_input_even_with_dimension_one(self):
        with pytest.raises(vc.VectorCodecError):
            vc.encode_vector("ab", dimensions=2)

    def test_empty_container_rejected(self):
        for empty in ([], ()):
            with pytest.raises(vc.VectorCodecError) as excinfo:
                vc.encode_vector(empty, dimensions=1)
            assert excinfo.value.code == vc.INVALID_VALUES

    def test_wrong_cardinality_rejected(self):
        for values in ([1.0], [1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0]):
            with pytest.raises(vc.VectorCodecError) as excinfo:
                vc.encode_vector(values, dimensions=2)
            assert excinfo.value.code == vc.INVALID_VALUES

    def test_bool_values_rejected(self):
        for values in ([True], [1.0, False], [False, True]):
            with pytest.raises(vc.VectorCodecError) as excinfo:
                vc.encode_vector(values, dimensions=len(values))
            assert excinfo.value.code == vc.INVALID_VALUES

    def test_non_numeric_values_rejected(self):
        for values in (["x"], [None], [1 + 2j], ["1.0"], [b"1"]):
            with pytest.raises(vc.VectorCodecError) as excinfo:
                vc.encode_vector(values, dimensions=len(values))
            assert excinfo.value.code == vc.INVALID_VALUES

    def test_float_subclass_not_an_exact_float(self):
        class MyFloat(float):
            pass

        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector([MyFloat(1.5)], dimensions=1)
        assert excinfo.value.code == vc.INVALID_VALUES


class TestEncodeNonFiniteAndOverflow:
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_values_rejected_before_packing(self, value):
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector([value, 1.0], dimensions=2)
        assert excinfo.value.code == vc.INVALID_VALUES

    @pytest.mark.parametrize("value", [1e39, -1e39, 10**400, -(10**400), 2**2000])
    def test_float32_overflow_and_huge_int_rejected(self, value):
        # struct.pack raises OverflowError/struct.error for these; the
        # codec must convert to a sanitized VectorCodecError, never leak
        # the raw exception.
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector([value], dimensions=1)
        assert excinfo.value.code == vc.INVALID_VALUES

    def test_max_float32_value_encodes(self):
        max_f32 = struct.unpack("<f", struct.pack("<f", 3.4028234663852886e38))[0]
        blob = vc.encode_vector([max_f32], dimensions=1)
        assert vc.decode_vector(blob, dimensions=1) == (max_f32,)


class TestDimensions:
    def test_dimension_one_accepted(self):
        assert vc.encode_vector([-1.5], dimensions=1) == struct.pack("<f", -1.5)

    def test_max_dimension_accepted(self):
        values = tuple(0.0 for _ in range(vc.MAX_DIMENSION))
        blob = vc.encode_vector(values, dimensions=vc.MAX_DIMENSION)
        assert len(blob) == vc.MAX_DIMENSION * 4
        decoded = vc.decode_vector(blob, dimensions=vc.MAX_DIMENSION)
        assert len(decoded) == vc.MAX_DIMENSION
        assert decoded == tuple(0.0 for _ in range(vc.MAX_DIMENSION))

    @pytest.mark.parametrize(
        "dimensions",
        [0, -1, vc.MAX_DIMENSION + 1, 16385, True, False, "8", 8.0, None, 1.5],
    )
    def test_invalid_dimensions_rejected(self, dimensions):
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector([1.0], dimensions=dimensions)
        assert excinfo.value.code == vc.INVALID_DIMENSION

    def test_all_apis_validate_dimensions(self):
        blob = vc.encode_vector([1.0], dimensions=1)
        for api in (lambda: vc.decode_vector(blob, dimensions=0),
                    lambda: vc.validate_vector_blob(blob, dimensions=vc.MAX_DIMENSION + 1),
                    lambda: vc.decode_vector(blob, dimensions=True)):
            with pytest.raises(vc.VectorCodecError) as excinfo:
                api()
            assert excinfo.value.code == vc.INVALID_DIMENSION

    def test_dimension_mismatch_roundtrip_length_is_rejected(self):
        blob = vc.encode_vector([1.0, 2.0, 3.0], dimensions=3)
        # Correct dimension decodes; every other dimension has the wrong
        # byte length and is rejected.
        assert vc.decode_vector(blob, dimensions=3) == (1.0, 2.0, 3.0)
        for wrong in (1, 2, 4, vc.MAX_DIMENSION):
            with pytest.raises(vc.VectorCodecError) as excinfo:
                vc.decode_vector(blob, dimensions=wrong)
            assert excinfo.value.code == vc.INVALID_BLOB


class TestDecode:
    def test_decode_returns_float_tuple(self):
        blob = struct.pack("<3f", 1.0, 2.0, 3.0)
        assert vc.decode_vector(blob, dimensions=3) == (1.0, 2.0, 3.0)

    @pytest.mark.parametrize(
        "blob",
        [
            "abc",
            b"",
            bytearray(b""),
            bytearray(b"\x00\x00\x80?"),
            None,
            memoryview(b"\x00\x00\x80?"),
            [1.0],
            5,
        ],
    )
    def test_wrong_container_or_empty_rejected(self, blob):
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.decode_vector(blob, dimensions=1)
        assert excinfo.value.code == vc.INVALID_BLOB

    def test_short_blob_rejected(self):
        blob = struct.pack("<2f", 1.0, 2.0)  # 8 bytes, not 12
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.decode_vector(blob, dimensions=3)
        assert excinfo.value.code == vc.INVALID_BLOB

    def test_long_blob_with_trailing_bytes_rejected(self):
        blob = struct.pack("<2f", 1.0, 2.0) + b"\x00\x00\x00"
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.decode_vector(blob, dimensions=2)
        assert excinfo.value.code == vc.INVALID_BLOB

    def test_trailing_single_byte_rejected(self):
        blob = struct.pack("<f", 1.0) + b"\x00"
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.decode_vector(blob, dimensions=1)
        assert excinfo.value.code == vc.INVALID_BLOB

    @pytest.mark.parametrize(
        "blob",
        [struct.pack("<f", float("inf")), struct.pack("<f", float("-inf")),
         struct.pack("<f", float("nan"))],
    )
    def test_non_finite_blob_rejected(self, blob):
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.decode_vector(blob, dimensions=1)
        assert excinfo.value.code == vc.INVALID_BLOB

    def test_non_finite_in_middle_rejected(self):
        blob = struct.pack("<3f", 1.0, float("inf"), 2.0)
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.decode_vector(blob, dimensions=3)
        assert excinfo.value.code == vc.INVALID_BLOB


class TestValidateVectorBlob:
    def test_valid_blob_returns_none(self):
        blob = vc.encode_vector([1.0, 2.0], dimensions=2)
        assert vc.validate_vector_blob(blob, dimensions=2) is None

    def test_accepts_exact_bytes_only(self):
        blob = vc.encode_vector([1.0], dimensions=1)
        with pytest.raises(vc.VectorCodecError):
            vc.validate_vector_blob(bytearray(blob), dimensions=1)
        assert vc.validate_vector_blob(bytes(blob), dimensions=1) is None

    def test_length_mismatch_rejected(self):
        blob = vc.encode_vector([1.0, 2.0, 3.0, 4.0], dimensions=4)
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.validate_vector_blob(blob, dimensions=3)
        assert excinfo.value.code == vc.INVALID_BLOB

    def test_non_finite_rejected(self):
        blob = struct.pack("<f", float("nan"))
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.validate_vector_blob(blob, dimensions=1)
        assert excinfo.value.code == vc.INVALID_BLOB

    def test_accepts_max_dimension_blob(self):
        blob = vc.encode_vector([0.0] * vc.MAX_DIMENSION, dimensions=vc.MAX_DIMENSION)
        assert vc.validate_vector_blob(blob, dimensions=vc.MAX_DIMENSION) is None


class TestErrorSanitization:
    def test_encode_error_never_contains_values(self):
        values = [float("nan"), "SECRET-CANARY-STRING-1234", 999.0]
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector(values, dimensions=len(values))
        message = str(excinfo.value)
        assert "SECRET-CANARY-STRING-1234" not in message
        assert "999.0" not in message
        assert repr(values) not in message

    def test_cardinality_error_never_echoes_values(self):
        values = ["SECRET-CANARY-STRING-1234"]
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector(values, dimensions=3)
        assert "SECRET-CANARY-STRING-1234" not in str(excinfo.value)

    def test_overflow_error_never_contains_value(self):
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector([10**400], dimensions=1)
        assert str(excinfo.value) == "values could not be packed as float32"

    def test_decode_error_never_contains_blob_content(self):
        blob = b"\x00\x00\x80?" + _canary_blob()
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.decode_vector(blob, dimensions=1)
        message = str(excinfo.value)
        assert "TOP-SECRET-BLOB-CANARY-9876" not in message
        assert message.isascii()

    def test_dimension_error_never_echoes_dimension_value(self):
        with pytest.raises(vc.VectorCodecError) as excinfo:
            vc.encode_vector([1.0], dimensions=vc.MAX_DIMENSION + 1)
        assert "16385" not in str(excinfo.value)

    def test_codes_are_stable_and_error_is_value_error(self):
        assert vc.INVALID_DIMENSION == "invalid_dimension"
        assert vc.INVALID_VALUES == "invalid_values"
        assert vc.INVALID_BLOB == "invalid_blob"
        assert vc.MAX_DIMENSION == 16384
        assert issubclass(vc.VectorCodecError, ValueError)
