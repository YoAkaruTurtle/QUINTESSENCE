"""Install and verify the checkout's vendored Scope Recall package."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


_CHECK = r"""
from importlib import metadata, util
import hashlib
import json
from pathlib import Path
import sys

third_party = Path(sys.argv[1])
try:
    manifest = json.loads((third_party / "scope-recall-upstream.json").read_text(encoding="utf-8"))
    allowlist = json.loads(
        (third_party / "scope-recall/packaging/v11-module-allowlist.json").read_text(encoding="utf-8")
    )
    runtime = set(allowlist["python_modules"]) | set(allowlist["package_data"])
    package = metadata.distribution(manifest["distribution"])
    module = util.find_spec("scope_recall")
    installed = package.locate_file("scope_recall/__init__.py").resolve()
    reported = {
        "/".join(path.parts[1:]) for path in package.files or ()
        if path.parts[0] == "scope_recall" and "__pycache__" not in path.parts
    }
    if package.version != manifest["version"] or reported != runtime:
        raise ValueError("installed version or runtime files differ from the vendored package")
    if module is None or not module.origin or Path(module.origin).resolve() != installed:
        raise ValueError("Scope Recall import does not resolve to the verified distribution")
    for name in sorted(runtime):
        source = installed.parent / name
        digest = hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if digest != manifest["sha256_normalized_lf"][name]:
            raise ValueError(f"installed Scope Recall file differs from the vendored source: {name}")
    deps = ["yaml", "jsonschema", "packaging", *(["tzdata"] if sys.platform == "win32" else [])]
    missing = [name for name in deps if util.find_spec(name) is None]
    if missing:
        raise ValueError(f"missing dependencies: {', '.join(missing)}")
except (OSError, ValueError, KeyError, TypeError, metadata.PackageNotFoundError) as error:
    print(f"Scope Recall validation failed: {error}", file=sys.stderr)
    sys.exit(1)

print(f"Python: {sys.executable}")
print(f"Scope Recall {package.version}: {installed.parent}")
print(f"Verified upstream revision: {manifest['revision']}")
"""



def venv_python(repo: Path) -> Path:
    suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return repo / ".venv" / suffix


def select_python(repo: Path, override: str | None = None) -> str:
    requested = override or os.environ.get("QUINTESSENCE_DESKTOP_PYTHON")
    if requested:
        return requested
    local = venv_python(repo)
    return str(local) if local.is_file() else sys.executable


def check_python(repo: Path, python: str) -> int:
    return subprocess.call(
        [python, "-I", "-B", "-c", _CHECK, str(repo / "third_party")],
        cwd=repo,
    )


def build_root(repo: Path) -> Path:
    checkout = hashlib.sha256(os.fsencode(os.path.normcase(repo))).hexdigest()[:16]
    root = (Path(tempfile.gettempdir()) / "quintessence-builds" / checkout).resolve()
    if root.is_relative_to(repo) or repo.is_relative_to(root):
        raise ValueError("build output and source checkout must be disjoint directory trees")
    return root


def stage_vendor(repo: Path, destination: Path) -> None:
    third_party = repo / "third_party"
    vendor = third_party / "scope-recall"
    manifest = json.loads((third_party / "scope-recall-upstream.json").read_text(encoding="utf-8"))
    for name, digest in manifest["sha256_normalized_lf"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid source path in the Scope Recall inventory")
        data = (vendor / relative).read_bytes()
        if hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest() != digest:
            raise ValueError(f"vendored Scope Recall file does not match the inventory: {name}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def install(repo: Path, *, wheelhouse: Path | None, build: bool) -> int:
    local = venv_python(repo)
    if not local.is_file():
        directory = repo / ".venv"
        if directory.exists():
            raise ValueError("the checkout .venv exists without its Python executable")
        venv.EnvBuilder(with_pip=True).create(directory)
    command = [str(local), "-I", "-B", "-m", "pip", "install", "--disable-pip-version-check"]
    if wheelhouse:
        wheelhouse = wheelhouse.resolve(strict=True)
        if not wheelhouse.is_dir():
            raise ValueError("wheelhouse must be a directory")
        command += ["--no-index", "--find-links", str(wheelhouse)]
    build_requirements = ["pyinstaller==6.16.0", "Pillow"] if build else []
    root = build_root(repo)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scope-recall-install-", dir=root) as temp:
        stage = Path(temp)
        stage_vendor(repo, stage)
        result = subprocess.call([*command, str(stage), *build_requirements], cwd=stage)
    return result if result else check_python(repo, str(local))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="verify a prepared interpreter")
    modes.add_argument("--show-python", action="store_true", help="print the selected interpreter")
    modes.add_argument("--show-build-root", action="store_true", help="print the external build root")
    parser.add_argument("--python", help="interpreter override for --check or --show-python")
    parser.add_argument("--wheelhouse", type=Path, help="install using only this local wheel directory")
    parser.add_argument("--build", action="store_true", help="also install desktop packaging tools")
    args = parser.parse_args()
    if args.python and not (args.check or args.show_python):
        parser.error("--python is only used with --check or --show-python")
    if (args.check or args.show_python or args.show_build_root) and (args.wheelhouse or args.build):
        parser.error("--wheelhouse and --build are only used when installing")

    repo = Path(__file__).resolve().parents[1]
    try:
        if args.show_build_root:
            print(build_root(repo))
        elif args.show_python:
            print(select_python(repo, args.python))
        elif args.check:
            result = check_python(repo, select_python(repo, args.python))
            if result:
                print("Prepare this checkout with python tools/setup_scope_recall.py.", file=sys.stderr)
            return result
        else:
            return install(repo, wheelhouse=args.wheelhouse, build=args.build)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Scope Recall setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
