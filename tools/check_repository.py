"""Fail closed on unexpected files, unsafe payloads and reachable Git history.

The default mode is safe to run after a remote exists.  ``--pre-upload``
additionally requires that no remote is configured; it is for the pre-upload
audit only.

This structural/secret-pattern gate supplements human content and rights review.
It cannot certify the absence of every possible secret or copyrighted passage.
"""
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Approved public documents may name the private instance with one exact phrase
# per language. Any other legacy-term occurrence in these files, and every
# occurrence in any other path, still fails the gate.
PUBLIC_DOC_PATHS = frozenset({
    'README.md', 'README.zh-CN.md',
    'ARCHITECTURE.md', 'ARCHITECTURE.zh-CN.md',
    'ROADMAP.md', 'ROADMAP.zh-CN.md',
})
# Built from parts so this gate's own source does not match the legacy terms.
PUBLIC_LEGACY_PHRASES = (
    '千' + '岁' + '（Chi' + 'tose' + '）',
    'Chi' + 'tose' + ' (' + '千' + '岁' + ')',
)

# Exact public brand asset allowed to be binary. Every other binary file fails.
APPROVED_BINARY_PATHS = frozenset({'assets/quintessence-banner.png'})

# Absolute-path style rules were introduced after this public commit. Commits
# reachable from this boundary are exempt from the new style rules only; they
# are still scanned for secrets. Every later commit is treated as new content.
HISTORICAL_BASELINE = '246d87458b548021b762f1c349cdccab05c696f5'

# These upstream files contain reviewed portable examples, Windows system
# defaults, or path-detection regexes. The exemptions apply to the precise
# imported bytes and rule indices only. Other content still uses the gate.
SCOPE_RECALL_PATH_EXAMPLES = {
    'README.md': ('10b5f5dcfa5ba95e59cc38677175b2532558eda98715891160570e78d58bb9db', (3,)),
    'adapters/hermes/boundary.py': ('4786e419c3498cfeaaa918a6900974b9720db6468b01c6f1f4981487b17bec55', (3,)),
    'core/capture_filters.py': ('de2b4ef9adac1dbf2dc3d7a2df81b0745129104f4035838034fd9b2e724b22b1', (2, 3, 4)),
    'core/claims.py': ('e6ba0db3ee31e279bb97a5118855c8786601fd6021298a96fc97b23b6cd6bf00', (3,)),
    'core/mutate.py': ('da68bff9e5346b5082356ca5a6d0414bac7990bdb964d215a1acaf816516da24', (3,)),
    'core/private.py': ('adc5a82f8915226d8bea19aefa3d762fd576c1052f6f7ff7b8ff1f3f98a9320d', (3,)),
    'core/secret_patterns.py': ('3a8b34c68feffaf158f65babaf7f5847a222cb8f537da306252181de5ecdfe00', (3,)),
    'runtime/codex_cli.py': ('2785aa17766b8d2c7467aa7f9ab84682906d098bc3f36c0a35003e2bc7c0884d', (3,)),
}


def git(*args):
    return subprocess.check_output(['git', '--no-optional-locks', '-C', str(ROOT), *args])

def inspect_content(path, data, *, historical=False):
    errors = []
    if b'\0' in data:
        base = path.split(' (', 1)[0]
        if base in APPROVED_BINARY_PATHS and data.startswith(b'\x89PNG\r\n\x1a\n'):
            return []
        return [f'{path}: binary data is not permitted']
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        return [f'{path}: not UTF-8']
    secret_patterns = [
        r'sk-[A-Za-z0-9_-]{24,}',
        r'-----BEGIN ' + r'(?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
        r'(?i)[A-Z]:[/\\]+Users[/\\]+[^/\\\s]+',
    ]
    # An absolute location identifies one machine, so it never belongs in
    # source. Drive X: is reserved for the synthetic long-path fixtures.
    # Immutable pre-baseline history is exempt; the worktree, the index, and
    # every post-baseline commit are checked with the rule active.
    if not historical:
        secret_patterns += [
            r'(?i)(?<![A-Za-z0-9_])' + r'(?![Xx]:)[A-Z]:[/\\]',
            r'(?:^|[\s"\'`(=:,\[{])' + r'/(?:home|Users|root)/',
        ]
    legacy_patterns = [
        r'(?i)' + 'chi' + 'tose',
        '千' + '岁',
    ]
    base = path.split(' (', 1)[0]
    scope_prefix = 'third_party/scope-recall/'
    exception = SCOPE_RECALL_PATH_EXAMPLES.get(base.removeprefix(scope_prefix)) if base.startswith(scope_prefix) else None
    exempt = ()
    if exception is not None and sha256(data.replace(b'\r\n', b'\n')).hexdigest() == exception[0]:
        exempt = exception[1]
    for index, pattern in enumerate(secret_patterns):
        if index not in exempt and re.search(pattern, text): errors.append(f'{path}: sensitive pattern {index}')
    scan = text
    if path.split(' (', 1)[0] in PUBLIC_DOC_PATHS:
        for phrase in PUBLIC_LEGACY_PHRASES:
            scan = scan.replace(phrase, '')
    for index, pattern in enumerate(legacy_patterns):
        if re.search(pattern, scan): errors.append(f'{path}: legacy pattern {index}')
    return errors

def main():
    pre_upload = '--pre-upload' in sys.argv[1:]
    allowed = set(json.loads((ROOT / 'tools/source-files.json').read_text(encoding='utf-8')))
    files = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode().split('\0')
    files = {p for p in files if p}
    errors = [f'unapproved path: {p}' for p in files - allowed]
    errors += [f'missing approved file: {p}' for p in allowed - files]
    for path in sorted(files & allowed):
        p = ROOT / path
        if p.is_symlink(): errors.append(f'{path}: symlinks not permitted')
        else: errors.extend(inspect_content(path, p.read_bytes()))
    # Check the index, not just the bytes currently in the worktree.
    for entry in git('ls-files', '-s', '-z').split(b'\0'):
        if not entry: continue
        meta, raw_path = entry.split(b'\t'); mode, blob, stage = meta.decode().split()
        path = raw_path.decode()
        if mode != '100644' or stage != '0': errors.append(f'{path}: unsupported index entry')
        errors.extend(inspect_content(path + ' (index)', git('cat-file', 'blob', blob)))
    commits = git('rev-list', '--all').decode().splitlines()
    baseline_commits = set(git('rev-list', HISTORICAL_BASELINE).decode().splitlines())
    blobs = set()
    checked_strict = set()
    checked_historical = set()
    for commit in commits:
        historical = commit in baseline_commits
        errors.extend(inspect_content('commit metadata', git('cat-file', 'commit', commit), historical=historical))
        for entry in git('ls-tree', '-r', '-z', commit).split(b'\0'):
            if not entry: continue
            meta, raw_path = entry.split(b'\t'); mode, kind, blob = meta.decode().split()
            path = raw_path.decode()
            if path not in allowed or mode != '100644' or kind != 'blob':
                errors.append(f'{commit}: unsafe historical path or mode: {path}')
            blobs.add(blob)
            # A new commit may reuse an old blob. Scan it strictly if it has
            # not already been checked as post-baseline content.
            if historical:
                if blob not in checked_historical and blob not in checked_strict:
                    checked_historical.add(blob)
                    errors.extend(inspect_content(path + ' (history)', git('cat-file', 'blob', blob), historical=True))
            elif blob not in checked_strict:
                checked_strict.add(blob)
                errors.extend(inspect_content(path + ' (history)', git('cat-file', 'blob', blob)))
    if (ROOT / '.git/objects/info/alternates').exists(): errors.append('alternate object stores forbidden')
    if pre_upload and git('remote').strip():
        errors.append('pre-upload: candidate must have no remote before upload authorization')
    print(json.dumps({'mode': 'pre-upload' if pre_upload else 'default',
                      'files': len(files), 'commits': len(commits), 'history_blobs': len(blobs),
                      'passed': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
    return bool(errors)

if __name__ == '__main__':
    raise SystemExit(main())
