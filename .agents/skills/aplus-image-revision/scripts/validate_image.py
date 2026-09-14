#!/usr/bin/env python3
"""Validate an Amazon A+ external-AI candidate image.

Requires Python 3.11+ and Pillow. The reusable ``validate_image`` function is
also used by submit_result.py before a completed result is uploaded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import warnings
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageStat
except ImportError:  # pragma: no cover - depends on local environment
    Image = None
    ImageStat = None


EXPECTED_FORMAT = "PNG"
EXPECTED_SIZE = (970, 600)
MIN_BYTES = 10_000
MAX_BYTES = 30 * 1024 * 1024
MIN_GRAYSCALE_VARIANCE = 2.0
MAX_PIXELS = 10_000_000


class ImageValidationError(ValueError):
    """Raised when a candidate cannot safely be submitted to image-reviewer."""


def file_sha256(path: Path) -> str:
    """Return a lowercase SHA-256 digest without loading the whole file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_image(
    image_path: str | Path,
    *,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate a candidate using the same file-level limits as the server.

    ``source_sha256`` is optional for standalone validation. When provided, a
    byte-identical candidate is rejected because image-reviewer will not apply
    it as a completed revision.
    """
    if Image is None or ImageStat is None:
        raise ImageValidationError(
            "Pillow is required. Install it in the Python environment used for validation."
        )

    path = Path(image_path)
    if not path.is_file():
        raise ImageValidationError(f"Image file does not exist or is not a regular file: {path}")

    try:
        size_bytes = path.stat().st_size
    except OSError as error:
        raise ImageValidationError(f"Cannot read image metadata: {error}") from error

    if size_bytes < MIN_BYTES or size_bytes > MAX_BYTES:
        raise ImageValidationError(
            f"Image size must be between {MIN_BYTES} and {MAX_BYTES} bytes; got {size_bytes} bytes"
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = image.format
                width, height = image.size
                if width * height > MAX_PIXELS:
                    raise ImageValidationError(f"Image pixel count exceeds {MAX_PIXELS}: {width}x{height}")
                image.load()
                grayscale_variance = float(ImageStat.Stat(image.convert("L")).var[0])
    except ImageValidationError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ImageValidationError(f"Image cannot be decoded: {error}") from error

    if image_format != EXPECTED_FORMAT:
        raise ImageValidationError(f"Image must be a real PNG; decoded format is {image_format!r}")
    if (width, height) != EXPECTED_SIZE:
        raise ImageValidationError(
            f"Image must be exactly {EXPECTED_SIZE[0]}x{EXPECTED_SIZE[1]}; got {width}x{height}"
        )
    if grayscale_variance < MIN_GRAYSCALE_VARIANCE:
        raise ImageValidationError(
            "Image is near-solid/near-flat "
            f"(grayscale variance {grayscale_variance:.6g}, minimum {MIN_GRAYSCALE_VARIANCE:g})"
        )

    sha256 = file_sha256(path)
    if source_sha256 and sha256.lower() == source_sha256.strip().lower():
        raise ImageValidationError("Candidate bytes are identical to the task source image")

    return {
        "path": str(path),
        "format": image_format,
        "width": width,
        "height": height,
        "bytes": size_bytes,
        "variance": grayscale_variance,
        "sha256": sha256,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a PNG candidate for image-reviewer external A+ revision submission."
    )
    parser.add_argument("image", type=Path, help="Candidate image to validate")
    parser.add_argument(
        "--source-sha256",
        help="Optional source-image SHA-256; reject a candidate with identical bytes",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = validate_image(args.image, source_sha256=args.source_sha256)
    except ImageValidationError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps({"ok": True, "validation": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
