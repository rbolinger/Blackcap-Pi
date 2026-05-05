#!/usr/bin/env python3
from __future__ import annotations

import argparse
import configparser
import html
import io
import json
import os
import re
import sys
import shutil
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

from capture_recipe import CaptureRecipeError, build_capture_recipe_model, remove_capture_assets

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("INKY_CONFIG_PATH", str(Path.home() / "inky_menu_config.ini")))
CACHE_CHECK_DAYS = 30

DEFAULT_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
]
DEFAULT_BOLD_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]

lock_file = None

class RecipeModeError(Exception):
    pass


def resolve_path(value: str) -> Path:
    path = Path(os.path.expanduser(str(value).strip()))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def require_path(config: configparser.ConfigParser, section: str, key: str, fallback: str) -> Path:
    return resolve_path(config.get(section, key, fallback=fallback))


def acquire_lock(lock_path: Path) -> None:
    """Create the configured display lock file before touching the e-ink display.

    Match inky_menu.py behavior: an existing lock file means the display is busy,
    so exit cleanly instead of waiting. release_lock() removes the file in a
    finally block after the recipe render finishes or fails.
    """
    global lock_file
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise RecipeModeError(f"Display is busy; lock file already exists: {lock_path}")

    lock_file = os.fdopen(fd, "w")
    lock_file.write(str(os.getpid()))
    lock_file.flush()


def release_lock(lock_path: Path) -> None:
    global lock_file
    try:
        if lock_file:
            lock_file.close()
    finally:
        lock_file = None
        try:
            lock_path.unlink(missing_ok=True)
        except Exception as exc:
            print(f"Warning: could not remove display lock file {lock_path}: {exc}")


def find_font(configured: str, candidates: list[str]) -> str:
    if configured:
        configured_path = Path(os.path.expanduser(configured))
        if configured_path.exists():
            return str(configured_path)
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return ""


def load_font(path: str, size: int) -> ImageFont.ImageFont:
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def load_config(require_recipe_mode: bool = True) -> tuple[configparser.ConfigParser, dict[str, Any]]:
    if not CONFIG_PATH.exists():
        raise RecipeModeError(f"Config file not found: {CONFIG_PATH}")

    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)

    runtime: dict[str, Any] = {}
    runtime["display_mode"] = config.get("general", "display_mode", fallback="normal").strip().lower()
    if require_recipe_mode and runtime["display_mode"] != "recipe":
        raise RecipeModeError(f"Blackcap Pi is in {runtime['display_mode']} mode; recipe renderer skipped.")

    runtime["display_width"] = config.getint("display", "display_width", fallback=1600)
    runtime["display_height"] = config.getint("display", "display_height", fallback=1200)
    runtime["lock_path"] = require_path(config, "paths", "lockfile", "/tmp/inky_menu_display.lock")
    runtime["selected_recipe_id"] = config.get("recipe_mode", "selected_recipe_id", fallback="").strip()
    # Do not require selected_recipe_id while running --cache-only with an explicit
    # --recipe-id. Newly added recipes may not be selected yet. The renderer will
    # validate the active recipe ID inside render_selected_recipe().

    runtime["repo_path"] = require_path(config, "recipe_repository", "repo_path", "/home/pi/inky_recipe_repo.json")
    runtime["recipe_cache_dir"] = require_path(config, "recipe_repository", "cache_dir", "/home/pi/recipe_cache")
    runtime["recipe_preview_path"] = require_path(config, "paths", "recipe_preview", str(Path.home() / "recipe_preview.png"))
    runtime["current_preview_path"] = require_path(config, "paths", "current_preview", str(runtime["recipe_preview_path"]))
    runtime["current_recipe_image_path"] = require_path(config, "paths", "current_recipe_image", str(runtime["recipe_cache_dir"] / "current_recipe_image.png"))

    runtime["margin"] = config.getint("recipe_rendering", "margin", fallback=28)
    runtime["min_margin"] = config.getint("recipe_rendering", "min_margin", fallback=18)
    runtime["title_font_size"] = config.getint("recipe_rendering", "title_font_size", fallback=30)
    runtime["heading_font_size"] = config.getint("recipe_rendering", "heading_font_size", fallback=22)
    runtime["body_font_size"] = config.getint("recipe_rendering", "body_font_size", fallback=17)
    runtime["small_font_size"] = config.getint("recipe_rendering", "small_font_size", fallback=14)
    runtime["min_title_font_size"] = config.getint("recipe_rendering", "min_title_font_size", fallback=22)
    runtime["min_heading_font_size"] = config.getint("recipe_rendering", "min_heading_font_size", fallback=17)
    runtime["min_body_font_size"] = config.getint("recipe_rendering", "min_body_font_size", fallback=12)
    runtime["min_small_font_size"] = config.getint("recipe_rendering", "min_small_font_size", fallback=10)
    runtime["line_spacing"] = config.getint("recipe_rendering", "line_spacing", fallback=3)
    runtime["section_gap"] = config.getint("recipe_rendering", "section_gap", fallback=10)
    runtime["threshold"] = config.getint("recipe_rendering", "threshold", fallback=190)

    runtime["font_path"] = find_font(config.get("recipe_rendering", "font_path", fallback="").strip(), DEFAULT_FONT_CANDIDATES)
    runtime["bold_font_path"] = find_font(config.get("recipe_rendering", "bold_font_path", fallback="").strip(), DEFAULT_BOLD_FONT_CANDIDATES)

    for key in ["recipe_cache_dir", "repo_path", "recipe_preview_path", "current_preview_path", "current_recipe_image_path"]:
        (runtime[key] if key.endswith("dir") else runtime[key].parent).mkdir(parents=True, exist_ok=True)
    return config, runtime


def load_recipe_repo(repo_path: Path) -> dict[str, Any]:
    if not repo_path.exists():
        repo_path.write_text('{\n  "recipes": []\n}\n', encoding="utf-8")
        return {"recipes": []}
    try:
        return json.loads(repo_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecipeModeError(f"Could not parse recipe repository JSON at {repo_path}: {exc}")


def save_recipe_repo(repo_path: Path, repo: dict[str, Any]) -> None:
    tmp_path = repo_path.with_suffix(repo_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(repo, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, repo_path)


def get_recipe_by_id(repo: dict[str, Any], recipe_id: str) -> dict[str, Any]:
    for recipe in repo.get("recipes", []):
        if str(recipe.get("id", "")).strip() == recipe_id:
            return recipe
    raise RecipeModeError(f"Selected recipe ID '{recipe_id}' was not found in the recipe repository.")


def normalize_source(value: str) -> str:
    value = (value or "").strip().lower()
    aliases = {
        "website": "web", "web_recipe_url": "web", "url": "web",
        "shared_file_url": "file", "google drive": "google_drive",
        "gdrive": "google_drive", "drive": "google_drive",
        "photo": "capture", "photos": "capture", "scan": "capture", "scanned": "capture",
    }
    return aliases.get(value, value or "web")


def normalize_layout(value: str) -> str:
    value = (value or "").strip().lower()
    aliases = {
        "two_panel": "two_page", "two-page": "two_page", "2page": "two_page", "2_page": "two_page",
        "single": "single_page", "single-page": "single_page", "one_page": "single_page",
    }
    return aliases.get(value, "two_page")


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_utc_datetime(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def safe_recipe_id(recipe: dict[str, Any]) -> str:
    recipe_id = str(recipe.get("id", "recipe")).strip().lower() or "recipe"
    return re.sub(r"[^a-z0-9_]+", "_", recipe_id).strip("_") or "recipe"


def cache_pdf_path_for_recipe(recipe: dict[str, Any], runtime: dict[str, Any]) -> Path:
    return runtime["recipe_cache_dir"] / f"{safe_recipe_id(recipe)}.pdf"


def cache_image_path_for_recipe(recipe: dict[str, Any], runtime: dict[str, Any]) -> Path:
    return runtime["recipe_cache_dir"] / f"{safe_recipe_id(recipe)}.png"


def cache_rendered_png_path_for_recipe(recipe: dict[str, Any], runtime: dict[str, Any]) -> Path:
    return runtime["recipe_cache_dir"] / f"{safe_recipe_id(recipe)}_rendered.png"


def get_cached_recipe_image_path(recipe: dict[str, Any]) -> Optional[Path]:
    for key in ["recipe_image_path", "dish_image_path", "cached_image_path"]:
        cached = str(recipe.get(key, "") or "").strip()
        if cached:
            path = Path(os.path.expanduser(cached))
            if path.exists() and path.is_file():
                return path
    return None


def copy_current_recipe_image(recipe: dict[str, Any], runtime: dict[str, Any]) -> None:
    image_path = get_cached_recipe_image_path(recipe)
    if not image_path:
        return
    dest = runtime["current_recipe_image_path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copyfile(image_path, dest)
        print(f"Current recipe image copied to: {dest}")
    except Exception as exc:
        print(f"Warning: could not copy current recipe image: {exc}")


def get_cached_pdf_path(recipe: dict[str, Any]) -> Optional[Path]:
    for key in ["cached_pdf_path", "cached_file_path"]:
        cached = str(recipe.get(key, "") or "").strip()
        if cached:
            path = Path(os.path.expanduser(cached))
            if path.exists() and path.is_file() and path.suffix.lower() == ".pdf":
                return path
    return None


def cache_refresh_reason(recipe: dict[str, Any], source: str, url: str, layout: str, refresh_cache: bool) -> Optional[str]:
    if refresh_cache:
        return "--refresh-cache requested"
    if get_cached_pdf_path(recipe) is None:
        return "no cached PDF is available"
    if str(recipe.get("cached_source_url", "")).strip() != url:
        return "recipe URL changed"
    if normalize_source(str(recipe.get("cached_source", ""))) != source:
        return "recipe source changed"
    if normalize_layout(str(recipe.get("cached_layout", ""))) != layout:
        return "recipe layout changed"
    last_checked = parse_utc_datetime(recipe.get("cache_last_checked_at"))
    if last_checked is None:
        return "cache has never been checked"
    if utc_now() - last_checked >= timedelta(days=CACHE_CHECK_DAYS):
        return f"cache is at least {CACHE_CHECK_DAYS} days old"
    return None


def update_recipe_cache_metadata(
    repo_path: Path,
    repo: dict[str, Any],
    recipe_id: str,
    cache_path: Path,
    source_url: str,
    source: str,
    layout: str,
    rendered_png_path: Optional[Path] = None,
) -> None:
    now = utc_now_iso()
    for item in repo.get("recipes", []):
        if str(item.get("id", "")).strip() == recipe_id:
            item["cached_file_path"] = str(cache_path)
            item["cached_pdf_path"] = str(cache_path)
            item["cached_file_written_at"] = now
            item["cached_file_type"] = "pdf"
            item["cached_source_url"] = source_url
            item["cached_source"] = source
            item["cached_layout"] = layout
            item["cache_last_checked_at"] = now
            item["cache_build_status"] = "ready"
            item["cache_build_message"] = "Recipe cache created."
            item["cache_build_finished_at"] = now
            if not str(item.get("cache_build_started_at", "")).strip():
                item["cache_build_started_at"] = now
            if rendered_png_path is not None:
                item["cached_rendered_image_path"] = str(rendered_png_path)
                item["cached_png_path"] = str(rendered_png_path)
                item["cached_png_written_at"] = now
            save_recipe_repo(repo_path, repo)
            return


def update_recipe_image_metadata(repo_path: Path, repo: dict[str, Any], recipe_id: str, image_path: Path, image_source_url: str) -> None:
    now = utc_now_iso()
    for item in repo.get("recipes", []):
        if str(item.get("id", "")).strip() == recipe_id:
            item["recipe_image_path"] = str(image_path)
            item["dish_image_path"] = str(image_path)
            item["recipe_image_written_at"] = now
            item["recipe_image_source_url"] = image_source_url
            save_recipe_repo(repo_path, repo)
            return


def fetch_url(url: str) -> tuple[bytes, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 Chrome Safari InkyPiRecipeMode/1.0",
        "Accept": "text/html,application/pdf,image/*,*/*;q=0.8",
    }
    response = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return response.content, content_type


def fetch_web_page_with_playwright(url: str) -> str:
    if sync_playwright is None:
        raise RecipeModeError(
            "Direct web fetch was blocked and Playwright is not available. "
            "Install it in the Inky venv or use a shared PDF/image URL for this recipe."
        )

    print("Direct web fetch was blocked. Trying Playwright browser fetch...")
    browser = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(
                viewport={"width": 1280, "height": 1800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome Safari"
                ),
            )
            page.goto(url, wait_until="domcontentloaded", timeout=90000)

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass

            return page.content()
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass


def fetch_web_recipe_html(url: str) -> str:
    try:
        content, _ = fetch_url(url)
        return content.decode("utf-8", errors="ignore")
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {401, 403, 429}:
            return fetch_web_page_with_playwright(url)
        raise


def extract_google_drive_file_id(url: str) -> Optional[str]:
    for pattern in [r"/file/d/([^/]+)", r"[?&]id=([^&]+)", r"/document/d/([^/]+)", r"/spreadsheets/d/([^/]+)", r"/presentation/d/([^/]+)"]:
        match = re.search(pattern, url)
        if match:
            return urllib.parse.unquote(match.group(1))
    return None


def normalize_shared_file_url(url: str, source: str) -> str:
    source = normalize_source(source)
    if source == "dropbox" or "dropbox.com" in url:
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        query.pop("dl", None)
        query["raw"] = "1"
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
    if source == "google_drive" or "drive.google.com" in url:
        file_id = extract_google_drive_file_id(url)
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={urllib.parse.quote(file_id)}"
    return url


def absolute_url(url: str, base_url: str) -> str:
    if not url:
        return ""
    return urllib.parse.urljoin(base_url, html.unescape(str(url).strip()))


def extract_schema_image_url(value: Any, base_url: str) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return absolute_url(value, base_url)
    if isinstance(value, list):
        for item in value:
            found = extract_schema_image_url(item, base_url)
            if found:
                return found
    if isinstance(value, dict):
        for key in ["url", "contentUrl", "thumbnailUrl"]:
            found = extract_schema_image_url(value.get(key), base_url)
            if found:
                return found
    return ""


def extract_html_image_url(text: str, base_url: str) -> str:
    if BeautifulSoup is None:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    selectors = [
        ('meta[property="og:image"]', "content"),
        ('meta[name="og:image"]', "content"),
        ('meta[name="twitter:image"]', "content"),
        ('meta[property="twitter:image"]', "content"),
        ('link[rel="image_src"]', "href"),
    ]
    for selector, attr in selectors:
        node = soup.select_one(selector)
        if node and node.get(attr):
            return absolute_url(node.get(attr), base_url)
    img = soup.select_one("img[src]")
    if img and img.get("src"):
        return absolute_url(img.get("src"), base_url)
    return ""


def fetch_image_bytes_with_playwright(image_url: str, referer_url: str) -> Optional[bytes]:
    if sync_playwright is None:
        return None
    browser = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome Safari"
                ),
            )
            page = context.new_page()
            try:
                page.goto(referer_url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                response = context.request.get(
                    image_url,
                    headers={
                        "Referer": referer_url,
                        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    },
                    timeout=60000,
                )
                if response.ok:
                    return response.body()
            except Exception as exc:
                print(f"Playwright image fetch failed: {exc}")
            try:
                # Final fallback: screenshot the first large visible image on the rendered page.
                candidates = page.query_selector_all("img")
                best = None
                best_area = 0
                for el in candidates:
                    try:
                        box = el.bounding_box()
                        src = el.get_attribute("src") or ""
                        if not box:
                            continue
                        area = float(box.get("width", 0)) * float(box.get("height", 0))
                        if image_url in src or area > best_area:
                            best = el
                            best_area = area
                    except Exception:
                        continue
                if best is not None:
                    return best.screenshot(type="png")
            except Exception as exc:
                print(f"Playwright image screenshot fallback failed: {exc}")
    except Exception as exc:
        print(f"Playwright image fallback failed: {exc}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return None


def cache_recipe_image_from_url(image_url: str, recipe: dict[str, Any], runtime: dict[str, Any], repo: dict[str, Any], recipe_id: str, referer_url: str = "") -> Optional[Path]:
    image_url = str(image_url or "").strip()
    if not image_url:
        return None
    content: Optional[bytes] = None
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 Chrome Safari",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        if referer_url:
            headers["Referer"] = referer_url
        response = requests.get(image_url, headers=headers, timeout=60, allow_redirects=True)
        response.raise_for_status()
        content = response.content
    except Exception as exc:
        print(f"Direct image fetch failed for {image_url}: {exc}")
        if referer_url:
            print("Trying Playwright image fallback...")
            content = fetch_image_bytes_with_playwright(image_url, referer_url)

    if not content:
        print(f"Warning: could not cache recipe image from {image_url}")
        return None

    try:
        img = Image.open(io.BytesIO(content))
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((1200, 1200), Image.LANCZOS)
        image_path = cache_image_path_for_recipe(recipe, runtime)
        image_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(image_path, format="PNG")
        update_recipe_image_metadata(runtime["repo_path"], repo, recipe_id, image_path, image_url)
        print(f"Recipe image cached to: {image_path}")
        return image_path
    except Exception as exc:
        print(f"Warning: could not process/cache recipe image from {image_url}: {exc}")
        return None


def parse_web_recipe(url: str) -> dict[str, Any]:
    text = fetch_web_recipe_html(url)
    recipe = parse_json_ld_recipe(text, url)
    if recipe:
        if not recipe.get("image_url"):
            recipe["image_url"] = extract_html_image_url(text, url)
        return recipe
    if BeautifulSoup is None:
        raise RecipeModeError("Could not find recipe JSON-LD data. Install BeautifulSoup with: /home/pi/inky_env/bin/pip install beautifulsoup4")
    return parse_recipe_from_html_fallback(text)


def parse_json_ld_recipe(text: str, base_url: str = "") -> Optional[dict[str, Any]]:
    if BeautifulSoup is None:
        scripts = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, flags=re.IGNORECASE | re.DOTALL)
    else:
        soup = BeautifulSoup(text, "html.parser")
        scripts = [s.get_text() for s in soup.find_all("script", type="application/ld+json")]
    for raw in scripts:
        raw = html.unescape(raw).strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        found = find_recipe_object(data)
        if found:
            return recipe_object_to_model(found, base_url)
    return None


def find_recipe_object(data: Any) -> Optional[dict[str, Any]]:
    if isinstance(data, list):
        for item in data:
            found = find_recipe_object(item)
            if found:
                return found
    if isinstance(data, dict):
        obj_type = data.get("@type")
        is_recipe = any(str(t).lower() == "recipe" for t in obj_type) if isinstance(obj_type, list) else str(obj_type).lower() == "recipe"
        if is_recipe:
            return data
        for key in ["@graph", "mainEntity", "mainEntityOfPage"]:
            if data.get(key):
                found = find_recipe_object(data.get(key))
                if found:
                    return found
    return None


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(clean_text(v) for v in value if clean_text(v))
    if isinstance(value, dict):
        return clean_text(value.get("text") or value.get("name") or "")
    return html.unescape(re.sub(r"\s+", " ", str(value)).strip())


def recipe_object_to_model(obj: dict[str, Any], base_url: str = "") -> dict[str, Any]:
    name = clean_text(obj.get("name")) or "Recipe"
    description = clean_text(obj.get("description"))
    ingredients = obj.get("recipeIngredient") or obj.get("ingredients") or []
    if isinstance(ingredients, str):
        ingredients = [ingredients]
    ingredients = [clean_text(i) for i in ingredients if clean_text(i)]
    directions = parse_instructions(obj.get("recipeInstructions") or obj.get("instructions") or [])
    meta_parts = []
    for label, key in [("Yield", "recipeYield"), ("Prep", "prepTime"), ("Cook", "cookTime"), ("Total", "totalTime")]:
        value = clean_text(obj.get(key))
        if value:
            meta_parts.append(f"{label}: {format_iso_duration(value)}")
    image_url = extract_schema_image_url(obj.get("image"), base_url)
    return {"title": name, "description": description, "meta": "   •   ".join(meta_parts), "ingredients": ingredients, "directions": directions, "image_url": image_url}


def parse_instructions(value: Any) -> list[str]:
    directions: list[str] = []
    if isinstance(value, str):
        parts = re.split(r"\n+|\s+(?=Step\s+\d+[:.])", value)
        return [clean_text(p) for p in parts if clean_text(p)]
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                txt = clean_text(item)
                if txt:
                    directions.append(txt)
            elif isinstance(item, dict):
                if str(item.get("@type", "")).lower() == "howtosection":
                    directions.extend(parse_instructions(item.get("itemListElement", [])))
                else:
                    txt = clean_text(item.get("text") or item.get("name"))
                    if txt:
                        directions.append(txt)
    elif isinstance(value, dict):
        directions.extend(parse_instructions(value.get("itemListElement") or value.get("text") or ""))
    return directions


def format_iso_duration(value: str) -> str:
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?", value.strip(), flags=re.IGNORECASE)
    if not match:
        return value
    days, hours, minutes = match.groups()
    parts = []
    if days:
        parts.append(f"{int(days)}d")
    if hours:
        parts.append(f"{int(hours)}h")
    if minutes:
        parts.append(f"{int(minutes)}m")
    return " ".join(parts) if parts else value


def parse_recipe_from_html_fallback(text: str) -> dict[str, Any]:
    if BeautifulSoup is None:
        raise RecipeModeError("BeautifulSoup is required for HTML fallback parsing.")
    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "aside", "form"]):
        tag.decompose()
    title = clean_text(soup.find("h1").get_text(" ")) if soup.find("h1") else "Recipe"
    ingredients = []
    for node in soup.select("[class*=ingredient], [id*=ingredient], li[itemprop=recipeIngredient]"):
        txt = clean_text(node.get_text(" "))
        if txt and len(txt) < 220 and txt.lower() != "ingredients":
            ingredients.append(txt)
    directions = []
    for node in soup.select("[class*=instruction], [class*=direction], [id*=instruction], [id*=direction], li[itemprop=recipeInstructions]"):
        txt = clean_text(node.get_text(" "))
        if txt and len(txt) > 10 and txt.lower() not in {"directions", "instructions"}:
            directions.append(txt)
    ingredients = unique_preserve_order(ingredients)[:80]
    directions = unique_preserve_order(directions)[:40]
    if not ingredients and not directions:
        raise RecipeModeError("Could not extract recipe content from this webpage.")
    return {"title": title, "description": "", "meta": "", "ingredients": ingredients, "directions": directions, "image_url": extract_html_image_url(text, "")}


def unique_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def looks_like_pdf(content: bytes, content_type: str, url: str, file_type: str) -> bool:
    return content.startswith(b"%PDF") or content_type == "application/pdf" or file_type == "pdf" or url.lower().split("?")[0].endswith(".pdf")


def render_shared_file(recipe: dict[str, Any], runtime: dict[str, Any], repo: Optional[dict[str, Any]] = None, recipe_id: str = "") -> Image.Image:
    url = normalize_shared_file_url(str(recipe.get("url", "")), str(recipe.get("source", "")))
    file_type = str(recipe.get("file_type", "") or recipe.get("type", "")).strip().lower()
    content, content_type = fetch_url(url)
    if looks_like_pdf(content, content_type, url, file_type):
        return render_pdf_content(content, runtime)
    if content_type.startswith("image/") or file_type in {"image", "jpg", "jpeg", "png", "webp"}:
        img = Image.open(io.BytesIO(content))
        if repo is not None and recipe_id:
            try:
                image_path = cache_image_path_for_recipe(recipe, runtime)
                ImageOps.exif_transpose(img).convert("RGB").save(image_path, format="PNG")
                update_recipe_image_metadata(runtime["repo_path"], repo, recipe_id, image_path, url)
            except Exception as exc:
                print(f"Warning: could not cache shared recipe image: {exc}")
        return fit_image_to_display(img, runtime["display_width"], runtime["display_height"], runtime["threshold"])
    if content_type in {"text/plain", "text/markdown", "text/html"} or file_type in {"text", "markdown", "html"}:
        text = content.decode("utf-8", errors="ignore")
        model = {"title": str(recipe.get("name", "Recipe")), "description": str(recipe.get("description", "")), "meta": "", "ingredients": [], "directions": [text]}
        return render_recipe_text(model, runtime, normalize_layout(str(recipe.get("layout", "single_page"))))
    raise RecipeModeError(f"Unsupported shared file type. content-type={content_type or 'unknown'}, file_type={file_type or 'unset'}")


def render_pdf_file(pdf_path: Path, runtime: dict[str, Any]) -> Image.Image:
    try:
        import fitz
    except Exception:
        raise RecipeModeError("PDF rendering requires PyMuPDF. Install with: /home/pi/inky_env/bin/pip install pymupdf")
    doc = fitz.open(str(pdf_path))
    return render_pdf_doc(doc, runtime, fitz)


def render_pdf_content(content: bytes, runtime: dict[str, Any]) -> Image.Image:
    try:
        import fitz
    except Exception:
        raise RecipeModeError("PDF rendering requires PyMuPDF. Install with: /home/pi/inky_env/bin/pip install pymupdf")
    doc = fitz.open(stream=content, filetype="pdf")
    return render_pdf_doc(doc, runtime, fitz)


def render_pdf_doc(doc: Any, runtime: dict[str, Any], fitz_module: Any) -> Image.Image:
    if doc.page_count < 1:
        raise RecipeModeError("PDF has no pages.")
    pages = []
    for i in range(min(2, doc.page_count)):
        page = doc.load_page(i)
        pix = page.get_pixmap(matrix=fitz_module.Matrix(2, 2), alpha=False)
        pages.append(Image.open(io.BytesIO(pix.tobytes("png"))).convert("L"))
    if len(pages) == 1:
        return fit_image_to_display(pages[0], runtime["display_width"], runtime["display_height"], runtime["threshold"])
    canvas = Image.new("L", (runtime["display_width"], runtime["display_height"]), 255)
    panel_w = runtime["display_width"] // 2
    for idx, page_img in enumerate(pages):
        fitted = fit_image_to_display(page_img, panel_w, runtime["display_height"], runtime["threshold"]).convert("L")
        canvas.paste(fitted, (idx * panel_w, 0))
    return threshold_for_eink(canvas, runtime["threshold"])


def fit_image_to_display(img: Image.Image, width: int, height: int, threshold: int) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("L")
    img.thumbnail((width, height), Image.LANCZOS)
    canvas = Image.new("L", (width, height), 255)
    canvas.paste(img, ((width - img.width) // 2, (height - img.height) // 2))
    return threshold_for_eink(canvas, threshold)


def render_recipe_text(recipe_model: dict[str, Any], runtime: dict[str, Any], layout: str) -> Image.Image:
    best_img: Optional[Image.Image] = None
    best_overflow = 10**9
    best_sizes: Optional[dict[str, int]] = None
    max_steps = max(
        runtime["title_font_size"] - runtime["min_title_font_size"],
        runtime["heading_font_size"] - runtime["min_heading_font_size"],
        runtime["body_font_size"] - runtime["min_body_font_size"],
        runtime["small_font_size"] - runtime["min_small_font_size"],
        runtime["margin"] - runtime["min_margin"],
        0,
    )
    for step in range(max_steps + 1):
        sizes = {
            "title": max(runtime["min_title_font_size"], runtime["title_font_size"] - step),
            "heading": max(runtime["min_heading_font_size"], runtime["heading_font_size"] - step),
            "body": max(runtime["min_body_font_size"], runtime["body_font_size"] - step),
            "small": max(runtime["min_small_font_size"], runtime["small_font_size"] - step),
            "margin": max(runtime["min_margin"], runtime["margin"] - step),
            "line_spacing": max(1, runtime["line_spacing"] - step // 3),
            "section_gap": max(4, runtime["section_gap"] - step // 2),
        }
        img, overflow = render_recipe_text_once(recipe_model, runtime, layout, sizes, mark_overflow=False)
        if overflow < best_overflow:
            best_img = img
            best_overflow = overflow
            best_sizes = sizes
        if overflow <= 0:
            print(f"Recipe text fit using body font {sizes['body']} and margin {sizes['margin']}.")
            return img
    print("Recipe text still overflowed after shrinking; rendering with truncation marker.")
    if best_sizes is None:
        best_sizes = {"title": runtime["min_title_font_size"], "heading": runtime["min_heading_font_size"], "body": runtime["min_body_font_size"], "small": runtime["min_small_font_size"], "margin": runtime["min_margin"], "line_spacing": 1, "section_gap": 4}
    img, _ = render_recipe_text_once(recipe_model, runtime, layout, best_sizes, mark_overflow=True)
    return img if img is not None else best_img


def render_recipe_text_once(recipe_model: dict[str, Any], runtime: dict[str, Any], layout: str, sizes: dict[str, int], mark_overflow: bool) -> tuple[Image.Image, int]:
    width = runtime["display_width"]
    height = runtime["display_height"]
    margin = sizes["margin"]
    local_runtime = dict(runtime)
    local_runtime["margin"] = margin
    local_runtime["line_spacing"] = sizes["line_spacing"]
    local_runtime["section_gap"] = sizes["section_gap"]
    img = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(img)
    fonts = {
        "title": load_font(runtime["bold_font_path"], sizes["title"]),
        "heading": load_font(runtime["bold_font_path"], sizes["heading"]),
        "body": load_font(runtime["font_path"], sizes["body"]),
        "small": load_font(runtime["font_path"], sizes["small"]),
    }
    title = recipe_model.get("title", "Recipe")
    description = recipe_model.get("description", "")
    meta = recipe_model.get("meta", "")
    ingredients = recipe_model.get("ingredients", [])
    directions = recipe_model.get("directions", [])
    y = margin
    y, overflow = draw_wrapped_text(draw, title, margin, y, width - margin * 2, fonts["title"], local_runtime, max_lines=2)
    if meta:
        y += 4
        y, extra = draw_wrapped_text(draw, meta, margin, y, width - margin * 2, fonts["small"], local_runtime, max_lines=2)
        overflow += extra
    if description:
        y += 4
        y, extra = draw_wrapped_text(draw, description, margin, y, width - margin * 2, fonts["small"], local_runtime, max_lines=2)
        overflow += extra
    y += local_runtime["section_gap"]
    if layout == "single_page":
        y, extra = draw_section(draw, "Ingredients", ingredients, margin, y, width - margin * 2, fonts, local_runtime, bullet=True, mark_overflow=mark_overflow)
        overflow += extra
        y += local_runtime["section_gap"]
        _, extra = draw_section(draw, "Directions", directions, margin, y, width - margin * 2, fonts, local_runtime, numbered=True, mark_overflow=mark_overflow)
        overflow += extra
    else:
        panel_gap = 24
        panel_w = (width - margin * 2 - panel_gap) // 2
        left_x = margin
        right_x = margin + panel_w + panel_gap
        divider_x = right_x - panel_gap // 2
        draw.line((divider_x, y, divider_x, height - margin), fill=0, width=1)
        _, left_overflow = draw_section(draw, "Ingredients", ingredients, left_x, y, panel_w, fonts, local_runtime, bullet=True, mark_overflow=mark_overflow)
        _, right_overflow = draw_section(draw, "Directions", directions, right_x, y, panel_w, fonts, local_runtime, numbered=True, mark_overflow=mark_overflow)
        overflow += left_overflow + right_overflow
    return threshold_for_eink(img, runtime["threshold"]), overflow


def draw_section(draw: ImageDraw.ImageDraw, heading: str, items: list[str], x: int, y: int, width: int, fonts: dict[str, ImageFont.ImageFont], runtime: dict[str, Any], bullet: bool = False, numbered: bool = False, mark_overflow: bool = False) -> tuple[int, int]:
    overflow = 0
    y, extra = draw_wrapped_text(draw, heading, x, y, width, fonts["heading"], runtime, max_lines=1)
    overflow += extra
    y += 5
    if not items:
        items = ["No items found."]
    bottom_limit = runtime["display_height"] - runtime["margin"]
    for idx, item in enumerate(items, start=1):
        prefix = f"{idx}. " if numbered else "• " if bullet else ""
        y_before = y
        y, extra = draw_wrapped_text(draw, prefix + clean_text(item), x, y, width, fonts["body"], runtime, hanging_indent=28 if (bullet or numbered) else 0)
        y += 4
        overflow += extra
        if y > bottom_limit:
            overflow += y - bottom_limit
            if mark_overflow:
                draw.rectangle((x, max(y_before - 2, bottom_limit - 28), x + width, bottom_limit), fill=255)
                draw.text((x, bottom_limit - 22), "…", font=fonts["body"], fill=0)
            break
    return y, overflow


def draw_wrapped_text(draw: ImageDraw.ImageDraw, text: str, x: int, y: int, width: int, font: ImageFont.ImageFont, runtime: dict[str, Any], max_lines: Optional[int] = None, hanging_indent: int = 0) -> tuple[int, int]:
    if not text:
        return y, 0
    lines = wrap_text(draw, text, width, font, hanging_indent=hanging_indent)
    overflow = 0
    if max_lines is not None and len(lines) > max_lines:
        overflow += len(lines) - max_lines
        lines = lines[:max_lines]
        if lines:
            lines[-1] = lines[-1].rstrip() + "…"
    line_height = text_bbox_height(draw, "Ag", font) + runtime["line_spacing"]
    first = True
    for line in lines:
        line_x = x if first else x + hanging_indent
        draw.text((line_x, y), line, font=font, fill=0)
        y += line_height
        first = False
    bottom_limit = runtime["display_height"] - runtime["margin"]
    if y > bottom_limit:
        overflow += y - bottom_limit
    return y, overflow


def wrap_text(draw: ImageDraw.ImageDraw, text: str, width: int, font: ImageFont.ImageFont, hanging_indent: int = 0) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines = []
    current = ""
    for word in words:
        test = word if not current else current + " " + word
        effective_width = width if not lines else max(10, width - hanging_indent)
        if text_bbox_width(draw, test, font) <= effective_width:
            current = test
        else:
            if current:
                lines.append(current)
            if text_bbox_width(draw, word, font) > effective_width:
                chunks = split_long_word(draw, word, effective_width, font)
                lines.extend(chunks[:-1])
                current = chunks[-1] if chunks else ""
            else:
                current = word
    if current:
        lines.append(current)
    return lines


def split_long_word(draw: ImageDraw.ImageDraw, word: str, width: int, font: ImageFont.ImageFont) -> list[str]:
    chunks = []
    current = ""
    for ch in word:
        test = current + ch
        if text_bbox_width(draw, test, font) <= width:
            current = test
        else:
            if current:
                chunks.append(current)
            current = ch
    if current:
        chunks.append(current)
    return chunks


def text_bbox_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def text_bbox_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]


def threshold_for_eink(img: Image.Image, threshold: int) -> Image.Image:
    img = img.convert("L")
    return img.point(lambda p: 0 if p < threshold else 255).convert("1")


def save_image_as_pdf(img: Image.Image, pdf_path: Path) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = pdf_path.with_suffix(pdf_path.suffix + ".tmp")
    img.convert("RGB").save(tmp_path, "PDF", resolution=100.0)
    os.replace(tmp_path, pdf_path)


def save_image_as_png(img: Image.Image, png_path: Path) -> None:
    png_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = png_path.with_suffix(png_path.suffix + ".tmp")
    img.convert("RGB").save(tmp_path, "PNG")
    os.replace(tmp_path, png_path)


def clear_capture_working_files(repo_path: Path, repo: dict[str, Any], recipe_id: str) -> None:
    for item in repo.get("recipes", []):
        if str(item.get("id", "")).strip() == recipe_id:
            item["capture_dir"] = ""
            item["source_image_paths"] = []
            item["ocr_text_path"] = ""
            item["recipe_model_path"] = ""
            save_recipe_repo(repo_path, repo)
            return


def render_fresh_recipe(recipe: dict[str, Any], runtime: dict[str, Any], source: str, url: str, layout: str, repo: dict[str, Any], recipe_id: str) -> Image.Image:
    if source == "web":
        recipe_model = parse_web_recipe(url)
        name = str(recipe.get("name", "")).strip()
        if name:
            recipe_model["title"] = name
        if recipe.get("description"):
            recipe_model["description"] = str(recipe.get("description"))
        if recipe_model.get("image_url"):
            cache_recipe_image_from_url(str(recipe_model.get("image_url")), recipe, runtime, repo, recipe_id, url)
        return render_recipe_text(recipe_model, runtime, layout)
    if source == "capture":
        try:
            recipe_model = build_capture_recipe_model(recipe, runtime)
        except CaptureRecipeError as exc:
            raise RecipeModeError(str(exc)) from exc
        return render_recipe_text(recipe_model, runtime, layout)
    if source in {"dropbox", "google_drive", "file", "image", "pdf"}:
        return render_shared_file(recipe, runtime, repo=repo, recipe_id=recipe_id)
    if url.startswith("http"):
        recipe_model = parse_web_recipe(url)
        name = str(recipe.get("name", "")).strip()
        if name:
            recipe_model["title"] = name
        if recipe.get("description"):
            recipe_model["description"] = str(recipe.get("description"))
        if recipe_model.get("image_url"):
            cache_recipe_image_from_url(str(recipe_model.get("image_url")), recipe, runtime, repo, recipe_id, url)
        return render_recipe_text(recipe_model, runtime, layout)
    raise RecipeModeError(f"Unsupported recipe source: {source}")


def render_selected_recipe(runtime: dict[str, Any], refresh_cache: bool = False, recipe_id: Optional[str] = None) -> Image.Image:
    repo = load_recipe_repo(runtime["repo_path"])
    active_recipe_id = (recipe_id or runtime["selected_recipe_id"]).strip()
    if not active_recipe_id:
        raise RecipeModeError("No recipe ID was provided or selected.")
    recipe = get_recipe_by_id(repo, active_recipe_id)
    name = str(recipe.get("name", "Recipe")).strip() or "Recipe"
    source = normalize_source(str(recipe.get("source", "web")))
    url = str(recipe.get("url", "")).strip()
    layout = normalize_layout(str(recipe.get("layout", "two_page")))
    if source != "capture" and not url:
        raise RecipeModeError(f"Recipe '{name}' does not have a URL.")
    print("Recipe mode active.")
    print(f"Selected recipe: {name}")
    print(f"Source: {source}")
    print(f"Layout: {layout}")
    if url:
        print(f"URL: {url}")
    reason = cache_refresh_reason(recipe, source, url, layout, refresh_cache)
    cached_pdf = get_cached_pdf_path(recipe)
    if reason is None and cached_pdf is not None:
        print(f"Using cached recipe PDF: {cached_pdf}")
        copy_current_recipe_image(recipe, runtime)
        return render_pdf_file(cached_pdf, runtime)
    print(f"Refreshing recipe cache because {reason}.")
    rendered = render_fresh_recipe(recipe, runtime, source, url, layout, repo, active_recipe_id)
    cache_path = cache_pdf_path_for_recipe(recipe, runtime)
    rendered_png_path = cache_rendered_png_path_for_recipe(recipe, runtime)
    save_image_as_pdf(rendered, cache_path)
    save_image_as_png(rendered, rendered_png_path)
    print(f"Cached recipe PDF written to: {cache_path}")
    print(f"Cached rendered recipe PNG written to: {rendered_png_path}")
    update_recipe_cache_metadata(runtime["repo_path"], repo, active_recipe_id, cache_path, url, source, layout, rendered_png_path)
    if source == "capture":
        try:
            remove_capture_assets(recipe)
            repo_after_cleanup = load_recipe_repo(runtime["repo_path"])
            clear_capture_working_files(runtime["repo_path"], repo_after_cleanup, active_recipe_id)
            print("Capture upload working folder removed after successful cache build.")
        except Exception as exc:
            print(f"Warning: could not remove capture upload working folder: {exc}")
    try:
        repo_after = load_recipe_repo(runtime["repo_path"])
        copy_current_recipe_image(get_recipe_by_id(repo_after, active_recipe_id), runtime)
    except Exception as exc:
        print(f"Warning: could not update current recipe image after cache refresh: {exc}")
    return rendered


def update_display(img: Image.Image, dry_run: bool = False) -> None:
    if dry_run:
        print("Dry run enabled; not updating e-ink display.")
        return
    print("Initializing e-ink display...")
    try:
        from waveshare_epd import epd13in3k
    except Exception as exc:
        raise RecipeModeError(f"waveshare_epd.epd13in3k could not be imported. Preview was saved, but display was not updated: {exc}")
    epd = epd13in3k.EPD()
    try:
        epd.init()
        epd.display(epd.getbuffer(img))
    finally:
        try:
            epd.sleep()
        except Exception:
            pass
    print("Recipe display update complete.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render selected Blackcap Pi recipe mode item.")
    parser.add_argument("--dry-run", action="store_true", help="Render preview but do not update the e-ink display.")
    parser.add_argument("--refresh-cache", action="store_true", help="Force recipe cache rebuild even if cached PDF is current.")
    parser.add_argument("--cache-only", action="store_true", help="Build/update the cached recipe PDF only; do not save previews or update the display.")
    parser.add_argument("--recipe-id", default="", help="Recipe ID to render/cache instead of [recipe_mode] selected_recipe_id.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _, runtime = load_config(require_recipe_mode=not args.cache_only)
    recipe_id = args.recipe_id.strip() or None

    if args.cache_only:
        active_recipe_id = recipe_id or runtime["selected_recipe_id"]
        if active_recipe_id:
            set_recipe_cache_build_status(runtime["repo_path"], active_recipe_id, "building", "Recipe cache build running.", preserve_started_at=True)
        try:
            render_selected_recipe(runtime, refresh_cache=True, recipe_id=recipe_id)
        except Exception as exc:
            if active_recipe_id:
                set_recipe_cache_build_status(runtime["repo_path"], active_recipe_id, "error", str(exc), preserve_started_at=True)
            raise
        # The call above writes the cached PDF and JSON metadata. Do not touch the display
        # or the shared preview images when this is launched from the admin add-recipe flow.
        if active_recipe_id:
            set_recipe_cache_build_status(runtime["repo_path"], active_recipe_id, "ready", "Recipe cache build complete.", preserve_started_at=True)
        print("Recipe cache build complete.")
        return

    acquire_lock(runtime["lock_path"])
    try:
        img = render_selected_recipe(runtime, refresh_cache=args.refresh_cache, recipe_id=recipe_id)
        img.save(runtime["recipe_preview_path"])
        img.save(runtime["current_preview_path"])
        print(f"Recipe preview saved to: {runtime['recipe_preview_path']}")
        print(f"Current display preview saved to: {runtime['current_preview_path']}")
        update_display(img, dry_run=args.dry_run)
    finally:
        release_lock(runtime["lock_path"])


if __name__ == "__main__":
    try:
        main()
    except RecipeModeError as exc:
        print(f"Recipe mode error: {exc}")
        sys.exit(2)
    except requests.HTTPError as exc:
        print(f"Recipe mode HTTP error: {exc}")
        sys.exit(3)
    except Exception as exc:
        print(f"Unexpected recipe mode error: {exc}")
        sys.exit(1)
