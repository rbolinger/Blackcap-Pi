#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shutil
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
    import pytesseract
except Exception:  # Optional dependency; handled at runtime with a clear error.
    pytesseract = None

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


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
                img.thumbnail((2400, 2400), Image.LANCZOS)
                normalized_path = target_dir / f"image_{index:03d}.jpg"
                img.convert("RGB").save(normalized_path, format="JPEG", quality=92)
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


def _crop_best_photo_region(img: Image.Image) -> Image.Image | None:
    """Find the most photo-like color region in a captured recipe image.

    The extractor first isolates the bright printed page, then looks for colorful,
    rectangular regions inside that page. This avoids using the full phone photo as
    the thumbnail and avoids most black text because text is low-saturation.
    """
    if cv2 is None or np is None:
        return None

    rgb = img.convert("RGB")
    arr_full = np.array(rgb)
    full_height, full_width = arr_full.shape[:2]
    if full_width < 80 or full_height < 80:
        return None

    page_x, page_y, page_w, page_h = _page_bounds_for_capture(arr_full)
    page = arr_full[page_y:page_y + page_h, page_x:page_x + page_w]
    if page.size == 0:
        page = arr_full
        page_x = page_y = 0
        page_h, page_w = page.shape[:2]

    hsv = cv2.cvtColor(page, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    height, width = page.shape[:2]
    page_area = max(width * height, 1)

    def build_photo_mask(saturation_floor: int) -> Any:
        mask = ((sat > saturation_floor) & (val > 35) & (val < 252)).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (27, 27)))
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)), iterations=1)
        return mask

    candidates: list[tuple[float, tuple[int, int, int, int]]] = []
    for saturation_floor in (34, 24, 18):
        mask = build_photo_mask(saturation_floor)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            area = w * h
            if area < page_area * 0.004 or w < 55 or h < 55:
                continue
            if area > page_area * 0.28:
                continue
            aspect = w / float(max(h, 1))
            if aspect < 0.35 or aspect > 3.4:
                continue

            crop = page[y:y + h, x:x + w]
            crop_hsv = hsv[y:y + h, x:x + w]
            region_mask = mask[y:y + h, x:x + w]
            filled_ratio = float(np.count_nonzero(region_mask)) / float(max(area, 1))
            mean_sat = float(np.mean(crop_hsv[:, :, 1]))
            median_sat = float(np.median(crop_hsv[:, :, 1]))
            std_rgb = float(np.mean(np.std(crop, axis=(0, 1))))
            dark_ratio = float(np.count_nonzero(crop_hsv[:, :, 2] < 70)) / float(max(area, 1))

            if filled_ratio < 0.05:
                continue
            if mean_sat < 15 and std_rgb < 18:
                continue
            if dark_ratio > 0.45:
                continue

            center_x = x + w / 2.0
            center_y = y + h / 2.0
            right_bias = 1.35 if center_x > width * 0.50 else 1.0
            upper_bias = 1.18 if center_y < height * 0.55 else 1.0
            rectangular_bonus = 1.12 if 0.65 <= aspect <= 1.8 else 1.0
            score = (area / page_area) * (1.0 + mean_sat / 45.0) * (1.0 + median_sat / 60.0) * (1.0 + std_rgb / 55.0)
            score *= (1.0 + filled_ratio) * right_bias * upper_bias * rectangular_bonus
            score *= 1.0 + (saturation_floor / 100.0)
            candidates.append((score, (page_x + x, page_y + y, w, h)))
        if candidates:
            break

    if not candidates:
        return None

    _, (x, y, w, h) = max(candidates, key=lambda item: item[0])
    pad = max(8, int(min(w, h) * 0.035))
    x1 = max(0, x - pad)
    y1 = max(0, y - pad)
    x2 = min(full_width, x + w + pad)
    y2 = min(full_height, y + h + pad)
    return rgb.crop((x1, y1, x2, y2))

def save_capture_recipe_image(manifest: dict[str, Any], recipe_cache_dir: Path, recipe_id: str) -> Path | None:
    """Save the best color dish thumbnail as recipe_cache/<recipe_id>.png."""
    image_paths = manifest.get("source_image_paths") or []
    if not image_paths:
        return None

    target = Path(recipe_cache_dir).expanduser().resolve() / f"{slugify(recipe_id)}.png"
    target.parent.mkdir(parents=True, exist_ok=True)

    first_valid: Image.Image | None = None
    best_crop: Image.Image | None = None
    best_area = 0

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
                crop = _crop_best_photo_region(img)
                if crop is None:
                    continue
                area = crop.width * crop.height
                if area > best_area:
                    best_crop = crop
                    best_area = area
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

def extract_text_from_images(image_paths: Iterable[str]) -> str:
    if pytesseract is None:
        raise CaptureRecipeError(
            "Capture OCR requires pytesseract. Install it with: "
            "/home/pi/inky_env/bin/pip install pytesseract and sudo apt install tesseract-ocr"
        )

    chunks: list[str] = []
    for image_path in image_paths:
        path = Path(image_path).expanduser()
        if not path.exists():
            raise CaptureRecipeError(f"Capture image not found: {path}")
        try:
            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img).convert("L")
                text = pytesseract.image_to_string(img, config="--psm 6")
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
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


def clean_recipe_line(value: str) -> str:
    value = _strip_ocr_prefix(value)
    value = _strip_trailing_ocr_noise(value)
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
