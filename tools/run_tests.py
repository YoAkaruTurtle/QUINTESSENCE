"""Offline tests with external output, no bytecode in the checkout."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    output = (args.output or Path(tempfile.mkdtemp(prefix='quintessence-tests-'))).resolve()
    if output == repo or repo in output.parents or output in repo.parents:
        parser.error('test output and code checkout must be disjoint directory trees')
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in list(env):
        if any(marker in key.upper() for marker in ('API_KEY', 'API_TOKEN', 'ACCESS_TOKEN', 'AUTH_TOKEN')):
            env.pop(key)
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               TEMP=str(output), TMP=str(output), QUINTESSENCE_TEST_OUTPUT=str(output),
               PYTHONPATH=str(repo / 'tools' / 'offline_guard'))
    with (output / 'unittest.log').open('w', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-v'],
                                cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT)
    print(f'Test log: {output / "unittest.log"}')
    return result.returncode

if __name__ == '__main__':
    raise SystemExit(main())
