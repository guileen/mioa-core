"""Evidence-classification tests; never substitute for executing native probes."""
import json
import unittest

from quality_native import (
    ASSETS, BROWSER, CONTRACTS, MUTATIONS, SENTINEL, apply_mutation,
    baseline_inventory, entity_cases, entity_inventory, mutation_verdict, sha256,
)


class NativeQualityEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.mutation = MUTATIONS[0]
        self.compiled = {"returncode": 0, "timed_out": False}
        self.failed = {"returncode": 1, "timed_out": False}
        self.evidence = f"1/1 browser.entities.test.{self.mutation['test']}...expected 824, found 0\nFAIL (TestExpectedEqual)\n0 passed; 0 skipped; 1 failed.\n"

    def test_only_matching_semantic_assertion_kills(self):
        self.assertEqual(mutation_verdict(self.compiled, self.failed, self.mutation, self.evidence), "KILLED")
        unrelated = self.evidence.replace(self.mutation["test"], "unrelated test")
        self.assertEqual(mutation_verdict(self.compiled, self.failed, self.mutation, unrelated), "INVALID")
        wrong_error = self.evidence.replace("TestExpectedEqual", "OutOfMemory")
        self.assertEqual(mutation_verdict(self.compiled, self.failed, self.mutation, wrong_error), "INVALID")

    def test_compile_failures_and_timeouts_never_kill(self):
        for compiled in ({"returncode": 1}, {"returncode": -9}, {"returncode": None, "timed_out": True}):
            self.assertEqual(mutation_verdict(compiled, self.failed, self.mutation, self.evidence), "INVALID")

    def test_test_crashes_and_timeouts_never_kill(self):
        for tested in ({"returncode": -6}, {"returncode": 134}, {"returncode": 127}, {"returncode": None, "timed_out": True}, {"returncode": 1, "timed_out": True}):
            self.assertEqual(mutation_verdict(self.compiled, tested, self.mutation, self.evidence), "INVALID")

    def test_zero_test_exit_is_survived_not_killed(self):
        self.assertEqual(mutation_verdict(self.compiled, {"returncode": 0}, self.mutation, self.evidence), "SURVIVED")

    def test_failure_from_next_test_cannot_kill_intended_test(self):
        text = f"1/2 browser.entities.test.{self.mutation['test']}...OK\n2/2 unrelated...FAIL (TestExpectedEqual)\n"
        self.assertEqual(mutation_verdict(self.compiled, self.failed, self.mutation, text), "INVALID")

    def test_only_cleanup_mutant_accepts_intended_leak_evidence(self):
        leak = next(m for m in MUTATIONS if m.get("leak"))
        text = f"1/1 browser.png.test.{leak['test']}...OK\nerror: memory address 0x123 leaked\n1 tests leaked memory.\n"
        self.assertEqual(mutation_verdict(self.compiled, self.failed, leak, text), "KILLED")
        text = text.replace(leak["test"], self.mutation["test"])
        self.assertEqual(mutation_verdict(self.compiled, self.failed, self.mutation, text), "INVALID")

    def test_vendored_assets_match_pinned_manifest(self):
        manifest = json.loads((ASSETS / "manifest.json").read_text())
        for name, item in manifest["files"].items():
            with self.subTest(asset=name):
                self.assertEqual(sha256(ASSETS / name), item["sha256"])
                self.assertIn("/" + manifest["corpora"][0]["revision"] + "/" if name.startswith("WHATWG") or name == "entities.json" else "/" + manifest["corpora"][1]["revision"] + "/", item["url"])

    def test_community_oracle_separates_exact_prefix_and_dead_end(self):
        table = json.loads((ASSETS / "entities.json").read_text())
        names, expected, counts = entity_cases(table)
        self.assertEqual(counts["exact"], len(table))
        self.assertGreater(counts["exact"], 2000)
        self.assertGreater(counts["prefix_only"], 0)
        lookup = dict(zip(names, expected))
        self.assertEqual(lookup["NotEqualTilde;"], {"status": 2, "codepoints": [0x2242, 0x338]})
        self.assertEqual(lookup["amp"], {"status": 2, "codepoints": [38, 0]})
        self.assertEqual(lookup["apos"], {"status": 1, "codepoints": SENTINEL})
        self.assertEqual(lookup["Amp;"], {"status": 0, "codepoints": SENTINEL})
        self.assertEqual(lookup[""], {"status": 1, "codepoints": SENTINEL})

    def test_declared_mutations_have_individual_semantic_contracts(self):
        self.assertGreaterEqual(len(MUTATIONS), 10)
        self.assertEqual(len({m["id"] for m in MUTATIONS}), len(MUTATIONS))
        self.assertEqual({m["module"] for m in MUTATIONS}, {"entities.zig", "png.zig"})
        for mutation in MUTATIONS:
            self.assertTrue(mutation["test"])
            self.assertTrue(mutation["errors"])
            self.assertNotEqual(mutation["old"], mutation["new"])

    def test_fixed_native_inventory_includes_both_import_entries(self):
        contracts = json.loads(CONTRACTS.read_text())
        required = contracts["required_native_tests"]
        self.assertEqual(contracts["required_native_test_count"], 13)
        self.assertEqual(len(required), 13)
        self.assertEqual(
            {entry["name"] for entry in required if entry["id"].startswith("native.import-")},
            {"native_quality_tests.test_0", "native_tests.test_0"},
        )
        text = "\n".join(f"{i}/13 {entry['name']}...OK" for i, entry in enumerate(required, 1))
        result = baseline_inventory(text + "\nAll 13 tests passed.\n", contracts)
        self.assertEqual(result["required_tests"], required)
        self.assertEqual(result["total"], 13)

    def test_native_test_deletion_rename_skip_or_duplicate_cannot_reduce_gate(self):
        contracts = json.loads(CONTRACTS.read_text())
        required = contracts["required_native_tests"]
        records = [f"{i}/13 {entry['name']}...OK" for i, entry in enumerate(required, 1)]
        for index in (0, 1, 7, 12):
            with self.subTest(required_id=required[index]["id"]):
                for replacement in ("", records[index].replace(required[index]["name"], "unrelated"),
                                    records[index].replace("...OK", "...SKIP"),
                                    records[index].replace(required[index]["name"], required[(index + 1) % 13]["name"])):
                    changed = records.copy()
                    changed[index] = replacement
                    with self.assertRaises(ValueError):
                        baseline_inventory("\n".join(changed) + "\nAll 13 tests passed.\n", contracts)

    def test_native_entity_inventory_rejects_extra_missing_or_mistyped_counts(self):
        contracts = json.loads(CONTRACTS.read_text())
        table = json.loads((ASSETS / "entities.json").read_text())
        self.assertEqual(len(table), 2231)
        self.assertEqual(entity_inventory({"entries": 2231}, len(table), contracts),
                         {"native_entries": 2231, "community_entries": 2231})
        for native in ({"entries": 2232}, {"entries": 2230}, {"entries": "2231"}, {"entries": True}, {}):
            with self.subTest(native=native), self.assertRaises(ValueError):
                entity_inventory(native, len(table), contracts)
        with self.assertRaises(ValueError):
            entity_inventory({"entries": 2231}, len(table) - 1, contracts)

    def test_every_declared_mutation_changes_only_production_prefix(self):
        contracts = json.loads(CONTRACTS.read_text())
        for mutation in MUTATIONS:
            with self.subTest(mutation=mutation["id"]):
                original = (BROWSER / mutation["module"]).read_text()
                boundary = contracts["mutation_modules"][mutation["module"]]["test_helper_boundary"]
                patched = apply_mutation(original, mutation, contracts)
                self.assertNotEqual(original, patched)
                self.assertEqual(original.split(boundary, 1)[1], patched.split(boundary, 1)[1])

    def test_mutation_refuses_helpers_before_first_test_declaration(self):
        contracts = json.loads(CONTRACTS.read_text())
        helper_anchors = {
            "entities.zig": "try testing.expectEqual(expected, out);",
            "png.zig": "try testing.expectEqualSlices(u8, signature, png[0..8]);",
        }
        for module, anchor in helper_anchors.items():
            with self.subTest(module=module):
                original = (BROWSER / module).read_text()
                self.assertIn(anchor, original.partition('\ntest "')[0])
                with self.assertRaisesRegex(ValueError, "never test helpers"):
                    apply_mutation(original, {"module": module, "old": anchor, "new": "unreachable;"}, contracts)

    def test_mutation_refuses_absent_or_ambiguous_production_boundaries(self):
        contracts = json.loads(CONTRACTS.read_text())
        for mutation in (MUTATIONS[0], next(m for m in MUTATIONS if m["module"] == "png.zig")):
            original = (BROWSER / mutation["module"]).read_text()
            boundary = contracts["mutation_modules"][mutation["module"]]["test_helper_boundary"]
            for changed in (original.replace(boundary, "\n"), original + boundary):
                with self.subTest(mutation=mutation["id"]), self.assertRaisesRegex(ValueError, "boundary"):
                    apply_mutation(changed, mutation, contracts)


if __name__ == "__main__":
    unittest.main()
