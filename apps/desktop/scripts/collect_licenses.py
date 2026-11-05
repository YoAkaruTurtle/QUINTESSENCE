"""Collect original notices for a Windows build; never download or relicense code.

Outputs are external build resources. Missing notices, version drift and unsafe
paths fail the build. Cargo metadata is lockfile-based; Python is the validated
build environment. Sources for MPL components are included in the inventory.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

REPO = Path(__file__).resolve().parents[3]
TOKENS = ('license', 'licence', 'copying', 'notice', 'copyright', 'unlicense')

def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def text_digest(path: Path) -> str:
    # Git normalizes checkout newlines; terms/whitespace remain fully checked.
    return hashlib.sha256(path.read_text(encoding='utf-8-sig').encode('utf-8')).hexdigest()

def license_files(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_file() and any(t in p.name.lower() for t in TOKENS))

def safe_name(value: str) -> str:
    if not value or not re.fullmatch(r'[A-Za-z0-9_.+-]+', value) or value in ('.', '..'):
        raise ValueError('unsafe notice component name')
    return value

def copy_component(output: Path, name: str, version: str, license_expression: str,
                   source: str, files: list[tuple[str, Path]]) -> dict:
    directory = output / safe_name(name) / safe_name(version)
    if not files:
        raise ValueError('missing original license: ' + name + '@' + version)
    if not license_expression or not source:
        raise ValueError('missing license/source metadata: ' + name)
    copied = []
    for label, path in files:
        relative = Path(label)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('notice destination escaped component')
        data = path.read_bytes()
        if not data.strip():
            raise ValueError('empty license: ' + name)
        data.decode('utf-8-sig')
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        copied.append({'file': target.relative_to(output).as_posix(), 'sha256': digest(target)})
    return {'name': name, 'version': version, 'license': license_expression,
            'source': source, 'notices': copied}

def cargo_components(repo: Path, output: Path, metadata_document: dict, fallback: dict) -> list[dict]:
    records = []
    for package in metadata_document['packages']:
        if package.get('source') is None:
            continue  # first-party package, handled separately
        name, version = package['name'], package['version']
        root = Path(package['manifest_path']).parent
        files = [(p.name, p) for p in license_files(root)]
        if package.get('license_file'):
            path = root / package['license_file']
            if path.is_file() and path not in [p for _, p in files]:
                files.append((path.name, path))
        if not files:
            entry = fallback.get(name + '@' + version)
            if not entry:
                raise ValueError('review required: missing Cargo license ' + name + '@' + version)
            path = repo / 'licenses' / entry['file']
            if text_digest(path) != entry['text_sha256']:
                raise ValueError('fallback license hash mismatch')
            files = [(path.name, path)]
        source = 'https://crates.io/api/v1/crates/' + name + '/' + version + '/download'
        record = copy_component(output, 'cargo-' + name, version, package.get('license') or 'SEE LICENSE FILE', source, files)
        if 'MPL-2.0' in (package.get('license') or ''):
            record['source_availability'] = 'Unmodified covered source is available from the exact-version source archive above under MPL-2.0.'
        records.append(record)
    return records

def collect(repo: Path, output: Path) -> dict:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    if output.resolve().is_relative_to(repo.resolve()) or repo.resolve().is_relative_to(output.resolve()):
        raise ValueError('notices output must be disjoint from source checkout')
    if output.exists() and any(output.iterdir()):
        raise ValueError('notices output must be empty')
    output.mkdir(parents=True, exist_ok=True)
    scope = metadata.distribution('hermes-scope-recall')
    if scope.version != '3.14.1':
        raise ValueError('Scope Recall version changed; review required')
    if sys.version_info[:3] != (3, 12, 10):
        raise ValueError('native notices validated for CPython 3.12.10; review another runtime first')
    import ssl, sqlite3, pyexpat, zlib
    if (ssl.OPENSSL_VERSION.split()[1], sqlite3.sqlite_version, pyexpat.EXPAT_VERSION, zlib.ZLIB_RUNTIME_VERSION) != ('3.0.16','3.49.1','expat_2.7.1','1.3.1'):
        raise ValueError('native dependency versions changed; review original notices')
    records = []
    queue = ['hermes-scope-recall']; seen = set()
    while queue:
        name = canonicalize_name(queue.pop())
        if name in seen:
            continue
        seen.add(name);dist = metadata.distribution(name)
        licenses = [(str(p).split('.dist-info/', 1)[-1], dist.locate_file(p)) for p in dist.files or []
                    if '.dist-info/' in str(p).replace('\\', '/') and any(t in str(p).lower() for t in TOKENS)
                    and not str(p).endswith(('.py', '.pyc'))]
        expression = dist.metadata.get('License-Expression') or dist.metadata.get('License') or ''
        source = ('https://github.com/410979729/scope-recall-hermes/tree/2b640260134565ac7fef3a76644e352a50866533'
                  if name == 'hermes-scope-recall' else 'https://pypi.org/project/' + name + '/' + dist.version + '/')
        records.append(copy_component(output, 'python-' + name, dist.version, expression, source, licenses))
        if name == 'hermes-scope-recall':
            expected = (repo / 'licenses/Scope-Recall-MIT.txt').read_text(encoding='utf-8')
            if not any(path.read_text(encoding='utf-8') == expected for _, path in licenses):
                raise ValueError('Scope Recall original MIT notice changed')
        for dependency in dist.requires or []:
            req = Requirement(dependency)
            if req.marker is None or req.marker.evaluate({'extra': ''}):
                if metadata.version(req.name) not in req.specifier:
                    raise ValueError('installed runtime dependency does not satisfy its requirement')
                queue.append(req.name)
    for name in ('pyinstaller', 'pyinstaller-hooks-contrib'):
        dist = metadata.distribution(name)
        files = [(Path(str(f)).name, dist.locate_file(f)) for f in dist.files or [] if '.dist-info/' in str(f)
                 and any(t in Path(str(f)).name.lower() for t in TOKENS)]
        records.append(copy_component(output, name, dist.version, 'SEE ORIGINAL LICENSE AND BOOTLOADER EXCEPTION',
                                      'https://pypi.org/project/' + name + '/' + dist.version + '/', files))
    records.append(copy_component(output, 'cpython', '3.12.10', 'PSF-2.0 AND bundled notices',
                                  'https://www.python.org/downloads/release/python-31210/', [('LICENSE.txt', Path(sys.base_prefix) / 'LICENSE.txt')]))
    records.append(copy_component(output, 'quintessence', 'source', 'Apache-2.0',
                                  'https://github.com/YoAkaruTurtle/QUINTESSENCE', [(n, repo / n) for n in ('LICENSE','NOTICE','THIRD_PARTY_NOTICES.md')]))
    native = [('openssl', '3.0.16', 'Apache-2.0', 'OpenSSL-LICENSE.txt'),
              ('expat', '2.7.1', 'MIT', 'expatLicense.txt'),
              ('xz-liblzma', '5.2.5', 'Public Domain; see component boundary', 'xzLicense.txt'),
              ('zlib', '1.3.1', 'Zlib', 'zlibLicense.txt')]
    sources = {x['name']:x['source'] for x in json.loads((repo / 'licenses/upstream-sources.json').read_text())}
    for name, version, expression, filename in native:
        records.append(copy_component(output, name, version, expression, sources[filename], [(filename, repo / 'licenses' / filename)]))
    # The SDK loader is Microsoft code, separate from the Rust wrapper's MIT license.
    records.append(copy_component(output, 'microsoft-webview2-sdk', '1.0.3800.47', 'BSD-3-Clause and bundled notices',
        'https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.3800.47',
        [(n, repo / 'licenses' / n) for n in ('WebView2-SDK-LICENSE.txt','WebView2-SDK-NOTICE.txt')]))
    record = copy_component(output, 'nsis', '3.13', 'Zlib and original compressor licenses',
        'https://sourceforge.net/projects/nsis/files/NSIS%203/3.13/', [('COPYING', repo / 'licenses/NSIS-COPYING.txt')])
    record['source_availability'] = 'Unmodified NSIS 3.13 source, including compressor source, is available from the official source archive in the linked release directory.'
    records.append(record)
    doc = json.loads(subprocess.check_output(['cargo','metadata','--offline','--locked','--format-version','1',
                                             '--filter-platform','x86_64-pc-windows-msvc'], cwd=repo / 'apps/desktop/src-tauri'))
    sdk = next(p for p in doc['packages'] if p['name'] == 'webview2-com-sys')
    loader = Path(sdk['manifest_path']).parent / 'x64/WebView2LoaderStatic.lib'
    if sdk['version'] != '0.39.1' or digest(loader) != '89c6b872783b8f6c3cedbff618adb42082d455c615453ae10cfc753f1e8f25d8':
        raise ValueError('WebView2 SDK loader changed; review native license')
    fallback = json.loads((repo / 'licenses/cargo-license-fallbacks.json').read_text())
    # rpds-py embeds its own Rust graph, identified by the installed wheel's SBOM.
    dist = metadata.distribution('rpds-py')
    sbom = next(dist.locate_file(f) for f in dist.files or [] if str(f).endswith('.cyclonedx.json'))
    cargo_home = Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo'))
    package_ids = {(p['name'],p['version']) for p in doc['packages']}
    for component in json.loads(sbom.read_text())['components']:
        name, version = component['name'],component['version']
        if (name,version) in package_ids:
            continue
        roots = list((cargo_home / 'registry/src').glob('*/' + name + '-' + version))
        if len(roots) != 1:
            raise ValueError('rpds-py source notices missing: ' + name + '@' + version)
        doc['packages'].append({'name':name,'version':version,'source':'registry',
            'manifest_path':str(roots[0] / 'Cargo.toml'),
            'license':' AND '.join(x['expression'] for x in component.get('licenses',[]) if 'expression' in x)})
    records += cargo_components(repo, output, doc, fallback)
    npm_root = repo / 'apps/desktop/node_modules/.pnpm'
    packages = {}
    for manifest in sorted([*npm_root.glob('*/node_modules/*/package.json'), *npm_root.glob('*/node_modules/@*/*/package.json')]):
        data = json.loads(manifest.read_text())
        packages[(data['name'], data['version'])] = (data, manifest.parent)
    if ('@tauri-apps/api', '2.12.4') not in packages:
        raise ValueError('review required: frontend runtime package/version changed')
    for (name, version), (data, root) in sorted(packages.items()):
        files = license_files(root)
        # Platform build executables are copies of their named parent projects,
        # published at the same version, not independent runtime dependencies.
        if not files:
            parent = ('esbuild' if name.startswith('@esbuild/') else
                      'rollup' if name.startswith('@rollup/') else
                      '@tauri-apps/cli' if name.startswith('@tauri-apps/cli-') else None)
            if not parent or (parent, version) not in packages:
                raise ValueError('review required: missing npm license ' + name)
            parent_data, parent_root = packages[(parent, version)]
            if data.get('license') != parent_data.get('license'):
                raise ValueError('npm platform/parent license mismatch')
            files = license_files(parent_root)
        records.append(copy_component(output, 'npm-' + name.replace('@','').replace('/','-'), version,
            data.get('license') or '', 'https://www.npmjs.com/package/' + name + '/v/' + version,
            [(f.name,f) for f in files]))
    rustdoc = Path(subprocess.check_output(['rustc','--print','sysroot'],text=True).strip()) / 'share/doc/rust'
    rustfiles = [('COPYRIGHT-library.html', rustdoc / 'COPYRIGHT-library.html')]
    rustfiles += [('licenses/' + f.name,f) for f in (rustdoc / 'licenses').iterdir() if f.is_file()]
    records.append(copy_component(output,'rust-standard-library',subprocess.check_output(['rustc','--version'],text=True).split()[1],
                                  'MIT OR Apache-2.0; see original bundled notices','https://github.com/rust-lang/rust',rustfiles))
    result = {'schema':'quintessence.build-notices.v1','scope_recall_version':scope.version,
              'note':'Includes runtime and conservative build/target dependency notices; inclusion does not claim every component is linked. Third-party code retains its original license.',
              'components':records}
    (output / 'inventory.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output / 'README.txt').write_text('Original third-party notices and exact-version source links are indexed in inventory.json.\n'
        'MPL-2.0 covered source is available, unmodified, from each listed exact-version crate archive under MPL-2.0.\n'
        'First-party Apache-2.0 does not replace third-party terms, including Microsoft Distributable Code terms in the CPython notice.\n',encoding='utf-8')
    return result

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root',type=Path,default=REPO)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=collect(args.repo_root,args.output)
    print(json.dumps({'components':len(result['components']),'scope_recall_version':result['scope_recall_version']}))
