#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageOps

try:
    import cv2
    import numpy as np
except Exception:  # Optional dependency; thumbnail extraction falls back to the full image.
    cv2 = None
    np = None

try:
    from pyzbar.pyzbar import decode as pyzbar_decode
except Exception:  # Optional dependency; QR extraction still falls back to OpenCV/OCR.
    pyzbar_decode = None

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except Exception:  # Optional dependency; handled at runtime with a clear error.
    pytesseract = None
    TESSERACT_AVAILABLE = False

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
URL_RE = re.compile(r"(?:https?\s*[:;]\s*/\s*/|www\.)[A-Za-z0-9\s\n\r\t:/._~%?#[\]@!$&\'()*+,;=-]+", re.I)
BARE_DOMAIN_RE = re.compile(r"\b(?:[A-Za-z0-9-]+\.)+(?:com|org|net|co|io|edu|gov|us|uk|ca|au|de|fr|it|nl|se|no|fi|me|app|dev)(?:/[A-Za-z0-9._~%?#\[\]@!$&\'()*+,;=:/-]*)?", re.I)
LOGGER = logging.getLogger(__name__)


class CaptureRecipeError(Exception):
    pass


def slugify(value: str, fallback: str = "recipe") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    return slug or fallback


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def capture_upload_root(recipe_cache_dir: Path) -> Path:
    return Path(recipe_cache_dir).expanduser().resolve() / "capture_uploads"


def capture_dir_for_recipe(recipe_cache_dir: Path, recipe_id: str) -> Path:
    return capture_upload_root(recipe_cache_dir) / slugify(recipe_id)


def _safe_extension(filename: str, content_type: str = "") -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in ALLOWED_IMAGE_EXTENSIONS:
        return ".jpg" if ext == ".jpeg" else ext
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "tiff" in content_type or "tif" in content_type:
        return ".tif"
    return ".jpg"


def save_capture_images(files: Iterable[Any], recipe_cache_dir: Path, recipe_id: str) -> dict[str, Any]:
    """Save uploaded recipe-card photos under recipe_cache/capture_uploads/<recipe_id>."""
    target_dir = capture_dir_for_recipe(recipe_cache_dir, recipe_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    saved_paths: list[str] = []
    for index, uploaded in enumerate(files, start=1):
        if uploaded is None or not getattr(uploaded, "filename", ""):
            continue
        ext = _safe_extension(uploaded.filename, getattr(uploaded, "content_type", ""))
        raw_path = target_dir / f"image_{index:03d}{ext}"
        uploaded.save(raw_path)

        # Normalize orientation and convert unusual formats to JPEG/PNG where possible.
        try:
            with Image.open(raw_path) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode not in {"RGB", "L"}:
                    img = img.convert("RGB")
                # Preserve as much image detail as practical for OCR. Do not downscale
                # recipe-card photos during upload; the background cache process can
                # decide how to render/fit the OCR output later.
                normalized_path = target_dir / f"image_{index:03d}.jpg"
                img.convert("RGB").save(normalized_path, format="JPEG", quality=95)
            if raw_path != normalized_path:
                raw_path.unlink(missing_ok=True)
            saved_paths.append(str(normalized_path))
        except Exception as exc:
            raw_path.unlink(missing_ok=True)
            raise CaptureRecipeError(f"Could not process uploaded image {uploaded.filename}: {exc}") from exc

    if not saved_paths:
        raise CaptureRecipeError("Upload at least one recipe photo.")

    manifest = {
        "recipe_id": recipe_id,
        "capture_dir": str(target_dir),
        "source_image_paths": saved_paths,
        "created_at": utc_now_iso(),
    }
    (target_dir / "capture_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest




def _text_mask_from_ocr(img: Image.Image) -> Any:
    """Return a mask of OCR text regions so thumbnails can avoid text-heavy crops."""
    if pytesseract is None or cv2 is None or np is None:
        return None
    try:
        data = pytesseract.image_to_data(img.convert("L"), output_type=pytesseract.Output.DICT, config="--psm 6")
    except Exception:
        return None

    mask = np.zeros((img.height, img.width), dtype=np.uint8)
    count = len(data.get("text", []))
    for idx in range(count):
        text = str(data.get("text", [""])[idx] or "").strip()
        if not text:
            continue
        try:
            conf = float(data.get("conf", ["-1"])[idx])
        except Exception:
            conf = -1
        if conf < 20:
            continue
        x = int(data["left"][idx])
        y = int(data["top"][idx])
        w = int(data["width"][idx])
        h = int(data["height"][idx])
        if w <= 0 or h <= 0:
            continue
        pad = max(4, int(max(w, h) * 0.25))
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(img.width, x + w + pad)
        y2 = min(img.height, y + h + pad)
        mask[y1:y2, x1:x2] = 255
    return mask


def _fallback_capture_thumbnail(img: Image.Image, target: Path) -> Path:
    thumb = img.copy().convert("RGB")
    thumb.thumbnail((1600, 1600), Image.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    thumb.save(target, format="PNG")
    return target


def _page_bounds_for_capture(arr: Any) -> tuple[int, int, int, int]:
    """Return a loose bounding box for the bright recipe page, excluding dark counters/backgrounds."""
    if cv2 is None or np is None:
        h, w = arr.shape[:2]
        return 0, 0, w, h

    height, width = arr.shape[:2]
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]

    page_mask = ((val > 135) & (sat < 95)).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (35, 35))
    page_mask = cv2.morphologyEx(page_mask, cv2.MORPH_CLOSE, kernel)
    page_mask = cv2.morphologyEx(page_mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (13, 13)))

    contours, _ = cv2.findContours(page_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 0, 0, width, height

    page_area = width * height
    candidates: list[tuple[int, tuple[int, int, int, int]]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = w * h
        if area < page_area * 0.22:
            continue
        aspect = w / float(max(h, 1))
        if 0.35 <= aspect <= 1.65:
            candidates.append((area, (x, y, w, h)))

    if not candidates:
        return 0, 0, width, height

    _, (x, y, w, h) = max(candidates, key=lambda item: item[0])
    pad = max(10, int(min(width, height) * 0.015))
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(width, x + w + pad)
    y2 = min(height, y + h + pad)
    return x1, y1, x2 - x1, y2 - y1


def _photo_candidates_for_image(img: Image.Image) -> list[tuple[float, Image.Image]]:
    """Return scored color photo-like crops from one captured recipe image."""
    if cv2 is None or np is None:
        return []

    rgb = img.convert("RGB")
    arr_full = np.array(rgb)
    full_height, full_width = arr_full.shape[:2]
    if full_width < 80 or full_height < 80:
        return []

    text_mask_full = _text_mask_from_ocr(rgb)
    page_x, page_y, page_w, page_h = _page_bounds_for_capture(arr_full)
    page = arr_full[page_y:page_y + page_h, page_x:page_x + page_w]
    if page.size == 0:
        page = arr_full
        page_x = page_y = 0
        page_h, page_w = page.shape[:2]

    if text_mask_full is not None:
        text_mask = text_mask_full[page_y:page_y + page_h, page_x:page_x + page_w]
    else:
        text_mask = None

    hsv = cv2.cvtColor(page, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    height, width = page.shape[:2]
    page_area = max(width * height, 1)

    def build_photo_mask(saturation_floor: int) -> Any:
        # Colorful/non-white regions are usually embedded photos. Text is usually low-saturation.
        mask = ((sat > saturation_floor) & (val > 35) & (val < 252)).astype(np.uint8) * 255
        if text_mask is not None:
            mask[text_mask > 0] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        close_size = 13 if saturation_floor >= 50 else 21
        dilate_size = 5 if saturation_floor >= 50 else 9
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (close_size, close_size)))
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (dilate_size, dilate_size)), iterations=1)
        return mask

    candidates: list[tuple[float, Image.Image]] = []
    for saturation_floor in (70, 60, 50, 42, 34, 26, 18):
        mask = build_photo_mask(saturation_floor)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h
            if area < page_area * 0.0035 or w < 45 or h < 45:
                continue
            if area > page_area * 0.32:
                continue
            aspect = w / float(max(h, 1))
            if aspect < 0.28 or aspect > 3.8:
                continue

            crop = page[y:y + h, x:x + w]
            crop_hsv = hsv[y:y + h, x:x + w]
            region_mask = mask[y:y + h, x:x + w]
            filled_ratio = float(np.count_nonzero(region_mask)) / float(max(area, 1))
            mean_sat = float(np.mean(crop_hsv[:, :, 1]))
            median_sat = float(np.median(crop_hsv[:, :, 1]))
            std_rgb = float(np.mean(np.std(crop, axis=(0, 1))))
            dark_ratio = float(np.count_nonzero(crop_hsv[:, :, 2] < 70)) / float(max(area, 1))
            white_ratio = float(np.count_nonzero((crop_hsv[:, :, 1] < 25) & (crop_hsv[:, :, 2] > 225))) / float(max(area, 1))
            text_ratio = 0.0
            if text_mask is not None:
                text_crop = text_mask[y:y + h, x:x + w]
                text_ratio = float(np.count_nonzero(text_crop)) / float(max(area, 1))

            if filled_ratio < 0.04:
                continue
            if mean_sat < 14 and std_rgb < 16:
                continue
            if dark_ratio > 0.55 or white_ratio > 0.82 or text_ratio > 0.35:
                continue

            center_x = x + w / 2.0
            center_y = y + h / 2.0
            right_bias = 1.45 if center_x > width * 0.50 else 1.0
            upper_bias = 1.20 if center_y < height * 0.62 else 1.0
            rectangular_bonus = 1.16 if 0.55 <= aspect <= 2.2 else 1.0
            # Multiple photo captures should choose the most photo-like crop, not just the largest crop.
            score = (area / page_area) * (1.0 + mean_sat / 32.0) * (1.0 + median_sat / 48.0) * (1.0 + std_rgb / 42.0)
            score *= (1.0 + filled_ratio * 2.2) * right_bias * upper_bias * rectangular_bonus
            score *= (1.0 - min(text_ratio, 0.9)) * (1.0 - min(white_ratio, 0.9) * 0.55)
            score *= 1.0 + (saturation_floor / 120.0)

            pad = max(8, int(min(w, h) * 0.045))
            x1 = max(0, page_x + x - pad)
            y1 = max(0, page_y + y - pad)
            x2 = min(full_width, page_x + x + w + pad)
            y2 = min(full_height, page_y + y + h + pad)
            candidates.append((score, rgb.crop((x1, y1, x2, y2))))
        if candidates:
            # Higher saturation floors are more reliable; do not drop to loose matching unless needed.
            break
    return candidates


def _crop_best_photo_region(img: Image.Image) -> Image.Image | None:
    candidates = _photo_candidates_for_image(img)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def save_capture_recipe_image(manifest: dict[str, Any], recipe_cache_dir: Path, recipe_id: str) -> Path | None:
    """Save the best color dish thumbnail as recipe_cache/<recipe_id>.png."""
    image_paths = manifest.get("source_image_paths") or []
    if not image_paths:
        return None

    target = Path(recipe_cache_dir).expanduser().resolve() / f"{slugify(recipe_id)}.png"
    target.parent.mkdir(parents=True, exist_ok=True)

    first_valid: Image.Image | None = None
    best_crop: Image.Image | None = None
    best_score = float("-inf")

    for raw_path in image_paths:
        path = Path(str(raw_path)).expanduser()
        if not path.exists() or not path.is_file():
            continue
        try:
            with Image.open(path) as opened:
                img = ImageOps.exif_transpose(opened).convert("RGB")
                img.thumbnail((2200, 2200), Image.LANCZOS)
                if first_valid is None:
                    first_valid = img.copy()
                for score, crop in _photo_candidates_for_image(img):
                    if score > best_score:
                        best_crop = crop
                        best_score = score
        except Exception:
            continue

    if best_crop is None:
        if first_valid is None:
            return None
        return _fallback_capture_thumbnail(first_valid, target)

    best_crop = best_crop.convert("RGB")
    best_crop.thumbnail((1600, 1600), Image.LANCZOS)
    best_crop.save(target, format="PNG")
    return target


def _require_tesseract() -> None:
    if not TESSERACT_AVAILABLE or pytesseract is None:
        raise CaptureRecipeError(
            "pytesseract is not installed in this Python environment. "
            "Install it with: /home/pi/inky_admin/venv/bin/pip install pytesseract"
        )


def _safe_ocr(image: Image.Image, *, config: str = "--oem 3 --psm 6") -> str:
    """Run pytesseract without crashing the caller; used for best-effort OCR passes."""
    if not TESSERACT_AVAILABLE or pytesseract is None:
        return ""
    try:
        return pytesseract.image_to_string(image, config=config) or ""
    except Exception:
        return ""


def _prepare_ocr_variants(img: Image.Image) -> list[Image.Image]:
    """Create general OCR variants plus high-contrast variants that help small fractions."""
    img = ImageOps.exif_transpose(img).convert("RGB")
    max_side = max(img.size)
    if max_side > 2800:
        scale = 2800 / float(max_side)
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)

    # Small printed fractions benefit from a fairly aggressive upscale.
    if max(img.size) < 2200:
        scale = min(3.5, max(2.0, 2400 / float(max(img.size))))
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)

    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)
    variants: list[Image.Image] = [gray]

    if cv2 is not None and np is not None:
        try:
            arr = np.array(gray)
            otsu = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
            variants.append(Image.fromarray(otsu))

            adaptive = cv2.adaptiveThreshold(
                arr, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 9
            )
            variants.append(Image.fromarray(adaptive))

            # Slight dilation makes tiny numerator/denominator marks and fraction bars easier to see.
            inv = cv2.bitwise_not(otsu)
            dilated = cv2.dilate(inv, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)), iterations=1)
            variants.append(Image.fromarray(cv2.bitwise_not(dilated)))
        except Exception:
            pass

    return variants


def _ocr_score(text: str) -> float:
    """Pick the OCR pass most likely to preserve useful recipe structure and fractions."""
    value = text or ""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    score = len(value) * 0.02 + len(lines) * 2.0
    score += len(re.findall(r"\bingredients?\b|\binstructions?\b|\bdirections?\b|\bmethod\b", value, flags=re.I)) * 18
    score += len(re.findall(r"\b\d+\s+\d\s*/\s*\d\b|[¼½¾⅓⅔⅛⅜⅝⅞]", value)) * 12
    score += len(re.findall(r"\b\d+\s*/\s*\d\b", value)) * 4
    # Penalize lone percent signs because they often mean a lost fraction glyph.
    score -= len(re.findall(r"(?<!\d)%", value)) * 8
    return score


def _ocr_image(img: Image.Image) -> str:
    """OCR a recipe image with several passes; keep the pass that best preserves structure/fractions."""
    _require_tesseract()
    variants = _prepare_ocr_variants(img)
    configs = [
        "--oem 3 --psm 6 -c preserve_interword_spaces=1",
        "--oem 3 --psm 4 -c preserve_interword_spaces=1",
        "--oem 3 --psm 11",
        "--oem 3 --psm 6 -c preserve_interword_spaces=1",
    ]

    results: list[str] = []
    for variant in variants:
        for config in configs:
            text = _safe_ocr(variant, config=config)
            if text and text.strip():
                results.append(text)

    if not results:
        return ""
    return max(results, key=_ocr_score)


def extract_text_from_images(image_paths: Iterable[str]) -> str:
    chunks: list[str] = []
    for image_path in image_paths:
        path = Path(image_path).expanduser()
        if not path.exists():
            raise CaptureRecipeError(f"Capture image not found: {path}")
        try:
            with Image.open(path) as img:
                text = _ocr_image(img)
        except CaptureRecipeError:
            raise
        except Exception as exc:
            raise CaptureRecipeError(f"OCR failed for {path.name}: {exc}") from exc
        text = normalize_ocr_text(text)
        if text:
            chunks.append(text)
    combined = "\n\n".join(chunks).strip()
    if not combined:
        raise CaptureRecipeError("OCR did not find readable recipe text in the uploaded photos.")
    return combined


def normalize_ocr_text(text: str) -> str:
    text = (text or "").replace("\r", "\n")
    text = _repair_fraction_ocr(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _normalize_url_ocr_text(text: str) -> str:
    """Normalize OCR output before URL matching."""
    value = str(text or "")
    value = value.replace("‐", "-").replace("‑", "-").replace("‒", "-").replace("–", "-").replace("—", "-")
    value = value.replace("|", "/").replace("\\", "/")
    value = re.sub(r"(?<=[A-Za-z0-9])[’'`]+(?=[A-Za-z0-9])", "", value)
    # For URL-looking lines, remove OCR-inserted spaces inside the URL.
    value = re.sub(
        r"(?i)(https?\s*[:;]?\s*/?\s*/?|www\.)[^\n\r]+",
        lambda m: re.sub(r"\s+", "", m.group(0)),
        value,
    )
    value = re.sub(r"\b[nr]ttps\b", "https", value, flags=re.I)
    value = re.sub(r"\bh[il1]tps\b", "https", value, flags=re.I)
    value = re.sub(r"\bh[il1]tp\b", "http", value, flags=re.I)
    value = re.sub(r"h\s*t\s*t\s*p\s*s?", lambda m: re.sub(r"\s+", "", m.group(0)), value, flags=re.I)
    value = re.sub(r"https?\s*[;:]\s*/\s*/", lambda m: re.sub(r"\s+", "", m.group(0)).replace(";", ":"), value, flags=re.I)
    value = re.sub(r"www\s*\.\s*", "www.", value, flags=re.I)
    # OCR often reads the dot before a top-level domain as whitespace.
    value = re.sub(
        r"(?i)\b([a-z0-9-]{4,})\s+(com|org|net|co|io|edu|gov|us|uk|ca|au|de|fr|it|nl|se|no|fi|me|app|dev)\b",
        r"\1.\2",
        value,
    )
    value = re.sub(r"\s*([:/._~%?#[\]@!$&'()*+,;=-])\s*", r"\1", value)
    value = re.sub(r"(?<=https://[^\s]{3})\s+(?=[A-Za-z0-9])", "", value)
    value = re.sub(r"(?<=http://[^\s]{3})\s+(?=[A-Za-z0-9])", "", value)
    value = re.sub(r"(?<=www\.[^\s]{3})\s+(?=[A-Za-z0-9])", "", value)
    return value


def _clean_url_candidate(value: str) -> str:
    url = _normalize_url_ocr_text(str(value or ""))
    url = url.strip().rstrip(".,;:)>]}'\"")
    url = re.sub(r"\s+", "", url)
    url = url.replace("https;//", "https://").replace("http;//", "http://")
    url = url.replace("https:ll", "https://").replace("http:ll", "http://")
    url = re.sub(r"(?i)biggerbolderbakina", "biggerbolderbaking", url)
    url = re.sub(r"(?i)wprm_prin[uv]\b", "wprm_print", url)
    url = re.sub(r"(?i)wprm_prin[uv](?=[/-])", "wprm_print", url)
    # Common footer OCR errors: wprm_print is often read as worm/orint,
    # wprr/orint, or similar when the printed footer is small.
    url = re.sub(r"(?i)/(?:wprm|wprr|worm|worn|wor)[,._-]?(?:print|printh|orint|drint|0rint|orin|orinv|orinth)(?=/|how-to-|now-to-|[a-z])", "/wprm_print/", url)
    url = re.sub(r"(?i)(?:wprm|wprr|worm|worn|wor)[,._-]?(?:print|printh|orint|drint|0rint|orin|orinv|orinth)(?=/|how-to-|now-to-|[a-z])", "wprm_print/", url)
    # Common footer OCR error: the slash after wprm_print becomes f or is lost.
    url = re.sub(r"(?i)wprm_printf(?=[a-z0-9-])", "wprm_print/", url)
    url = re.sub(r"(?i)wprm_print(?=how-to-|now-to-|recipe-|print-|[a-z]+-[a-z]+)", "wprm_print/", url)
    url = re.sub(r"(?i)/now-to-", "/how-to-", url)
    url = url.replace("https:/l", "https://").replace("http:/l", "http://")
    if url.lower().startswith("https:/") and not url.lower().startswith("https://"):
        url = "https://" + url[7:].lstrip("/")
    if url.lower().startswith("http:/") and not url.lower().startswith("http://"):
        url = "http://" + url[6:].lstrip("/")
    # If OCR dropped the dot before a TLD inside a host like
    # www.biggerbolderbakingcom/wprm_print, put it back.
    match = re.match(r"(?i)^(https?://)?(www\.[a-z0-9-]+?)(com|org|net|co|io|edu|gov|us|uk|ca|au|de|fr|it|nl|se|no|fi|me|app|dev)(/.*)?$", url)
    if match and "." not in match.group(2)[4:]:
        url = f"{match.group(1) or ''}{match.group(2)}.{match.group(3)}{match.group(4) or ''}"

    if url.lower().startswith("www."):
        url = "https://" + url
    # Bare domain fallback, e.g. biggerbolderbaking.com/foo from OCR that missed https://
    if not url.lower().startswith(("http://", "https://")) and "." in url:
        url = "https://" + url.lstrip("/")
    return url


def _score_url_candidate(url: str) -> int:
    """Score URL candidates so full recipe URLs beat partial OCR fragments."""
    cleaned = str(url or "").strip()
    host_and_path = re.sub(r"^https?://", "", cleaned, flags=re.I)
    host, sep, path = host_and_path.partition("/")
    host_lower = host.lower()
    path_lower = path.lower()

    score = len(cleaned)
    if host_lower.startswith("www."):
        score += 12
    if path:
        score += min(40, len(path))
    if any(token in path_lower for token in ("recipe", "wprm", "print", "how-to", "make")):
        score += 35
    if any(token in host_lower for token in ("baking", "food", "recipe", "kitchen", "delish", "allrecipes")):
        score += 20
    # Penalize obvious fragments that look like a cut-off domain or no useful path.
    if not path and len(host_lower) < 18:
        score -= 35
    if host_lower.endswith(".co") and not path:
        score -= 35
    return score


def _find_urls_in_text(text: str) -> list[str]:
    normalized = _normalize_url_ocr_text(text)
    urls: list[str] = []

    for pattern in (URL_RE, BARE_DOMAIN_RE):
        for match in pattern.finditer(normalized):
            url = _clean_url_candidate(match.group(0))
            # Filter obvious OCR junk. A useful recipe URL should have a host with a dot.
            if url.lower().startswith(("http://", "https://")) and "." in url:
                host = re.sub(r"^https?://", "", url, flags=re.I).split("/", 1)[0].lower()
                host = host.split("@")[-1].split(":", 1)[0]
                if not re.fullmatch(r"(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
                    continue
                if len(url) >= 10 and url not in urls:
                    urls.append(url)
    return urls

def _dedupe_urls(urls: Iterable[str]) -> list[str]:
    """Preserve order while removing duplicate URL candidates."""
    return list(dict.fromkeys([u for u in urls if u]))


def _urls_from_decoded_qr_values(decoded_values: Iterable[str]) -> list[str]:
    urls: list[str] = []
    for value in decoded_values:
        value = str(value or "").strip()
        if value:
            urls.extend(_find_urls_in_text(value))
    return _dedupe_urls(urls)


def _order_qr_points(points: Any) -> Any:
    """Return QR corner points ordered TL, TR, BR, BL for perspective warp."""
    pts = np.asarray(points, dtype="float32").reshape(4, 2)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(4)
    ordered = np.zeros((4, 2), dtype="float32")
    ordered[0] = pts[np.argmin(s)]      # top-left
    ordered[2] = pts[np.argmax(s)]      # bottom-right
    ordered[1] = pts[np.argmin(diff)]   # top-right
    ordered[3] = pts[np.argmax(diff)]   # bottom-left
    return ordered


def _warp_qr_from_points(image: Any, points: Any, output_size: int = 900) -> Any | None:
    """Crop/deskew a detected QR quadrilateral into a square image for another decode pass."""
    if cv2 is None or np is None or points is None:
        return None
    try:
        ordered = _order_qr_points(points)
        destination = np.array(
            [
                [0, 0],
                [output_size - 1, 0],
                [output_size - 1, output_size - 1],
                [0, output_size - 1],
            ],
            dtype="float32",
        )
        transform = cv2.getPerspectiveTransform(ordered, destination)
        warped = cv2.warpPerspective(image, transform, (output_size, output_size))
        return cv2.copyMakeBorder(warped, 64, 64, 64, 64, cv2.BORDER_CONSTANT, value=[255, 255, 255])
    except Exception as exc:
        LOGGER.info("[URL SCAN] QR warp failed: %s", exc)
        return None


def _iter_detected_qr_points(points: Any) -> list[Any]:
    """Normalize OpenCV QR point return shapes into a list of 4-corner arrays."""
    if points is None:
        return []
    try:
        arr = np.asarray(points, dtype="float32")
        if arr.size < 8:
            return []
        arr = arr.reshape(-1, 4, 2)
        return [arr[i] for i in range(arr.shape[0])]
    except Exception:
        return []


def _opencv_qr_variants(arr: Any) -> list[tuple[str, Any]]:
    """Small OpenCV fallback set. pyzbar handles the fast first pass."""
    variants: list[tuple[str, Any]] = [("original", arr)]
    try:
        # A quiet-zone border is cheap and helps some screenshots/printed codes.
        variants.append(("white_border", cv2.copyMakeBorder(arr, 64, 64, 64, 64, cv2.BORDER_CONSTANT, value=[255, 255, 255])))
        # Keep OpenCV bounded. In real testing, expensive OpenCV passes were slower
        # than pyzbar and usually only helped by locating points for a later warp.
        if max(arr.shape[:2]) < 1200:
            variants.append(("upscaled_2x", cv2.resize(arr, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)))
    except Exception as exc:
        LOGGER.info("[URL SCAN] OpenCV QR variant prep failed: %s", exc)
    return variants


def _decode_qr_array_with_opencv(candidate: Any, label: str, detector: Any) -> tuple[list[str], list[str], bool, list[Any]]:
    """Return (urls, decoded_values, saw_points, point_sets) for one OpenCV candidate image."""
    urls: list[str] = []
    decoded_values: list[str] = []
    point_sets: list[Any] = []
    saw_points = False

    try:
        ok, decoded_info, points, _ = detector.detectAndDecodeMulti(candidate)
        point_sets.extend(_iter_detected_qr_points(points))
        saw_points = saw_points or bool(point_sets)
        LOGGER.info(
            "[URL SCAN] OpenCV QR multi variant=%s ok=%s decoded_count=%s points=%s",
            label,
            ok,
            len(decoded_info or []),
            points is not None,
        )
        if ok:
            for value in decoded_info or []:
                value = str(value or "").strip()
                if value:
                    decoded_values.append(value)
    except Exception as exc:
        LOGGER.info("[URL SCAN] OpenCV QR multi variant=%s failed: %s", label, exc)

    if not decoded_values:
        try:
            value, points, _ = detector.detectAndDecode(candidate)
            extra_points = _iter_detected_qr_points(points)
            point_sets.extend(extra_points)
            saw_points = saw_points or bool(extra_points)
            LOGGER.info(
                "[URL SCAN] OpenCV QR single variant=%s decoded=%s points=%s",
                label,
                bool(value),
                points is not None,
            )
            value = str(value or "").strip()
            if value:
                decoded_values.append(value)
        except Exception as exc:
            LOGGER.info("[URL SCAN] OpenCV QR single variant=%s failed: %s", label, exc)

    urls = _urls_from_decoded_qr_values(decoded_values)
    return urls, decoded_values, saw_points, point_sets


def _decode_qr_urls_with_opencv(path: Path) -> tuple[list[str], bool, list[Any]]:
    """Try bounded OpenCV QR decoding.

    Returns URL candidates, whether strong QR evidence was seen, and warped crops.
    Points found only after aggressive upscaling are treated as weak evidence;
    real footer URL photos can produce false QR-like points there, which used to
    trigger slow rescue work before OCR.
    """
    if cv2 is None or np is None:
        LOGGER.info("[URL SCAN] OpenCV QR detection skipped because cv2/numpy is not available")
        return [], False, []

    arr = cv2.imread(str(path))
    if arr is None:
        LOGGER.info("[URL SCAN] OpenCV QR detection could not read image: %s", path)
        return [], False, []

    detector = cv2.QRCodeDetector()
    urls: list[str] = []
    decoded_values: list[str] = []
    saw_any_points = False
    saw_strong_points = False
    warped_candidates: list[Any] = []

    for label, candidate in _opencv_qr_variants(arr):
        candidate_urls, candidate_values, candidate_saw_points, point_sets = _decode_qr_array_with_opencv(candidate, label, detector)
        urls.extend(candidate_urls)
        decoded_values.extend(candidate_values)
        saw_any_points = saw_any_points or candidate_saw_points

        # Treat points on original / white-border as strong QR evidence. Points
        # that appear only after upscaling are useful for logging, but too noisy
        # to justify the full QR rescue path on footer URL photos.
        label_is_strong = label in {"original", "white_border"}
        if candidate_saw_points and label_is_strong:
            saw_strong_points = True

        if point_sets:
            LOGGER.info(
                "[URL SCAN] OpenCV QR variant=%s point_sets=%s strong=%s",
                label,
                len(point_sets),
                label_is_strong,
            )

        # Only build warped QR crops from strong QR evidence. This avoids wasting
        # time warping text/table artifacts that OpenCV sees only after upscaling.
        if label_is_strong:
            for point_set in point_sets[:2]:
                warped = _warp_qr_from_points(candidate, point_set)
                if warped is not None:
                    warped_candidates.append(warped)

        if urls:
            break

    if saw_any_points and not saw_strong_points:
        LOGGER.info("[URL SCAN] OpenCV saw only weak/upscaled QR-like points; treating as non-QR for OCR fallback")

    # If OpenCV can locate a QR strongly but not decode it, deskew/crop the
    # quadrilateral and try OpenCV again.
    if not urls and warped_candidates:
        for index, warped in enumerate(warped_candidates[:2], start=1):
            label = f"warped_{index}"
            candidate_urls, candidate_values, _, _ = _decode_qr_array_with_opencv(warped, label, detector)
            urls.extend(candidate_urls)
            decoded_values.extend(candidate_values)
            if urls:
                break

    unique_urls = _dedupe_urls(urls)
    LOGGER.info(
        "[URL SCAN] OpenCV QR decoded_values=%s url_candidates=%s strong_points=%s",
        decoded_values,
        unique_urls,
        saw_strong_points,
    )
    return unique_urls, saw_strong_points, warped_candidates


def _cv2_array_to_pil_rgb(arr: Any) -> Image.Image:
    if cv2 is not None and np is not None:
        return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))
    return Image.fromarray(arr)


def _pyzbar_image_variants(
    path: Path,
    warped_candidates: list[Any] | None = None,
    fast_only: bool = False,
) -> list[tuple[str, Image.Image]]:
    variants: list[tuple[str, Image.Image]] = []
    with Image.open(path) as img:
        base = ImageOps.exif_transpose(img).convert("RGB")

    variants.append(("original", base))

    # pyzbar has proven best for branded QR codes in this workflow. Keep the
    # first pass intentionally small so normal QR scans return quickly.
    gray = ImageOps.autocontrast(ImageOps.grayscale(base))
    variants.append(("gray_autocontrast", gray))

    if cv2 is not None and np is not None:
        try:
            arr = np.array(gray)
            # threshold_105 decoded the real Delish test QR. Try it early.
            variants.append(("threshold_105", Image.fromarray(cv2.threshold(arr, 105, 255, cv2.THRESH_BINARY)[1])))
            if not fast_only:
                for threshold in (125, 145, 165, 185):
                    variants.append((f"threshold_{threshold}", Image.fromarray(cv2.threshold(arr, threshold, 255, cv2.THRESH_BINARY)[1])))
                otsu = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
                variants.append(("threshold_otsu", Image.fromarray(otsu)))
        except Exception as exc:
            LOGGER.info("[URL SCAN] pyzbar variant prep failed: %s", exc)

    if not fast_only:
        variants.append(("white_border", ImageOps.expand(base, border=64, fill="white")))
        if max(base.size) < 1400:
            variants.append(("upscaled_2x", base.resize((base.width * 2, base.height * 2), Image.LANCZOS)))

        for index, warped in enumerate(warped_candidates or [], start=1):
            try:
                warped_img = _cv2_array_to_pil_rgb(warped).convert("RGB")
                variants.append((f"opencv_warped_{index}", warped_img))
                warped_gray = ImageOps.autocontrast(ImageOps.grayscale(warped_img))
                variants.append((f"opencv_warped_{index}_gray", warped_gray))
            except Exception as exc:
                LOGGER.info("[URL SCAN] Could not prepare pyzbar warped variant %s: %s", index, exc)

    return variants


def _decode_qr_urls_with_pyzbar(
    path: Path,
    warped_candidates: list[Any] | None = None,
    fast_only: bool = False,
) -> list[str]:
    """Try pyzbar/libzbar QR decoding if available. Returns URL candidates only."""
    if pyzbar_decode is None:
        LOGGER.info("[URL SCAN] pyzbar QR detection skipped because pyzbar/libzbar is not available")
        return []

    urls: list[str] = []
    decoded_values: list[str] = []
    mode = "fast" if fast_only else "full"
    try:
        variants = _pyzbar_image_variants(path, warped_candidates=warped_candidates, fast_only=fast_only)
        for label, image in variants:
            decoded = pyzbar_decode(image)
            LOGGER.info("[URL SCAN] pyzbar QR %s variant=%s decoded_count=%s", mode, label, len(decoded or []))
            for item in decoded or []:
                raw = getattr(item, "data", b"") or b""
                try:
                    value = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    value = str(raw or "").strip()
                if value:
                    decoded_values.append(value)
                    urls.extend(_find_urls_in_text(value))
            if urls:
                break
        unique_urls = _dedupe_urls(urls)
        LOGGER.info("[URL SCAN] pyzbar QR %s decoded_values=%s url_candidates=%s", mode, decoded_values, unique_urls)
        return unique_urls
    except Exception as exc:
        LOGGER.info("[URL SCAN] pyzbar QR %s detection failed: %s", mode, exc)
        return []


def _decode_qr_urls(path: Path) -> tuple[list[str], bool]:
    LOGGER.info("[URL SCAN] QR detection starting for %s", path)

    # Fast path first: real-world tests showed pyzbar decoded branded QR codes
    # much faster than the full OpenCV/warp search. This does not affect printed
    # footer URL photos because failures still fall through to OCR below.
    urls = _decode_qr_urls_with_pyzbar(path, fast_only=True)
    if urls:
        LOGGER.info("[URL SCAN] QR URL found by pyzbar fast path: %s", urls[0])
        return urls, False

    # OpenCV fallback can still locate QR points and create warped crops for a
    # deeper pyzbar retry, but the variant set is intentionally bounded.
    urls, saw_qr_points, warped_candidates = _decode_qr_urls_with_opencv(path)
    if urls:
        LOGGER.info("[URL SCAN] QR URL found by OpenCV: %s", urls[0])
        return urls, saw_qr_points

    # If OpenCV did not even see QR finder points, this is almost certainly a
    # printed footer URL photo rather than a QR photo. Skip the heavier pyzbar
    # rescue variants and go straight to the OCR footer path. This preserves the
    # working URL-image behavior while shaving several seconds off non-QR scans.
    if not saw_qr_points:
        LOGGER.info("[URL SCAN] No QR points detected after fast checks; skipping full QR rescue and falling back to OCR")
        return [], False

    urls = _decode_qr_urls_with_pyzbar(path, warped_candidates=warped_candidates, fast_only=False)
    if urls:
        LOGGER.info("[URL SCAN] QR URL found by pyzbar full path: %s", urls[0])
        return urls, saw_qr_points

    LOGGER.info("[URL SCAN] QR-like points were detected but no decoder returned a URL; using quick OCR fallback only")
    return [], saw_qr_points


def _find_likely_url_line_crops(img: Image.Image) -> list[Image.Image]:
    """Find tight horizontal crops that look like a printed footer URL line."""
    if cv2 is None or np is None:
        return []

    rgb = ImageOps.exif_transpose(img).convert("RGB")
    arr = np.array(rgb)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)

    h, w = gray.shape[:2]
    # Dark-ish pixels catch black/gray printed text.  Ignore very dark full-width
    # regions because those are usually table/counter/desk edges, not the URL.
    mask = gray < 135
    row_counts = mask.sum(axis=1)

    min_count = max(10, int(w * 0.006))
    max_count = int(w * 0.45)
    candidate_rows = np.where((row_counts >= min_count) & (row_counts <= max_count))[0]

    groups: list[tuple[int, int, int]] = []
    if candidate_rows.size:
        s = int(candidate_rows[0])
        prev = s
        for y_raw in candidate_rows[1:]:
            y = int(y_raw)
            if y <= prev + 3:
                prev = y
            else:
                if prev - s >= 8:
                    score = int(row_counts[s:prev + 1].sum())
                    groups.append((s, prev, score))
                s = prev = y
        if prev - s >= 8:
            score = int(row_counts[s:prev + 1].sum())
            groups.append((s, prev, score))

    # Footer URLs usually live in the lower half, but allow a little flexibility
    # for photos with lots of bottom margin.
    groups = [
        g for g in groups
        if g[1] > int(h * 0.45) and (g[1] - g[0] + 1) <= int(h * 0.28)
    ]
    groups.sort(key=lambda g: (g[2], g[1]), reverse=True)

    crops: list[Image.Image] = []
    seen: set[tuple[int, int, int, int]] = set()
    for y1, y2, _score in groups[:6]:
        pad_y = max(24, int((y2 - y1 + 1) * 1.2))
        yy1 = max(0, y1 - pad_y)
        yy2 = min(h, y2 + pad_y)

        band = mask[yy1:yy2, :]
        col_counts = band.sum(axis=0)
        candidate_cols = np.where(col_counts >= 2)[0]
        if candidate_cols.size:
            x1 = max(0, int(candidate_cols.min()) - 90)
            x2 = min(w, int(candidate_cols.max()) + 90)
        else:
            x1, x2 = 0, w

        # If the right side includes the table/counter edge, keep a full-width
        # crop as well as the tighter line crop.
        for box in (
            (x1, yy1, x2, yy2),
            (0, yy1, w, yy2),
        ):
            if box in seen:
                continue
            seen.add(box)
            crop = rgb.crop(box)
            if crop.width >= 250 and crop.height >= 30:
                LOGGER.info("[URL SCAN] URL line crop candidate box=%s size=%s", box, crop.size)
                crops.append(crop)

    return crops


def _url_ocr_candidate_images(img: Image.Image) -> list[Image.Image]:
    """Return targeted crops likely to contain a printed footer URL."""
    img = ImageOps.exif_transpose(img).convert("RGB")

    max_side = 2600
    if max(img.size) > max_side:
        scale = max_side / float(max(img.size))
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)

    w, h = img.size

    # Try detected URL-line crops first. They are usually much smaller than the
    # fixed footer bands and were the fastest successful path in real footer
    # photo testing.
    candidates: list[Image.Image] = _find_likely_url_line_crops(img)

    boxes = [
        # tight footer URL bands; these are tuned for printed recipe footer URLs.
        (0, int(h * 0.50), w, int(h * 0.70)),
        (0, int(h * 0.52), w, int(h * 0.70)),
        (0, int(h * 0.55), w, int(h * 0.70)),
        (0, int(h * 0.55), w, int(h * 0.73)),
        (0, int(h * 0.58), w, int(h * 0.76)),
        (0, int(h * 0.58), w, int(h * 0.78)),
        (0, int(h * 0.62), w, int(h * 0.82)),
        (0, int(h * 0.66), w, int(h * 0.86)),
        (0, int(h * 0.70), w, int(h * 0.90)),
        (0, int(h * 0.74), w, int(h * 0.94)),
        (0, int(h * 0.78), w, h),
        # left-heavy variants, common for print footer URLs
        (0, int(h * 0.58), int(w * 0.82), int(h * 0.84)),
        (0, int(h * 0.64), int(w * 0.88), int(h * 0.90)),
        # whole image as last resort
        (0, 0, w, h),
    ]

    seen: set[tuple[int, int, int, int]] = set()
    for box in boxes:
        x1, y1, x2, y2 = box
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(w, x2), min(h, y2)
        if x2 <= x1 or y2 <= y1:
            continue
        norm = (x1, y1, x2, y2)
        if norm in seen:
            continue
        seen.add(norm)
        candidates.append(img.crop(norm))

    return candidates


def _ocr_image_once(image: Image.Image, config: str, timeout_seconds: int = 5) -> str:
    try:
        return pytesseract.image_to_string(image, config=config, timeout=timeout_seconds) or ""
    except RuntimeError as exc:
        LOGGER.warning("[URL SCAN] OCR timed out or failed for config %s: %s", config, exc)
        return ""
    except Exception as exc:
        LOGGER.warning("[URL SCAN] OCR failed for config %s: %s", config, exc)
        return ""



def _url_ocr_variants(candidate: Image.Image, crop_index: int) -> list[Image.Image]:
    candidate = candidate.convert("RGB")

    # Slight rotations are a big win for phone photos of printed footer URLs.
    # The common case is a small clockwise tilt, so try negative corrections first.
    rgb_candidates: list[Image.Image] = []
    if crop_index <= 2:
        for angle in (-2.0, -3.0):
            try:
                rgb_candidates.append(candidate.rotate(angle, resample=Image.BICUBIC, expand=True, fillcolor=(255, 255, 255)))
            except Exception:
                pass
    rgb_candidates.append(candidate)

    variants: list[Image.Image] = []
    for rgb in rgb_candidates:
        target_width = 2600 if crop_index <= 10 else 2000
        if rgb.width < target_width:
            scale = min(5.0, max(2.0, target_width / float(max(rgb.width, 1))))
            rgb = rgb.resize(
                (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))),
                Image.LANCZOS,
            )

        gray = ImageOps.grayscale(rgb)
        gray = ImageOps.autocontrast(gray)
        variants.append(gray)

        if cv2 is not None and np is not None:
            try:
                arr = np.array(gray)
                blurred = cv2.GaussianBlur(arr, (3, 3), 0)
                variants.append(Image.fromarray(blurred))

                # Keep URL OCR bounded on the Pi. A couple fixed thresholds catch
                # the useful footer cases without exploding into dozens of OCR calls.
                for threshold in (165, 185):
                    variants.append(Image.fromarray(cv2.threshold(blurred, threshold, 255, cv2.THRESH_BINARY)[1]))

                if crop_index <= 2:
                    otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
                    variants.append(Image.fromarray(otsu))
            except Exception as exc:
                LOGGER.debug("[URL SCAN] Could not build cv2 OCR variants: %s", exc)

    return variants


def _ocr_url_from_image(img: Image.Image, quick_mode: bool = False) -> tuple[list[str], str]:
    _require_tesseract()

    # PSM 7 is the key mode for printed footer URLs: one horizontal text line.
    line_config = "--oem 3 --psm 7"
    sparse_config = "--oem 3 --psm 11"
    block_config = "--oem 3 --psm 6"

    all_text: list[str] = []
    urls: list[str] = []
    deadline = time.monotonic() + (8.0 if quick_mode else 18.0)
    max_ocr_calls = 10 if quick_mode else 18
    ocr_calls = 0

    candidates = _url_ocr_candidate_images(img)
    if quick_mode:
        # QR-like images with detected corner points but no decoded value can spend
        # minutes in OCR on a Pi. Keep this fallback intentionally small while
        # preserving the full footer-URL OCR path for non-QR photos.
        candidates = candidates[:4]
    LOGGER.info("[URL SCAN] OCR candidate crop count: %s quick_mode=%s", len(candidates), quick_mode)

    for idx, candidate in enumerate(candidates, start=1):
        variants = _url_ocr_variants(candidate, idx)
        if quick_mode:
            variants = variants[:3]

        for variant_index, variant in enumerate(variants, start=1):
            if time.monotonic() >= deadline or ocr_calls >= max_ocr_calls:
                LOGGER.info("[URL SCAN] OCR bounded stop reached; returning best candidate so far if available")
                if urls:
                    urls = sorted(urls, key=_score_url_candidate, reverse=True)
                    return urls, "\n".join(all_text).strip()
                break

            # Try line mode first. Sparse mode is slower, so only use it on the
            # first two likely footer crops or quick QR fallback crops.
            configs = [line_config]
            if quick_mode or idx <= 2:
                configs.append(sparse_config)
            if not quick_mode and idx == len(candidates):
                configs.append(block_config)

            for config in configs:
                if time.monotonic() >= deadline or ocr_calls >= max_ocr_calls:
                    break
                ocr_calls += 1
                text = _ocr_image_once(variant, config=config, timeout_seconds=2 if quick_mode else 3).strip()
                if not text:
                    continue

                log_text = text[:260].replace("\n", " | ")
                LOGGER.info("[URL SCAN] OCR crop %s variant %s text: %s", idx, variant_index, log_text)
                all_text.append(text)

                for url in _find_urls_in_text(text):
                    if url not in urls:
                        LOGGER.info("[URL SCAN] URL candidate: %s score=%s", url, _score_url_candidate(url))
                        urls.append(url)
                if urls:
                    best_so_far = max(urls, key=_score_url_candidate)
                    best_score = _score_url_candidate(best_so_far)
                    # Once we have a plausible recipe/print URL, stop. Continuing
                    # OCR just burns CPU and can replace a useful candidate with noise.
                    if best_score >= 135 and ("print" in best_so_far.lower() or "wprm" in best_so_far.lower() or len(best_so_far) >= 55):
                        return [best_so_far] + [u for u in urls if u != best_so_far], "\n".join(all_text).strip()

    if urls:
        urls = sorted(urls, key=_score_url_candidate, reverse=True)
    elif all_text:
        LOGGER.info("[URL SCAN] No URL matched. OCR combined text: %s", " | ".join(all_text)[:900])
    return urls, "\n".join(all_text).strip()

def extract_url_from_images(image_paths: Iterable[str]) -> dict[str, Any]:
    """Find a recipe URL from QR code first, then a bounded OCR pass over likely footer regions."""
    all_urls: list[str] = []
    ocr_texts: list[str] = []
    for image_path in image_paths:
        path = Path(str(image_path)).expanduser()
        if not path.exists():
            continue
        qr_urls, saw_qr_points = _decode_qr_urls(path)
        for url in qr_urls:
            if url not in all_urls:
                all_urls.append(url)
        if all_urls:
            break
        try:
            with Image.open(path) as img:
                urls, text = _ocr_url_from_image(img, quick_mode=saw_qr_points)
        except CaptureRecipeError:
            raise
        except Exception:
            urls, text = [], ""
        if text:
            ocr_texts.append(text)
        for url in urls:
            if url not in all_urls:
                all_urls.append(url)
        if all_urls:
            break
    if not all_urls:
        raise CaptureRecipeError("Could not find a URL or QR code in the uploaded image. Try taking a closer photo of the footer or QR code.")
    return {"url": all_urls[0], "urls": all_urls, "ocr_text": "\n\n".join(ocr_texts).strip()}


def _alpha_words(value: str) -> list[str]:
    return re.findall(r"[a-z]+", str(value or "").lower())


def _is_section_heading(line: str, names: tuple[str, ...]) -> bool:
    """Return True for clean headings and common noisy OCR variants."""
    words = _alpha_words(line)
    if not words:
        return False
    wanted = {name.lower() for name in names}
    if any(word in wanted for word in words):
        # Avoid matching long prose accidentally; headings are usually short.
        non_noise = [w for w in words if w not in {"i", "j", "q", "e", "a", "z", "ie", "lei"}]
        return len(non_noise) <= 3
    return False


def _section_index(lines: list[str], names: tuple[str, ...]) -> int | None:
    for idx, line in enumerate(lines):
        if _is_section_heading(line, names):
            return idx
    return None


def _strip_ocr_prefix(value: str) -> str:
    value = str(value or "").strip()
    # Remove common leading OCR garbage from bullets/box borders.
    value = re.sub(r"^[\s:;|!¡iIjJqQzZ¢©•*+_\\/\-–—]+", "", value).strip()
    # OCR often adds one or two letters before the actual item: "ie 1 egg", "e 3 pears".
    value = re.sub(r"^(?:i|j|q|e|z|ie|lei)\s+(?=(?:\d|\d/|[¼½¾⅓⅔⅛⅜⅝⅞%]))", "", value, flags=re.I).strip()
    value = value.strip(" .:_-–—\\/")
    return re.sub(r"\s+", " ", value).strip()


def _strip_trailing_ocr_noise(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"\s*[\\|]+\s*$", "", value).strip()
    value = re.sub(r"\s+[ijqzea]$", "", value, flags=re.I).strip()
    value = value.strip(" '")
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_numbered_step(value: str) -> bool:
    return bool(re.match(r"^(?:\d+\s*[\).:-]|step\s+\d+\s*[:.)-]?)\s*", value, flags=re.I))


RECIPE_UNIT_PATTERN = (
    r"cup|cups|c\.?|teaspoon|teaspoons|tsp\.?|tablespoon|tablespoons|tbsp\.?|"
    r"ounce|ounces|oz\.?|pound|pounds|lb\.?|lbs\.?|gram|grams|g|kg|kilogram|kilograms|"
    r"milliliter|milliliters|ml|mL|liter|liters|l|L|"
    r"stick|sticks|pinch|pinches|dash|dashes|can|cans|package|packages|pkg\.?|"
    r"clove|cloves|slice|slices|sprig|sprigs"
)
RECIPE_UNIT_RE = re.compile(rf"\b(?:{RECIPE_UNIT_PATTERN})\b", re.I)
NUMERIC_CONTEXT_RE = re.compile(r"(?:\d|[¼½¾⅓⅔⅛⅜⅝⅞]|\b(?:one|two|three|four|half|quarter)\b)", re.I)
PERCENT_WITH_RECIPE_UNIT_RE = re.compile(
    rf"(?P<prefix>^|[^\w/])(?P<whole>\d+)?\s*%\s+(?P<unit>{RECIPE_UNIT_PATTERN})\b",
    re.I,
)
PERCENT_BLOCKLIST_RE = re.compile(
    r"\b(?:percent|percentage|nutrition|daily value|dv|reduced|discount|sale|off|proof|abv|alcohol|"
    r"fat|sodium|cholesterol|carbohydrate|protein)\b",
    re.I,
)
UNICODE_FRACTIONS = {
    "¼": "1/4", "½": "1/2", "¾": "3/4",
    "⅓": "1/3", "⅔": "2/3",
    "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}


def _log_percent_cleanup(original: str, updated: str, *, reason: str) -> None:
    if "%" not in original:
        return
    if original != updated:
        LOGGER.info('[OCR CLEANUP] Converted suspicious %% in recipe text: %r -> %r (%s)', original, updated, reason)
    else:
        LOGGER.info('[OCR CLEANUP] Left suspicious %% unchanged in recipe text: %r (%s)', original, reason)


def _normalize_unicode_fractions(value: str) -> str:
    for src, dst in UNICODE_FRACTIONS.items():
        # Keep mixed numbers readable: "1½" -> "1 1/2".
        value = re.sub(rf"(?<=\d){re.escape(src)}", f" {dst}", value)
        value = value.replace(src, dst)
    return value


def _repair_percent_fraction_ocr_line(line: str) -> str:
    """Repair OCR percent signs only when the surrounding text looks like a recipe amount.

    Tesseract commonly reads the ¾ glyph/fraction as %. We still keep this conservative:
    values such as "50% reduced fat" or URL-like text are left alone, while values such as
    "% cup" and "1 % cups" are converted because a recipe unit immediately follows.
    """
    original = str(line or "")
    if "%" not in original:
        return original

    if re.search(r"https?://|www\.|\b\w+\.com\b", original, flags=re.I):
        _log_percent_cleanup(original, original, reason="URL-like text")
        return original

    if PERCENT_BLOCKLIST_RE.search(original):
        _log_percent_cleanup(original, original, reason="percent appears to be literal/nutrition text")
        return original

    def replace_percent(match: re.Match[str]) -> str:
        whole = match.group("whole") or ""
        unit = match.group("unit") or ""
        prefix = match.group("prefix") or ""
        before = original[:match.start()]

        # Do not rewrite normal numeric percentages like "50%". A whole number followed by
        # a space before % is treated as a mixed recipe quantity: "1 % cups" -> "1 3/4 cups".
        matched_text = match.group(0)
        if whole and re.search(rf"\b{re.escape(whole)}%", matched_text):
            return matched_text

        # Require recipe-like context: a unit right after %, or a number/fraction before it.
        has_recipe_context = bool(unit) or bool(NUMERIC_CONTEXT_RE.search(before[-16:]))
        if not has_recipe_context:
            return matched_text

        replacement_amount = f"{whole} 3/4" if whole else "3/4"
        return f"{prefix}{replacement_amount} {unit}"

    updated = PERCENT_WITH_RECIPE_UNIT_RE.sub(replace_percent, original)

    if "%" in updated:
        # Handle spaced forms that the first regex intentionally avoided, but only when
        # the percent mark is isolated and a unit is still close by: "1  %  cup".
        updated = re.sub(
            rf"(?<!\d)(?P<prefix>^|\s)%\s+(?P<unit>{RECIPE_UNIT_PATTERN})\b",
            lambda m: f"{m.group('prefix')}3/4 {m.group('unit')}",
            updated,
            flags=re.I,
        )

    if "%" in updated:
        _log_percent_cleanup(original, updated, reason="no safe recipe-unit conversion found")
    else:
        _log_percent_cleanup(original, updated, reason="near recipe quantity/unit; interpreted as 3/4")
    return updated


def _repair_fraction_ocr_line(line: str) -> str:
    value = str(line or "")
    value = _normalize_unicode_fractions(value)

    # Normalize common OCR characters used where the numerator 1 was intended.
    value = re.sub(r"\b[Il|]\s*/\s*([2348])\b", r"1/\1", value)
    value = re.sub(r"\b(\d+)\s+[Il|]\s*/\s*([2348])\b", r"\1 1/\2", value)

    # Normalize spaced simple fractions and mixed numbers without touching decimals.
    value = re.sub(r"\b([1-7])\s*/\s*([2-8])\b", r"\1/\2", value)
    value = re.sub(r"\b(\d+)\s+([1-7])\s*/\s*([2-8])\b", r"\1 \2/\3", value)

    value = _repair_percent_fraction_ocr_line(value)

    # Clean OCR punctuation around fractions: "1.1/2" -> "1 1/2" when followed by a unit.
    value = re.sub(rf"\b(\d+)\.([1-7]/[2-8])\s+(?=({RECIPE_UNIT_PATTERN})\b)", r"\1 \2 ", value, flags=re.I)

    # Keep valid decimals intact, but fix obvious OCR comma decimals in quantities: "1,5 cups".
    value = re.sub(rf"\b(\d+),(\d+)\s+(?=({RECIPE_UNIT_PATTERN})\b)", r"\1.\2 ", value, flags=re.I)

    value = re.sub(r"[ \t]+", " ", value).strip()
    return value


def _repair_fraction_ocr(value: str) -> str:
    # Preserve line breaks for recipe parsing; only normalize each OCR line independently.
    lines = str(value or "").splitlines()
    return "\n".join(_repair_fraction_ocr_line(line) for line in lines)

def clean_recipe_line(value: str) -> str:
    value = _strip_ocr_prefix(value)
    value = _strip_trailing_ocr_noise(value)
    value = _repair_fraction_ocr(value)
    return value


def clean_direction_line(value: str) -> str:
    value = clean_recipe_line(value)
    value = re.sub(r"^(?:\d+\s*[\).:-]|step\s+\d+\s*[:.)-]?)\s*", "", value, flags=re.I)
    return value.strip()


def _clean_ingredient_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_headings = {
        "print recipe", "recipe", "ingredients", "ingredient", "instructions",
        "instruction", "directions", "direction", "method", "preparation", "steps",
    }
    for raw in lines:
        if _is_section_heading(raw, ("ingredient", "ingredients", "instruction", "instructions", "direction", "directions", "method", "preparation", "steps")):
            continue
        item = clean_recipe_line(raw)
        if not item:
            continue
        if item.lower() in skip_headings:
            continue
        cleaned.append(item)
    return cleaned


def _clean_direction_lines(lines: list[str]) -> list[str]:
    directions: list[str] = []
    for raw in lines:
        if _is_section_heading(raw, ("instruction", "instructions", "direction", "directions", "method", "preparation", "steps")):
            continue
        stripped = clean_recipe_line(raw)
        if not stripped:
            continue
        is_new_step = _looks_like_numbered_step(stripped)
        item = clean_direction_line(stripped)
        if not item:
            continue
        if is_new_step or not directions:
            directions.append(item)
        else:
            # Wrapped OCR line; continue the prior numbered step.
            directions[-1] = f"{directions[-1]} {item}".strip()
    return directions


def _model_needs_reparse(model: dict[str, Any]) -> bool:
    ingredients = model.get("ingredients") or []
    directions = model.get("directions") or []
    if not directions:
        joined_ingredients = "\n".join(str(x) for x in ingredients)
        if re.search(r"\binstructions?\b|\bdirections?\b|\bmethod\b", joined_ingredients, flags=re.I):
            return True
    return False


def parse_ocr_text_to_recipe_model(text: str, *, title: str = "Recipe", description: str = "") -> dict[str, Any]:
    raw_lines = [line.strip() for line in normalize_ocr_text(text).splitlines() if line.strip()]
    if not raw_lines:
        raise CaptureRecipeError("No OCR text was available to parse.")

    ing_idx = _section_index(raw_lines, ("ingredient", "ingredients"))
    dir_idx = _section_index(raw_lines, ("instruction", "instructions", "direction", "directions", "method", "preparation", "steps"))

    ingredient_lines: list[str] = []
    direction_lines: list[str] = []

    if ing_idx is not None and dir_idx is not None:
        if ing_idx < dir_idx:
            ingredient_lines = raw_lines[ing_idx + 1:dir_idx]
            direction_lines = raw_lines[dir_idx + 1:]
        else:
            direction_lines = raw_lines[dir_idx + 1:ing_idx]
            ingredient_lines = raw_lines[ing_idx + 1:]
    elif ing_idx is not None:
        after_ingredients = raw_lines[ing_idx + 1:]
        split_at = None
        for idx, line in enumerate(after_ingredients):
            if _looks_like_numbered_step(clean_recipe_line(line)):
                split_at = idx
                break
        if split_at is not None:
            ingredient_lines = after_ingredients[:split_at]
            direction_lines = after_ingredients[split_at:]
        else:
            ingredient_lines = after_ingredients
    elif dir_idx is not None:
        direction_lines = raw_lines[dir_idx + 1:]
    else:
        # Fallback: split near numbered steps if OCR did not retain headings.
        split_at = None
        for idx, line in enumerate(raw_lines):
            if _looks_like_numbered_step(clean_recipe_line(line)):
                split_at = idx
                break
        if split_at is not None and split_at > 0:
            ingredient_lines = raw_lines[:split_at]
            direction_lines = raw_lines[split_at:]
        else:
            ingredient_lines = raw_lines

    ingredients = _clean_ingredient_lines(ingredient_lines)
    directions = _clean_direction_lines(direction_lines)

    return {
        "title": title or "Recipe",
        "description": description or "Captured from recipe photos",
        "meta": "Source: Capture",
        "ingredients": ingredients,
        "directions": directions,
        "image_url": "",
    }

def build_capture_recipe_model(recipe: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    capture_dir = Path(str(recipe.get("capture_dir") or "")).expanduser()
    if not str(capture_dir) or not capture_dir.exists():
        capture_dir = capture_dir_for_recipe(Path(runtime["recipe_cache_dir"]), str(recipe.get("id", "recipe")))
    if not capture_dir.exists():
        raise CaptureRecipeError(f"Capture folder not found: {capture_dir}")

    model_path = capture_dir / "recipe_model.json"
    cached_model: dict[str, Any] | None = None
    if model_path.exists():
        try:
            cached_model = json.loads(model_path.read_text(encoding="utf-8"))
            if not _model_needs_reparse(cached_model):
                return cached_model
        except Exception:
            cached_model = None

    image_paths = recipe.get("source_image_paths") or []
    if isinstance(image_paths, str):
        image_paths = [p for p in image_paths.split("|") if p.strip()]
    if not image_paths:
        image_paths = [str(p) for p in sorted(capture_dir.glob("image_*.jpg"))]

    ocr_path = capture_dir / "ocr_text.txt"
    if ocr_path.exists():
        text = ocr_path.read_text(encoding="utf-8")
    else:
        text = extract_text_from_images(image_paths)
        ocr_path.write_text(text + "\n", encoding="utf-8")

    model = parse_ocr_text_to_recipe_model(
        text,
        title=str(recipe.get("name") or "Recipe"),
        description=str(recipe.get("description") or "Captured from recipe photos"),
    )
    model_path.write_text(json.dumps(model, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return model


def remove_capture_assets(recipe: dict[str, Any]) -> None:
    capture_dir = str(recipe.get("capture_dir") or "").strip()
    if not capture_dir:
        return
    path = Path(os.path.expanduser(capture_dir))
    if path.exists() and path.is_dir() and path.name:
        shutil.rmtree(path, ignore_errors=True)
