"""PCS2-007 B group required concurrency proof.

Two **independent processes** build an Episode from the same source event at the
same time.  The implementation has no "look first" branch in the write path: it
derives the body, appends a record, then claims the ``episode_sources``
``(namespace, event_id)`` primary key.  The loser therefore hits the unique key,
rolls its own transaction back, re-reads the winner's mapping and returns that
exact version.  The assertions prove only one record, one mapping, one audit row
and one revision exist afterwards, and that both processes observed the same
reference.

This is a real multi-process race (the required kind), not two sequential calls
in one test method; ``test_episodes.py`` covers the same duplicate path through
a second connection.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from domain_support import NS, RECORDED_AT, SRC_DIR  # noqa: E402

from quintessence_pcs.store.domain import DomainStore  # noqa: E402

REPO_ROOT = HERE.parents[1]
SECRET = "concurrent-secret-that-must-not-be-copied"

_CHILD_CODE = r"""
import json
import sys
import time
from pathlib import Path

from quintessence_pcs.domains import EpisodeService
from quintessence_pcs.store.domain import DomainStore

db_path, namespace, event_id, barrier_dir, out_path, index = sys.argv[1:7]
store = DomainStore.open(db_path)
barrier = Path(barrier_dir)
(barrier / ("ready-" + index)).write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 60.0
while not (barrier / "go").exists():
    if time.monotonic() > deadline:
        raise SystemExit("barrier timeout")
    time.sleep(0.005)

service = EpisodeService(store)
version = service.build_episode_from_event(
    namespace=namespace,
    event_id=event_id,
    scope={"session_id": "concurrent"},
    authorization_ref="auth:concurrent-" + index,
)
Path(out_path).write_text(
    json.dumps(
        {
            "outcome": "built",
            "record_id": version.record_id,
            "record_version": version.record_version,
            "kind": version.kind,
            "scope": version.scope,
            "index": index,
        }
    ),
    encoding="utf-8",
)
store.close()
"""


class ConcurrentEpisodeBuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs2-007-concurrent-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "concurrent.sqlite"
        self.barrier = self.root / "barrier"
        self.barrier.mkdir()

        self.store = DomainStore.open(self.db_path)
        self.addCleanup(self.store.close)
        self.receipt = self.store.ingest_event(
            namespace=NS,
            source_kind="synthetic_fixture",
            source_instance="concurrent-parent",
            source_event_id="concurrent-source-1",
            event_type="ConcurrentBuild",
            observed_at="2026-10-17T01:02:03Z",
            payload={"text": SECRET},
            recorded_at=RECORDED_AT,
        )
        self.assertEqual(0, self.store.current_state_revision(NS))

    def test_two_processes_create_exactly_one_episode(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(SRC_DIR) + (
            os.pathsep + environment["PYTHONPATH"]
            if environment.get("PYTHONPATH")
            else ""
        )

        children: list[tuple[subprocess.Popen[str], Path]] = []
        for index in ("a", "b"):
            out_path = self.root / f"out-{index}.json"
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _CHILD_CODE,
                    str(self.db_path),
                    NS,
                    self.receipt.event_id,
                    str(self.barrier),
                    str(out_path),
                    index,
                ],
                cwd=str(REPO_ROOT),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            children.append((process, out_path))
            self.addCleanup(
                lambda p=process: p.poll() is None and p.kill()
            )

        self._wait_for_markers(children)
        (self.barrier / "go").write_text("go", encoding="utf-8")

        results = []
        for process, out_path in children:
            try:
                stdout, stderr = process.communicate(timeout=60)
            except subprocess.TimeoutExpired:  # pragma: no cover - failure path
                process.kill()
                self.fail("child did not finish within 60 seconds")
            if process.returncode != 0:
                self.fail(
                    f"child exited with {process.returncode}: stdout={stdout!r} "
                    f"stderr={stderr!r}"
                )
            results.append(json.loads(out_path.read_text(encoding="utf-8")))

        # Both writers observed the same exact version.
        self.assertEqual(1, len({result["record_id"] for result in results}))
        self.assertEqual(1, len({result["record_version"] for result in results}))
        for result in results:
            self.assertEqual("episode", result["kind"])
            self.assertEqual(1, result["record_version"])
            self.assertEqual({"session_id": "concurrent"}, result["scope"])

        # Exactly one domain commit: one record, one version, one mapping, one
        # audit row.  The loser's attempt left nothing behind.
        self.assertEqual(1, self.store.current_state_revision(NS))
        self.assertEqual(1, self._count("records"))
        self.assertEqual(1, self._count("record_versions"))
        self.assertEqual(1, self._count("episode_sources"))
        self.assertEqual(1, self._count("authorization_audit"))

        version = self.store.get_version(NS, results[0]["record_id"], 1)
        self.assertEqual("episode", version.kind)
        self.assertEqual({"session_id": "concurrent"}, version.scope)
        self.assertNotIn(SECRET, json.dumps(version.body, ensure_ascii=False))
        mapping = self.store.episode_source(NS, self.receipt.event_id)
        self.assertEqual(results[0]["record_id"], mapping.record_id)

        # A later rebuild is idempotent as well and moves no watermark.
        from quintessence_pcs.domains import EpisodeService

        rebuilt = EpisodeService(self.store).build_episode_from_event(
            namespace=NS,
            event_id=self.receipt.event_id,
            scope={"session_id": "later"},
            authorization_ref="auth:later",
        )
        self.assertEqual(results[0]["record_id"], rebuilt.record_id)
        self.assertEqual(1, rebuilt.record_version)
        self.assertEqual(1, self.store.current_state_revision(NS))

    def _count(self, table: str) -> int:
        return int(
            self.store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        )

    def _wait_for_markers(
        self, children: list[tuple[subprocess.Popen[str], Path]]
    ) -> None:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            if all(
                (self.barrier / f"ready-{index}").exists() for index in ("a", "b")
            ):
                return
            for process, _out_path in children:
                if process.poll() is not None:
                    stderr = process.stderr.read() if process.stderr else ""
                    if process.stderr is not None:
                        process.stderr.close()
                    self.fail(f"child exited before the barrier: {stderr}")
            time.sleep(0.01)
        self.fail("children did not reach the barrier within 60 seconds")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
