"""J02 D1: contract structure and text-consistency self-check.

These tests read only ``contracts/memory-integration/v1``. They do not import
the upstream package, open a database, or require the Lab environment: the
point is that the frozen contract can be checked anywhere.

They assert required files, sections, keywords, phrases, enum classes and
cross-file consistency. Passing them does not prove runtime behaviour;
behavioural claims are backed by the probe outputs cited in the contracts.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts" / "memory-integration" / "v1"

CONTRACT_FILES = (
    "01-instance-identity.md",
    "02-source-mapping.md",
    "03-role-and-origin.md",
    "04-root-source-dedup.md",
    "05-delivery-receipt.md",
    "06-deletion.md",
    "07-budget.md",
    "08-permissions.md",
    "09-error-mapping.md",
)

#: A path that names one machine never belongs in a frozen contract.
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9_])(?![Xx]:)[A-Z]:[/\\]")
_HOME_PATH = re.compile(r"/(?:home|Users|root)/")

#: The evidence classes an enum value may belong to.
SOURCE_CLASSES = ("measured", "declared", "pcs_contract", "proposed", "patched")


def read(name: str) -> str:
    return (CONTRACTS / name).read_text(encoding="utf-8")


def flat(text: str) -> str:
    """Collapse whitespace so a phrase check is not tied to line wrapping."""

    return re.sub(r"\s+", " ", text)


class MemoryIntegrationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.files = {name: read(name) for name in ("README.md", *CONTRACT_FILES)}
        self.enums = json.loads(read("enums.json"))

    # ------------------------------------------------------------ structure

    def test_index_and_enums_exist(self) -> None:
        self.assertTrue((CONTRACTS / "README.md").is_file())
        self.assertTrue((CONTRACTS / "enums.json").is_file())

    def test_nine_contracts_are_present_and_shaped(self) -> None:
        self.assertEqual(9, len(CONTRACT_FILES))
        for name in CONTRACT_FILES:
            with self.subTest(file=name):
                text = self.files[name]
                self.assertTrue(text.startswith("# "), msg=name)
                self.assertGreater(len(text), 400, msg=name)
                self.assertIn("\n## ", text, msg=name)

    def test_pin_is_recorded_in_the_readme(self) -> None:
        readme = self.files["README.md"]
        self.assertIn("3.14.1", readme)
        self.assertIn("2b640260134565ac7fef3a76644e352a50866533", readme)
        self.assertIn("D1", readme)
        self.assertIn("D2", readme)

    # ------------------------------------------------ issue 1: source identity

    def test_source_key_is_per_immutable_version(self) -> None:
        text = self.files["02-source-mapping.md"]
        self.assertIn("deterministic(instance_id, namespace, ref_kind, ref_id, version)", text)
        self.assertIn("different", text.lower())
        # The four version axes are named and not equated.
        for axis in ("PCS ref version", "record_version", "source_revision", "attempt"):
            self.assertIn(axis, text)
        self.assertIn("never equated", text.lower())

    def test_conflict_is_not_a_retry(self) -> None:
        text = self.files["02-source-mapping.md"].lower()
        self.assertIn("conflict", text)
        self.assertIn("not a retry condition", text)
        self.assertIn("no overwrite", text)

    def test_root_contract_separates_version_from_root(self) -> None:
        text = self.files["04-root-source-dedup.md"]
        self.assertIn("Projection version identity", text)
        self.assertIn("Evidence root identity", text)
        self.assertIn("does not add a support vote", text)
        self.assertIn("enumerate", text.lower())
        self.assertIn("needs_review", text)

    def test_deletion_scope_distinguishes_version_and_root(self) -> None:
        text = self.files["04-root-source-dedup.md"]
        self.assertIn("delete one projection version", text)
        self.assertIn("delete the whole root", text)
        self.assertIn("not authorize", text.lower())

    # ------------------------------------------------ issue 2: retry by operation

    def test_retry_is_qualified_by_operation(self) -> None:
        text = self.files["09-error-mapping.md"]
        self.assertIn("Retry is per operation", text)
        self.assertIn("capture", text)
        self.assertIn("deletion", text)
        self.assertIn("read query", text)

    def test_deletion_is_never_retried_automatically(self) -> None:
        nine = self.files["09-error-mapping.md"].lower()
        six = self.files["06-deletion.md"].lower()
        self.assertIn("never automatic", nine)
        self.assertIn("no automatic replay", six)
        self.assertIn("needs_review", six)
        self.assertIn("not retried", six)

    def test_version_conflict_has_two_readings(self) -> None:
        six = flat(self.files["06-deletion.md"]).lower()
        self.assertIn("two readings", six)
        self.assertIn("preview clearly stale", six)
        self.assertIn("original deletion outcome unknown", six)

    def test_capture_retry_conditions_are_explicit(self) -> None:
        text = flat(self.files["05-delivery-receipt.md"])
        self.assertIn("capture only", text)
        self.assertIn('durability = "unknown"', text)
        self.assertIn("never reported as success", text)

    # ------------------------------------------------ issue 3: enum evidence

    def test_enum_inventory_is_well_formed(self) -> None:
        self.assertEqual("memory-integration/v1", self.enums["contract"])
        upstream = self.enums["upstream"]
        self.assertEqual("hermes-scope-recall", upstream["distribution"])
        self.assertEqual("3.14.1", upstream["version"])
        self.assertEqual(
            "2b640260134565ac7fef3a76644e352a50866533", upstream["commit"]
        )
        self.assertEqual(set(SOURCE_CLASSES), set(self.enums["source_classes"]))

    def test_every_value_has_exactly_one_evidence_class(self) -> None:
        for name, entry in self.enums["fields"].items():
            with self.subTest(field=name):
                self.assertIn(entry["owner"], ("upstream", "pcs"))
                buckets = entry["sources"]
                self.assertTrue(
                    set(buckets) <= set(SOURCE_CLASSES),
                    msg=f"{name} has an unknown source class",
                )
                self.assertTrue(
                    any(buckets.values()), msg=f"{name} has no values"
                )
                seen: dict[str, str] = {}
                for klass, values in buckets.items():
                    for value in values:
                        self.assertNotIn(
                            value, seen, msg=f"{name}: {value} in two classes"
                        )
                        seen[value] = klass
                for values in buckets.values():
                    self.assertEqual(len(values), len(set(values)), msg=name)

    def test_closed_sets_have_a_basis(self) -> None:
        for name, entry in self.enums["fields"].items():
            with self.subTest(field=name):
                self.assertIs(type(entry["closed"]), bool)
                if entry["closed"]:
                    self.assertIsInstance(entry["closed_basis"], str)
                    self.assertTrue(entry["closed_basis"].strip())
                else:
                    self.assertIsNone(entry["closed_basis"])

    def test_receipt_result_is_patch_only_not_an_upstream_capability(self) -> None:
        entry = self.enums["fields"]["receipt_result"]
        self.assertEqual([], entry["sources"]["measured"])
        self.assertEqual([], entry["sources"]["declared"])
        self.assertEqual([], entry["sources"]["proposed"])
        self.assertTrue(entry["sources"]["patched"])
        self.assertIn("not accepted upstream", entry["closed_basis"])
        upstream = self.enums["upstream"]
        patch = self.enums["local_patch"]
        self.assertIs(upstream["public_receipt_interface"], False)
        self.assertIs(patch["public_receipt_interface"], True)
        self.assertIs(patch["upstream_accepted"], False)
        self.assertEqual("pcs/receipt-v1", patch["branch"])
        self.assertEqual(
            "2b640260134565ac7fef3a76644e352a50866533", patch["base"]
        )
        six = self.files["06-deletion.md"]
        self.assertIn(patch["commit"], six)
        self.assertIn(patch["first_commit"], six)
        self.assertNotEqual(patch["first_commit"], patch["commit"])
        self.assertIn("not accepted", six.lower())

    def test_receipt_link_completeness_and_authority_order_are_documented(self) -> None:
        six = flat(self.files["06-deletion.md"]).lower()
        self.assertIn("empty link", six)
        self.assertIn("duplicate identifier", six)
        self.assertIn("target groups", six)
        self.assertIn("authorization is decided before", six)
        self.assertIn("non-deletion actions", six)
        nine = flat(self.files["09-error-mapping.md"]).lower()
        self.assertIn("authority is checked before", nine)
        self.assertIn("duplicate identifier", nine)

    def test_pcs_states_are_contract_values(self) -> None:
        entry = self.enums["fields"]["pcs_delivery_state"]
        self.assertEqual([], entry["sources"]["measured"])
        self.assertTrue(entry["sources"]["pcs_contract"])

    def test_conflict_and_unknown_durability_are_listed(self) -> None:
        disposition = self.enums["fields"]["capture_disposition"]["sources"]
        self.assertIn("conflict", disposition["measured"])
        durability = self.enums["fields"]["capture_durability"]["sources"]
        self.assertIn("unknown", durability["declared"])

    def test_every_enum_value_appears_in_its_contract(self) -> None:
        pairs = {
            "origin": "03-role-and-origin.md",
            "role": "03-role-and-origin.md",
            "capture_disposition": "05-delivery-receipt.md",
            "capture_durability": "05-delivery-receipt.md",
            "capture_mutation": "05-delivery-receipt.md",
            "capture_lexical_state": "05-delivery-receipt.md",
            "capture_semantic_state": "05-delivery-receipt.md",
            "deletion_layer": "06-deletion.md",
            "deletion_layer_state": "06-deletion.md",
            "contract_error_code": "09-error-mapping.md",
            "receipt_result": "06-deletion.md",
            "pcs_delivery_state": "02-source-mapping.md",
        }
        self.assertEqual(set(self.enums["fields"]), set(pairs))
        for field, filename in pairs.items():
            text = self.files[filename]
            for klass, values in self.enums["fields"][field]["sources"].items():
                for value in values:
                    with self.subTest(field=field, klass=klass, value=value):
                        self.assertIn(value, text)

    # ---------------------------------------------------------- deletion v1

    def test_deletion_contract_has_three_layers(self) -> None:
        text = self.files["06-deletion.md"]
        for layer in ("L1", "L2", "L3"):
            self.assertIn(layer, text)
        self.assertIn("receipt", text.lower())
        self.assertIn("PurgePort", text)
        self.assertIn("VERSION_CONFLICT", text)
        self.assertIn("schema migration", text.lower())

    def test_receipt_interface_is_patch_only_and_d2_is_declared_there(self) -> None:
        text = flat(self.files["06-deletion.md"]).lower()
        self.assertIn("local patch", text)
        self.assertIn("not upstream", text)
        self.assertIn("not accepted", text)
        self.assertIn("d2", text)
        self.assertIn("declared on the local patch baseline", text)
        self.assertIn("schema migration", text)

    # ------------------------------------------------- required vocabulary

    def test_each_contract_names_its_core_fields(self) -> None:
        required = {
            "01-instance-identity.md": ("installation_id", "data_directory", "scope_ids"),
            "02-source-mapping.md": ("source_event_key", "source_revision", "record_version"),
            "03-role-and-origin.md": ("source_principal", "human_direct"),
            "04-root-source-dedup.md": ("root", "episode"),
            "05-delivery-receipt.md": ("delivery_id", "request_hash"),
            "07-budget.md": ("estimate_tokens", "max_items", "remaining_seconds"),
            "08-permissions.md": ("InstallationMaintenance", "TrustedContext"),
            "09-error-mapping.md": ("ACCESS_DENIED", "SOURCE_MISSING", "VERSION_CONFLICT"),
        }
        for filename, words in required.items():
            text = self.files[filename].lower()
            for word in words:
                with self.subTest(file=filename, word=word):
                    self.assertIn(word.lower(), text)

    # ----------------------------------------------------------- hygiene

    def test_no_absolute_or_private_paths(self) -> None:
        for name, text in self.files.items():
            with self.subTest(file=name):
                self.assertIsNone(_ABSOLUTE_PATH.search(text), msg=name)
                self.assertIsNone(_HOME_PATH.search(text), msg=name)
                # Split so this test's own source does not match the gate.
                self.assertNotIn("chi" + "tose", text.lower())

    def test_contracts_do_not_claim_an_implementation(self) -> None:
        for name in CONTRACT_FILES:
            text = self.files[name]
            self.assertNotIn("is implemented", text.lower(), msg=name)


if __name__ == "__main__":
    unittest.main()
