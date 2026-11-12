import json
from pathlib import Path
import struct
import unittest
import zlib

from run_png_roundtrip import decode_png


SIGNATURE = b"\x89PNG\r\n\x1a\n"
PIXELS = bytes((10, 20, 30, 40, 50, 60, 70, 80,
                20, 40, 60, 80, 90, 120, 150, 180))
# Hand-authored PNG scanlines, not output from the Zig encoder or an inverse
# copy of the decoder. Each row uses the indicated standard PNG filter.
FILTERED = (
    bytes((0, 10, 20, 30, 40, 50, 60, 70, 80, 0, 20, 40, 60, 80, 90, 120, 150, 180)),
    bytes((1, 10, 20, 30, 40, 40, 40, 40, 40, 1, 20, 40, 60, 80, 70, 80, 90, 100)),
    bytes((2, 10, 20, 30, 40, 50, 60, 70, 80, 2, 10, 20, 30, 40, 40, 60, 80, 100)),
    bytes((3, 10, 20, 30, 40, 45, 50, 55, 60, 3, 15, 30, 45, 60, 55, 70, 85, 100)),
    bytes((4, 10, 20, 30, 40, 40, 40, 40, 40, 4, 10, 20, 30, 40, 40, 60, 80, 100)),
)


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))


def image(compressed, width=2, height=2):
    return (SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", compressed) + chunk(b"IEND", b""))


class PngExportOracleTests(unittest.TestCase):
    def test_all_standard_filters_reconstruct_exact_pixels(self):
        for filter_type, scanlines in enumerate(FILTERED):
            with self.subTest(filter=filter_type):
                self.assertEqual(decode_png(image(zlib.compress(scanlines)), 2, 2), PIXELS)

    def test_average_filter_uses_floor_for_odd_neighbors(self):
        scanlines = bytes((3, 1, 3, 5, 7, 3, 4, 7, 10, 13))
        self.assertEqual(decode_png(image(zlib.compress(scanlines), 1, 2), 1, 2),
                         bytes((1, 3, 5, 7, 4, 8, 12, 16)))

    def test_crc_and_zlib_checksum_corruption_are_rejected(self):
        encoded = bytearray(image(zlib.compress(FILTERED[0])))
        encoded[29] ^= 1  # IHDR checksum, not pixel data.
        with self.assertRaises(ValueError):
            decode_png(bytes(encoded), 2, 2)
        compressed = bytearray(zlib.compress(FILTERED[0]))
        compressed[-1] ^= 1
        with self.assertRaises(zlib.error):
            decode_png(image(bytes(compressed)), 2, 2)

    def test_incomplete_trailing_and_oversized_streams_are_rejected(self):
        good = zlib.compress(FILTERED[0])
        for compressed in (good[:-1], good + b"trailing", zlib.compress(FILTERED[0] + b"extra")):
            with self.subTest(compressed=compressed):
                with self.assertRaises(ValueError):
                    decode_png(image(compressed), 2, 2)

    def test_split_idat_is_valid_but_interleaving_and_unknown_critical_chunks_are_not(self):
        compressed = zlib.compress(FILTERED[0])
        prefix = image(compressed)[:33]
        end = chunk(b"IEND", b"")
        a, b = compressed[:3], compressed[3:]
        self.assertEqual(decode_png(prefix + chunk(b"IDAT", a) + chunk(b"IDAT", b) + end, 2, 2), PIXELS)
        for middle in (chunk(b"tEXt", b"note\x00text"), chunk(b"ABCD", b"")):
            with self.subTest(middle=middle):
                with self.assertRaises(ValueError):
                    decode_png(prefix + chunk(b"IDAT", a) + middle + chunk(b"IDAT", b) + end, 2, 2)

    def test_confirmed_nonletter_chunk_type_regression_is_rejected(self):
        fixture = json.loads((Path(__file__).parent / "quality_regressions/png-invalid-chunk-type.json").read_text())
        with self.assertRaises(ValueError):
            decode_png(bytes.fromhex(fixture["png_hex"]), 1, 1)

    def test_wrong_geometry_filter_and_trailing_png_data_are_rejected(self):
        invalid_filter = bytes((5,)) + FILTERED[0][1:]
        for encoded in (image(zlib.compress(FILTERED[0]), 1, 2),
                        image(zlib.compress(invalid_filter)), image(zlib.compress(FILTERED[0])) + b"extra"):
            with self.subTest(encoded=encoded):
                with self.assertRaises(ValueError):
                    decode_png(encoded, 2, 2)


if __name__ == "__main__":
    unittest.main()
