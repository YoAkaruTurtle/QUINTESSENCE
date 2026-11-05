"""RG05 supplement 03: the default narrow auto-commit grant.

Creates/reads one real J06 authorization through the production function; no
model, no network, synthetic config only.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
SIDECAR_DIR = REPO_ROOT / "apps" / "desktop" / "sidecar"
for _path in (REPO_ROOT / "src", SIDECAR_DIR, REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from host_config import load_config  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.governance import GovernanceStore  # noqa: E402
from runtime import (  # noqa: E402
    DEFAULT_AUTO_CLAIM_TYPES,
    RuntimeStartupError,
    default_cognition_grant_id,
    ensure_default_cognition_grant,
)

from tests.desktop import support  # noqa: E402


class DefaultCognitionGrantTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rg05-grant-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.database = self.directory / "desktop.sqlite"
        document = support.config_document(self.database)
        document["budget"]["pcs_context_max_bytes"] = 65536
        document["cognition"] = {"propose": True}
        self.config = load_config(
            support.write_config(self.directory, document, name="host.json")
        )
        self.store = CognitionStore.open(self.database)
        self.addCleanup(self.store.close)

    def test_the_grant_is_narrow_and_idempotent_across_restarts(self) -> None:
        first = ensure_default_cognition_grant(self.store, self.config)
        second = ensure_default_cognition_grant(self.store, self.config)
        self.assertEqual(first, second)
        self.assertEqual(
            default_cognition_grant_id(self.config.authority.namespace), first
        )
        row = GovernanceStore(self.store).load_commit_authorization(
            namespace=self.config.authority.namespace, authorization_id=first
        )
        self.assertIsNotNone(row)
        self.assertEqual(("cognition.change",), row.operations)
        self.assertEqual(set(DEFAULT_AUTO_CLAIM_TYPES), set(row.claim_types))
        self.assertNotIn("derived_rule", row.claim_types)
        self.assertIn("user_preference", row.claim_types)
        self.assertEqual("E1", row.max_impact)
        self.assertEqual(
            dict(self.config.query_plan.scope), dict(row.scope)
        )
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM cognition_commit_authorizations"
        ).fetchone()[0]
        self.assertEqual(1, count)
        # Restart: a fresh store still finds one active id.
        self.store.close()
        reopened = CognitionStore.open(self.database)
        self.addCleanup(reopened.close)
        self.assertEqual(first, ensure_default_cognition_grant(reopened, self.config))

    def test_a_scope_change_upserts_the_same_grant(self) -> None:
        first = ensure_default_cognition_grant(self.store, self.config)
        document = json.loads(
            (self.directory / "host.json").read_text(encoding="utf-8")
        )
        document["consult"]["query_plan"]["scope"] = {"project_id": "other"}
        changed = load_config(
            support.write_config(self.directory, document, name="host2.json")
        )
        self.assertEqual(first, ensure_default_cognition_grant(self.store, changed))
        row = GovernanceStore(self.store).load_commit_authorization(
            namespace=changed.authority.namespace, authorization_id=first
        )
        self.assertEqual({"project_id": "other"}, dict(row.scope))

    def test_creation_failure_is_a_startup_error(self) -> None:
        with patch.object(
            GovernanceStore,
            "grant_commit_authorization",
            side_effect=RuntimeError("synthetic failure"),
        ):
            with self.assertRaises(RuntimeStartupError):
                ensure_default_cognition_grant(self.store, self.config)


if __name__ == "__main__":
    unittest.main()
