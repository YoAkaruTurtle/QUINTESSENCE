"""Fail closed on unexpected files, unsafe payloads and reachable Git history.

This structural/secret-pattern gate supplements human content and rights review.
It cannot certify the absence of every possible secret or copyrighted passage.
"""
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Approved public documents may name the private instance with one exact phrase.
# Any other legacy-term occurrence in these files, and every occurrence in any
# other path, still fails the gate.
PUBLIC_DOC_PATHS = frozenset({'README.md', 'ARCHITECTURE.md', 'ROADMAP.md'})
# Built from parts so this gate's own source does not match the legacy terms.
PUBLIC_LEGACY_PHRASES = ('千' + '岁' + '（Chi' + 'tose' + '）',)

def git(*args):
    return subprocess.check_output(['git', '--no-optional-locks', '-C', str(ROOT), *args])

def inspect_content(path, data):
    errors = []
    if b'\0' in data:
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
    legacy_patterns = [
        r'(?i)' + 'chi' + 'tose',
        '千' + '岁',
    ]
    for index, pattern in enumerate(secret_patterns):
        if re.search(pattern, text): errors.append(f'{path}: sensitive pattern {index}')
    scan = text
    if path.split(' (', 1)[0] in PUBLIC_DOC_PATHS:
        for phrase in PUBLIC_LEGACY_PHRASES:
            scan = scan.replace(phrase, '')
    for index, pattern in enumerate(legacy_patterns):
        if re.search(pattern, scan): errors.append(f'{path}: legacy pattern {index}')
    return errors

def main():
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
    blobs = set()
    for commit in commits:
        errors.extend(inspect_content('commit metadata', git('cat-file', 'commit', commit)))
        for entry in git('ls-tree', '-r', '-z', commit).split(b'\0'):
            if not entry: continue
            meta, raw_path = entry.split(b'\t'); mode, kind, blob = meta.decode().split()
            path = raw_path.decode()
            if path not in allowed or mode != '100644' or kind != 'blob':
                errors.append(f'{commit}: unsafe historical path or mode: {path}')
            if blob not in blobs:
                blobs.add(blob)
                errors.extend(inspect_content(path + ' (history)', git('cat-file', 'blob', blob)))
    if (ROOT / '.git/objects/info/alternates').exists(): errors.append('alternate object stores forbidden')
    if git('remote').strip(): errors.append('candidate must have no remote before upload authorization')
    print(json.dumps({'files': len(files), 'commits': len(commits), 'history_blobs': len(blobs),
                      'passed': not errors, 'errors': errors}, ensure_ascii=False, indent=2))
    return bool(errors)

if __name__ == '__main__':
    raise SystemExit(main())
