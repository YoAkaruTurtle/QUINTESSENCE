"""J11 delivery checks: the invariants P3-P6 rely on must not quietly rot.

These are structural checks only.  They do not build the app and they do not
inspect a packaged installer; they pin the repository facts that the J11
delivery notes depend on, so a later edit cannot silently invalidate them.

Run:  python tools/check_j11_delivery.py
Exit: 0 = every check passed, 1 = at least one failed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIDEcar = ROOT / "apps/desktop/src-tauri"

FAILURES: list[str] = []
CHECKS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    # --- P4: the single-writer runbook exists and names the real error code
    runbook = ROOT / "docs/desktop/J11-SINGLE-WRITER-RUNBOOK.md"
    check("P4 runbook exists", runbook.is_file(), str(runbook))
    if runbook.is_file():
        body = text(runbook)
        check("P4 runbook names DATA_LOCKED", "DATA_LOCKED" in body)
        check("P4 runbook has a recovery section", "## 3. 怎么恢复" in body)
        check("P4 runbook has an acceptance section", "## 4. 验收时怎么操作" in body)

    # --- P5: log/cache locations documented, exemption explicit
    p5 = ROOT / "docs/desktop/J11-LOG-AND-CACHE-LOCATIONS.md"
    check("P5 locations doc exists", p5.is_file(), str(p5))
    if p5.is_file():
        body = text(p5)
        check("P5 doc names the app log dir env", "QUINTESSENCE_DESKTOP_LOG_DIR" in body)
        check("P5 doc states the exemption decision", "明确豁免" in body)
        check("P5 doc gives cleanup commands", "Remove-Item" in body)

    # --- P6: brand assets stay out of the repository, by design
    gitignore = text(ROOT / ".gitignore")
    check(
        "P6 icons directory is gitignored",
        "apps/desktop/src-tauri/icons/" in gitignore,
    )
    public_ignore = ROOT / "apps/desktop/public/.gitignore"
    check("P6 public brand art is gitignored", public_ignore.is_file() and "brand.png" in text(public_ignore))

    build_rs = SIDEcar / "build.rs"
    check("P6 build script exists", build_rs.is_file(), str(build_rs))
    if build_rs.is_file():
        body = text(build_rs)
        check("P6 build script generates the icons", "ensure_icons" in body)
        check("P6 build script keeps complete art untouched", "if complete" in body)
        check("P6 build script avoids rewrite churn", "write_if_changed" in body)
        required = ["32x32.png", "128x128.png", "128x128@2x.png", "icon.ico"]
        check(
            "P6 the four required icon files are the completeness gate",
            all(name in body for name in required),
        )

    p6 = ROOT / "docs/desktop/J11-BRAND-AND-REPRODUCIBLE-BUILD.md"
    check("P6 build notes exist", p6.is_file(), str(p6))
    if p6.is_file():
        body = text(p6)
        check("P6 notes explain the rebuild trigger", "touch" in body or "cargo clean" in body)
        check("P6 notes state the assets are not delivered", "不随仓库交付" in body)

    # --- P3: the three budgets must stay distinguishable
    bridge = text(ROOT / "apps/desktop/sidecar/pcs_bridge.py")
    for code in (
        "MODEL_CALLS_EXHAUSTED",
        "MODEL_CONTEXT_BUDGET_EXCEEDED",
        "MODEL_REQUEST_BUDGET_EXCEEDED",
        "MODEL_AUTHORIZATION_EXPIRED",
        "MODEL_AUTHORIZATION_MISMATCH",
    ):
        check(f"P3 bridge exposes {code}", code in bridge)
    port = text(ROOT / "src/quintessence_pcs/runtime/model_port.py")
    check(
        "P3 budget errors carry a reason",
        "class ModelBudgetExceededError" in port and "reason" in port,
    )

    # --- public delivery: required files and no stale merge claims
    for name in ("LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md"):
        check(f"public file present: {name}", (ROOT / name).is_file())
    stale = [word for word in ("P0 未合并", "未合并 P0") if any(
        word in text(path)
        for path in [ROOT / "README.md", ROOT / "README.zh-CN.md"]
        if path.is_file()
    )]
    check("no stale 'P0 not merged' claim", not stale, ",".join(stale))

    # --- the gate itself must still be runnable
    check("repository gate exists", (ROOT / "tools/check_repository.py").is_file())

    print("\n".join(CHECKS))
    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print(f"{len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
