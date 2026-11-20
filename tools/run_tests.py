"""Offline tests with external output, no bytecode in the checkout."""

import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from setup_scope_recall import check_python, select_python


SCOPE_RECALL_TESTS = (
    "tests.domain.test_dreaming_scope_recall",
    "tests.desktop.test_dreaming_semantic",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--python", help="interpreter override")
    parser.add_argument("--test", action="append", help="dotted unittest name (repeatable)")
    parser.add_argument(
        "--scope-recall", action="store_true",
        help="require the vendored installation and default to Scope Recall tests",
    )
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    python = select_python(repo, args.python)
    if args.scope_recall:
        try:
            checked = check_python(repo, python) == 0
        except OSError as error:
            parser.error(str(error))
        if not checked:
            parser.error("prepare this checkout with python tools/setup_scope_recall.py")

    output = (args.output or Path(tempfile.mkdtemp(prefix="quintessence-tests-"))).resolve()
    if output == repo or repo in output.parents or output in repo.parents:
        parser.error("test output and code checkout must be disjoint directory trees")
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in list(env):
        if any(marker in key.upper() for marker in ("API_KEY", "API_TOKEN", "ACCESS_TOKEN", "AUTH_TOKEN")):
            env.pop(key)
    env.update(
        PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
        TEMP=str(output), TMP=str(output), QUINTESSENCE_TEST_OUTPUT=str(output),
        PYTHONPATH=str(repo / "tools" / "offline_guard"),
    )
    targets = args.test or (SCOPE_RECALL_TESTS if args.scope_recall else ())
    selection = ["-v", *targets] if targets else ["discover", "-s", "tests", "-v"]
    with (output / "unittest.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            [python, "-m", "unittest", *selection],
            cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    print(f'Test log: {output / "unittest.log"}')
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
