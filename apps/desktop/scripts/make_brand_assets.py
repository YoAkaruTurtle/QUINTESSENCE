"""Generate desktop icons and the top-bar brand image from the owner's artwork.

The source image is supplied by the project owner and is intentionally not
committed to this public repository.  This script only scales the original
proportionally; it does not redraw, crop or restyle the brand mark.  Without the
source, `build.rs` keeps a neutral fallback icon so a clean checkout can still
compile, but a formal package should be built with this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--app-root", required=True, type=Path)
    parser.add_argument("--evidence", type=Path, default=None)
    args = parser.parse_args()

    source = args.source
    if not source.is_file():
        raise SystemExit(f"brand source not found: {source}")
    image = Image.open(source)
    image.load()
    if image.width != image.height:
        raise SystemExit("brand source must be square to preserve its proportions")
    image = image.convert("RGBA")

    public = args.app_root / "public"
    icons = args.app_root / "src-tauri" / "icons"
    public.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)

    # Top-bar / empty-state image.  Scale the complete artwork, never crop it.
    topbar = image.resize((256, 256), Image.Resampling.LANCZOS)
    topbar.save(public / "brand.png", optimize=True)

    generated: list[dict[str, object]] = []
    png_sizes = [16, 24, 32, 48, 64, 128, 256, 512]
    for size in png_sizes:
        scaled = image.resize((size, size), Image.Resampling.LANCZOS)
        name = f"{size}x{size}.png"
        scaled.save(icons / name, optimize=True)
        generated.append({"file": name, "size": size})
    # Tauri's conventional names.
    image.resize((256, 256), Image.Resampling.LANCZOS).save(
        icons / "128x128@2x.png", optimize=True
    )
    image.resize((512, 512), Image.Resampling.LANCZOS).save(
        icons / "icon.png", optimize=True
    )
    ico = image.resize((256, 256), Image.Resampling.LANCZOS)
    ico.save(
        icons / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    report = {
        "source": str(source),
        "source_sha256": digest,
        "source_dimensions": [image.width, image.height],
        "operation": "proportional LANCZOS downscale only; no crop or redraw",
        "public_brand": str(public / "brand.png"),
        "icons": generated,
        "ico": "icon.ico",
    }
    if args.evidence is not None:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
