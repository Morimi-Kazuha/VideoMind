"""Java-compatible perceptual image hashing for OCR frame de-duplication."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from dovideo.application.ports.media import ImageHashPort

from .errors import ImageHashError, ImageHashUnavailable

HASH_WIDTH = 9
HASH_HEIGHT = 8
DEFAULT_HAMMING_THRESHOLD = 5


class PillowDifferenceHash:
    """Compute the Java ``differenceHash`` over a local image.

    Pillow is imported lazily so domain/application imports remain free of
    image-library dependencies.  The optional dependency is declared by the
    project; callers receive a clear project error if it is absent at runtime.
    """

    def difference_hash(self, image_path: Path) -> int:
        path = Path(image_path)
        if not path.is_file():
            raise ImageHashError("OCR image does not exist")
        try:
            from PIL import Image
        except ImportError as exc:
            raise ImageHashUnavailable(
                "Pillow is required for image difference hashing"
            ) from exc
        try:
            with Image.open(path) as image:
                grayscale = image.convert("L")
                try:
                    resampling = Image.Resampling.BILINEAR
                except AttributeError:  # Pillow < 9 compatibility
                    resampling = Image.BILINEAR
                scaled = grayscale.resize((HASH_WIDTH, HASH_HEIGHT), resampling)
                return difference_hash_from_grayscale(
                    tuple(
                        tuple(int(scaled.getpixel((x, y))) for x in range(HASH_WIDTH))
                        for y in range(HASH_HEIGHT)
                    )
                )
        except ImageHashError:
            raise
        except Exception as exc:
            raise ImageHashError(f"could not decode OCR image {path.name}") from exc


def difference_hash_from_grayscale(pixels: Sequence[Sequence[int]]) -> int:
    """Return the 64-bit dHash for an already scaled 9x8 grayscale matrix."""

    if len(pixels) != HASH_HEIGHT or any(len(row) != HASH_WIDTH for row in pixels):
        raise ValueError("difference hash requires a 9x8 grayscale matrix")
    value = 0
    for row in pixels:
        for left, right in zip(row, row[1:]):
            if not isinstance(left, int) or not isinstance(right, int):
                raise TypeError("grayscale pixels must be integers")
            value <<= 1
            if left > right:
                value |= 1
    return value


def hamming_distance(left: int, right: int) -> int:
    """Count differing bits in two 64-bit hash values."""

    if not isinstance(left, int) or not isinstance(right, int):
        raise TypeError("image hashes must be integers")
    return (left ^ right).bit_count()


def is_duplicate_hash(
    previous: int,
    current: int,
    *,
    threshold: int = DEFAULT_HAMMING_THRESHOLD,
) -> bool:
    """Apply Java's duplicate rule: Hamming distance ``<= 5`` by default."""

    if threshold < 0:
        raise ValueError("hamming threshold cannot be negative")
    return hamming_distance(previous, current) <= threshold


__all__ = [
    "DEFAULT_HAMMING_THRESHOLD",
    "HASH_HEIGHT",
    "HASH_WIDTH",
    "PillowDifferenceHash",
    "difference_hash_from_grayscale",
    "hamming_distance",
    "is_duplicate_hash",
]
