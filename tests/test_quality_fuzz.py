import copy
import json
from pathlib import Path
import tempfile
import unittest

from quality_fuzz import (SIGNATURE, SPEC, candidate_record, ddmin, digest,
                          generated_cases, load_oracle, malformed_types, minimize,
                          neighbors, png, promote, read_record)


class QualityFuzzTests(unittest.TestCase):
    def test_valid_private_ancillary_chunk_is_not_over_rejected(self):
        oracle = load_oracle(Path(__file__).with_name("run_png_roundtrip.py"))
        valid = [{"kind": b"vpAg".hex(), "payload": ""}]
        self.assertEqual(oracle(png(valid), 1, 1), bytes((17, 83, 149, 211)))

    def test_ddmin_retains_required_trigger_and_removes_irrelevant_items(self):
        result = ddmin([0, 1, 2, 3, 4], lambda values: 2 in values, [100])
        self.assertEqual(result, [2])

    def test_shrink_preserves_exact_failure_and_certifies_declared_relation(self):
        case = [{"kind": b"vpAg".hex(), "payload": "01"},
                {"kind": b"t0Xt".hex(), "payload": "010203"}]
        def evaluate(value):
            return {"signature": SIGNATURE if malformed_types(png(value)) else "different-failure"}
        result, proof = minimize(case, evaluate)
        self.assertTrue(proof["one_minimal"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["payload"], "")
        self.assertEqual(evaluate(result)["signature"], SIGNATURE)
        self.assertTrue(all(evaluate(value)["signature"] != SIGNATURE for value in neighbors(result)))
        with self.assertRaises(ValueError):
            minimize([], evaluate)

    def test_budget_exhaustion_does_not_claim_minimality(self):
        case = [{"kind": b"t0Xt".hex(), "payload": "0102030405"}]
        def evaluate(value):
            return {"signature": SIGNATURE if malformed_types(png(value)) else None}
        result, proof = minimize(case, evaluate, max_evaluations=1)
        self.assertFalse(proof["one_minimal"])
        self.assertEqual(proof["evaluations"], 1)
        self.assertEqual(evaluate(result)["signature"], SIGNATURE)

    def test_seeds_and_mutations_are_reproducible(self):
        self.assertEqual(list(generated_cases(7, 16)), list(generated_cases(7, 16)))
        self.assertNotEqual(list(generated_cases(7, 16))[4:], list(generated_cases(8, 16))[4:])

    def test_records_reject_byte_tampering_and_unrecognized_executable_fields(self):
        case = [{"kind": b"v0Ag".hex(), "payload": ""}]
        record = candidate_record(case, {"signature": SIGNATURE, "libpng": {"status": "REJECT"}}, {},
                                  {"one_minimal": True})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            path.write_text(json.dumps(record))
            self.assertEqual(read_record(path)["png_sha256"], digest(png(case)))
            for key, value in (("png_sha256", "0" * 64), ("command", "execute-me")):
                corrupted = copy.deepcopy(record)
                corrupted[key] = value
                path.write_text(json.dumps(corrupted))
                with self.assertRaises(ValueError):
                    read_record(path)
            for key, value in (("observed", {"libpng": "not-an-object"}),
                               ("identity", {"tools": []}), ("original", {})):
                corrupted = copy.deepcopy(record)
                corrupted[key] = value
                path.write_text(json.dumps(corrupted))
                with self.assertRaises(ValueError):
                    read_record(path)

    def test_unconfirmed_promotion_does_not_create_output(self):
        record = {"kind": "candidate"}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "regression"
            for spec, confirmation in ((SPEC, "wrong-hash"), ("wrong-spec", "current-hash")):
                with self.assertRaises(ValueError):
                    promote(record, None, None, "current-hash", spec, confirmation, output)
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
