"""RG06 P04: the expected-persona startup contract (offline, zero model calls).

``app.persona_expected`` is a **startup/restart** contract (RG06 review Q2),
never a per-turn pin.  This module covers the three P04 arms and the neutral
provisioning proof:

* A: even the neutral provisioning fails -> ``NEUTRAL_PROVISION_FAILED``;
* B: neutral provisioning succeeds through the real publish/approve/activate
  path and a turn is accepted;
* C: an unavailable expected persona -> ``EXPECTED_PERSONA_UNAVAILABLE`` naming
  the expected id, no neutral profile created, zero model attempts.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tests.desktop import support  # noqa: F401 - installs the sidecar on sys.path
from tests.desktop.support import DesktopCase, config_document
from host_config import ConfigError, load_config
from runtime import DesktopRuntime, RuntimeStartupError
from quintessence_pcs.domains.persona.profiles import PersonaProfileService
from quintessence_pcs.store import CognitionStore

NAMESPACE = support.NAMESPACE
LAB_PROFILE_ID = "example.synthetic.desktop.lab"
SPEAKER = {"identity": "owner", "display_name": "Synthetic Owner"}


def _write(tmp_path: Path, document: dict) -> Path:
    from tests.desktop.support import write_config

    return write_config(tmp_path, document, name=f"persona-{len(list(tmp_path.glob('persona-*.json')))}.json")


class PersonaStartupCase(DesktopCase):
    def _document(
        self,
        *,
        expected: dict | None = None,
        speaker: dict | None = SPEAKER,
        profile: str = "lab",
        desktop: dict | None = None,
        transport_kind: str = "scripted",
        external: bool = False,
        **kwargs,
    ) -> dict:
        document = config_document(
            self.db_path,
            profile=profile,
            desktop=desktop,
            transport_kind=transport_kind,
            external=external,
            **kwargs,
        )
        if speaker is not None:
            document["app"]["speaker"] = dict(speaker)
        if expected is not None:
            document["app"]["persona_expected"] = dict(expected)
        return document

    def _info(self, process) -> dict:
        reply = process.request("app.info")
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def _seed_lab_profile(self) -> None:
        """Create the synthetic Lab profile once, then close the sidecar."""

        process = self.start(document=self._document(expected=None, speaker=None))
        self._info(process)
        process.close()
        self.processes.remove(process)

    def _store(self) -> CognitionStore:
        return CognitionStore.open(self.db_path)

    def _persona_profile_count(self) -> int:
        return int(
            self.scalar(
                "SELECT COUNT(*) FROM records WHERE kind='persona_profile'"
            )
        )


class ExpectedPersonaStartupTests(PersonaStartupCase):
    def test_arm_a_neutral_provisioning_failure_is_reported_with_zero_models(self) -> None:
        document = self._document(
            profile="production",
            desktop={"managed": True, "provision": True},
            transport_kind="http",
            external=True,
        )
        config = load_config(_write(self.tmp_path, document))
        with mock.patch.object(
            PersonaProfileService,
            "publish_profile",
            side_effect=RuntimeError("synthetic provisioning failure"),
        ):
            with self.assertRaises(RuntimeStartupError) as caught:
                DesktopRuntime(config)
        self.assertEqual("NEUTRAL_PROVISION_FAILED", caught.exception.code)
        self.assertEqual(0, self._persona_profile_count())
        self.assertEqual(0, int(self.scalar("SELECT COUNT(*) FROM model_attempts")))
        self.assertEqual(0, int(self.scalar("SELECT COUNT(*) FROM collab_turns")))

    def test_arm_a_approval_event_failure_is_the_same_state(self) -> None:
        document = self._document(
            profile="production",
            desktop={"managed": True, "provision": True},
            transport_kind="http",
            external=True,
        )
        config = load_config(_write(self.tmp_path, document))
        with mock.patch.object(
            CognitionStore, "ingest_event", side_effect=RuntimeError("no write")
        ):
            with self.assertRaises(RuntimeStartupError) as caught:
                DesktopRuntime(config)
        self.assertEqual("NEUTRAL_PROVISION_FAILED", caught.exception.code)
        self.assertEqual(0, self._persona_profile_count())

    def test_arm_b_lab_neutral_profile_is_real_and_a_turn_is_accepted(self) -> None:
        process = self.start(document=self._document(expected=None, speaker=None))
        info = self._info(process)
        self.assertTrue(info["host"]["ready"], info["host"])
        self.assertEqual(LAB_PROFILE_ID, info["persona"]["profile_id"])
        self.assertEqual("neutral_provisioned", info["persona"]["source"])
        self.assertIsNone(info["persona"]["expected"])
        self.assertIsNone(info["persona"]["matches_expected"])
        self.assertEqual(1, self._persona_profile_count())
        self.assertEqual(
            1,
            int(
                self.scalar(
                    "SELECT COUNT(*) FROM event_journal "
                    "WHERE event_type='persona.review.approve'"
                )
            ),
        )
        created = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": "chat"},
        )
        self.assertTrue(created["ok"], created)
        conversation_id = created["result"]["conversation"]["conversation_id"]
        accepted = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "client_request_id": "rg06-b1",
                "text": "Synthetic neutral turn.",
            },
        )
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual(1, int(self.scalar("SELECT COUNT(*) FROM collab_turns")))

    def test_arm_b_managed_production_provisions_the_generic_profile(self) -> None:
        process = self.start(
            document=self._document(
                expected=None,
                speaker=None,
                profile="production",
                desktop={"managed": True, "provision": True},
                transport_kind="http",
                external=True,
            )
        )
        info = self._info(process)
        self.assertFalse(info["host"]["ready"])
        self.assertEqual("MODEL_KEY_MISSING", info["host"]["reason_code"])
        self.assertEqual(
            "quintessence.desktop.general", info["persona"]["profile_id"]
        )
        self.assertEqual("neutral_provisioned", info["persona"]["source"])
        row = self.scalar(
            "SELECT p.body_json FROM record_versions r "
            "JOIN private_payloads p ON p.payload_ref = r.body_payload_ref "
            "WHERE r.kind='persona_profile'"
        )
        self.assertIsNotNone(row, "the neutral profile must be a real record")
        self.assertIn("QUINTESSENCE", row)

    def test_arm_c1_missing_expected_persona_creates_nothing(self) -> None:
        expected = {"profile_id": "example.synthetic.absent", "profile_version": "1"}
        process = self.start(document=self._document(expected=expected))
        info = self._info(process)
        self.assertFalse(info["host"]["ready"])
        self.assertEqual("EXPECTED_PERSONA_UNAVAILABLE", info["host"]["reason_code"])
        self.assertIn("example.synthetic.absent", info["host"]["reason_detail"])
        self.assertEqual(0, self._persona_profile_count())
        self.assertEqual(0, int(self.scalar("SELECT COUNT(*) FROM model_attempts")))
        self.assertEqual(0, int(self.scalar("SELECT COUNT(*) FROM collab_turns")))
        self.assertEqual(
            "EXPECTED_PERSONA_UNAVAILABLE", info["persona"]["reason_code"]
        )
        self.assertEqual(expected, info["persona"]["expected"])
        self.assertFalse(info["persona"]["matches_expected"])

    def test_arm_c2_existing_but_not_active(self) -> None:
        self._seed_lab_profile()
        store = self._store()
        try:
            store.connection.execute(
                "UPDATE namespace_state SET active_profile_record_id=NULL, "
                "active_profile_version=NULL WHERE namespace=?",
                (NAMESPACE,),
            )
        finally:
            store.close()
        process = self.start(
            document=self._document(expected={"profile_id": LAB_PROFILE_ID})
        )
        info = self._info(process)
        self.assertEqual("EXPECTED_PERSONA_UNAVAILABLE", info["host"]["reason_code"])
        self.assertIn("not_active", info["host"]["reason_detail"])
        self.assertEqual(1, self._persona_profile_count())
        self.assertEqual(0, int(self.scalar("SELECT COUNT(*) FROM collab_turns")))

    def test_arm_c3_version_mismatch(self) -> None:
        self._seed_lab_profile()
        process = self.start(
            document=self._document(
                expected={"profile_id": LAB_PROFILE_ID, "profile_version": "9.9.9"}
            )
        )
        info = self._info(process)
        self.assertEqual("EXPECTED_PERSONA_UNAVAILABLE", info["host"]["reason_code"])
        self.assertIn("version", info["host"]["reason_detail"])
        self.assertEqual(0, int(self.scalar("SELECT COUNT(*) FROM model_attempts")))

    def test_arm_c4_deleted_review_reference(self) -> None:
        self._seed_lab_profile()
        store = self._store()
        try:
            event_id = store.connection.execute(
                "SELECT event_id FROM event_journal "
                "WHERE event_type='persona.review.approve'"
            ).fetchone()[0]
            store.request_deletion(
                target_kind="event",
                namespace=NAMESPACE,
                target_id=event_id,
                authorization_ref="authorization:synthetic-test",
            )
            store.cleanup_deletions(namespace=NAMESPACE)
        finally:
            store.close()
        process = self.start(
            document=self._document(expected={"profile_id": LAB_PROFILE_ID})
        )
        info = self._info(process)
        self.assertEqual("EXPECTED_PERSONA_UNAVAILABLE", info["host"]["reason_code"])
        self.assertIn("review_ref", info["host"]["reason_detail"])
        self.assertEqual(0, int(self.scalar("SELECT COUNT(*) FROM model_attempts")))

    def test_expected_persona_requires_a_speaker(self) -> None:
        document = self._document(
            expected={"profile_id": LAB_PROFILE_ID}, speaker=None
        )
        with self.assertRaises(ConfigError) as caught:
            load_config(_write(self.tmp_path, document))
        self.assertEqual("CONFIG_INVALID", caught.exception.code)


class PersonaConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="persona-config-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _load(self, document: dict):
        return load_config(_write(self.tmp, document))

    def test_persona_expected_shape_is_closed(self) -> None:
        document = config_document(self.tmp / "desktop.sqlite")
        document["app"]["speaker"] = dict(SPEAKER)
        document["app"]["persona_expected"] = {
            "profile_id": "example.ok",
            "profile_name": "not allowed",
        }
        with self.assertRaises(ConfigError) as caught:
            self._load(document)
        self.assertIn("unknown field", str(caught.exception))

    def test_speaker_identity_is_closed_and_unknown_has_no_name(self) -> None:
        document = config_document(self.tmp / "desktop.sqlite")
        document["app"]["speaker"] = {"identity": "stranger"}
        with self.assertRaises(ConfigError):
            self._load(document)
        document["app"]["speaker"] = {
            "identity": "unknown",
            "display_name": "Synthetic",
        }
        with self.assertRaises(ConfigError):
            self._load(document)

    def test_persona_control_defaults_to_disabled(self) -> None:
        document = config_document(self.tmp / "desktop.sqlite")
        config = self._load(document)
        self.assertFalse(config.persona_control_publish)
        self.assertFalse(config.persona_control_activate)
        self.assertIsNone(config.persona_expected)
        self.assertIsNone(config.speaker)


if __name__ == "__main__":
    unittest.main()
