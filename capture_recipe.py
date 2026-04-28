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


def _section_index(lines: list[str], names: tuple[str, ...]) -> int | None:
    pattern = re.compile(r"^(?:" + "|".join(re.escape(n) for n in names) + r")\b[:\s-]*$", re.I)
    for idx, line in enumerate(lines):
        if pattern.match(line.strip()):
            return idx
    return None


def parse_ocr_text_to_recipe_model(text: str, *, title: str = "Recipe", description: str = "") -> dict[str, Any]:
    lines = [line.strip(" •-*\t") for line in normalize_ocr_text(text).splitlines() if line.strip()]
    if not lines:
        raise CaptureRecipeError("No OCR text was available to parse.")

    ing_idx = _section_index(lines, ("ingredient", "ingredients"))
    dir_idx = _section_index(lines, ("instruction", "instructions", "direction", "directions", "method", "preparation", "steps"))

    ingredients: list[str] = []
    directions: list[str] = []

    if ing_idx is not None and dir_idx is not None:
        start, end = sorted([ing_idx, dir_idx])
        if ing_idx < dir_idx:
            ingredients = lines[ing_idx + 1:dir_idx]
            directions = lines[dir_idx + 1:]
        else:
            directions = lines[dir_idx + 1:ing_idx]
            ingredients = lines[ing_idx + 1:]
    elif ing_idx is not None:
        ingredients = lines[ing_idx + 1:]
    elif dir_idx is not None:
        directions = lines[dir_idx + 1:]
    else:
        # Simple fallback: split near numbered steps if OCR did not retain headings.
        split_at = None
        for idx, line in enumerate(lines):
            if re.match(r"^(?:\d+[\).:-]|step\s+\d+)\s+", line, flags=re.I):
                split_at = idx
                break
        if split_at is not None and split_at > 0:
            ingredients = lines[:split_at]
            directions = lines[split_at:]
        else:
            ingredients = lines

    ingredients = [clean_recipe_line(item) for item in ingredients if clean_recipe_line(item)]
    directions = [clean_direction_line(item) for item in directions if clean_direction_line(item)]

    return {
        "title": title or "Recipe",
        "description": description or "Captured from recipe photos",
        "meta": "Source: Capture",
        "ingredients": ingredients,
        "directions": directions,
        "image_url": "",
    }


def clean_recipe_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip(" •-*\t"))


def clean_direction_line(value: str) -> str:
    value = clean_recipe_line(value)
    value = re.sub(r"^(?:\d+[\).:-]|step\s+\d+[:.)-]?)\s*", "", value, flags=re.I)
    return value.strip()


def build_capture_recipe_model(recipe: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    capture_dir = Path(str(recipe.get("capture_dir") or "")).expanduser()
    if not str(capture_dir) or not capture_dir.exists():
        capture_dir = capture_dir_for_recipe(Path(runtime["recipe_cache_dir"]), str(recipe.get("id", "recipe")))
    if not capture_dir.exists():
        raise CaptureRecipeError(f"Capture folder not found: {capture_dir}")

    model_path = capture_dir / "recipe_model.json"
    if model_path.exists():
        try:
            return json.loads(model_path.read_text(encoding="utf-8"))
        except Exception:
            pass

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
