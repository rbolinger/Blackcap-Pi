from __future__ import annotations

import configparser
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for, flash
from werkzeug.exceptions import ClientDisconnected, RequestEntityTooLarge

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from capture_recipe import CaptureRecipeError, extract_url_from_images, remove_capture_assets, save_capture_images, save_capture_recipe_image

CONFIG_PATH = Path(os.environ.get("INKY_CONFIG_PATH", str(Path.home() / "inky_menu_config.ini")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.secret_key = os.environ.get("INKY_ADMIN_SECRET", "change-me")

@dataclass
class RefreshStatus:
    running: bool = False
    mode: str = ""
    last_started: str = "Never"
    last_finished: str = "Never"
    last_return_code: str = "N/A"
    last_output: str = ""

refresh_status = RefreshStatus()
refresh_lock = threading.Lock()

DEFAULT_RECIPE_TYPES = ["Breakfast", "Lunch", "Dinner", "Side", "Snack", "Dessert"]


def recipe_type_options(recipes: list[dict] | None = None) -> list[str]:
    values = {item for item in DEFAULT_RECIPE_TYPES}
    for recipe in recipes or []:
        value = str(recipe.get("recipe_type", "")).strip()
        if value:
            values.add(value)
    return sorted(values, key=lambda value: (DEFAULT_RECIPE_TYPES.index(value) if value in DEFAULT_RECIPE_TYPES else 999, value.lower()))


def load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if CONFIG_PATH.exists():
        config.read(CONFIG_PATH)

    # Keep old sections, plus the new mode-aware sections.
    for section in [
        "inky_admin",
        "general",
        "normal_mode",
        "recipe_mode",
        "deep_clean_display",
        "recipe_repository",
        "api",
        "menu",
        "noun_project",
        "display",
        "paths",
        "footer",
        "processing",
    ]:
        if section not in config:
            config[section] = {}

    # Safe defaults for new keys. These do not overwrite existing config values.
    config["general"].setdefault("display_mode", "normal")

    # If display width/height were introduced under [general], mirror them from [display]
    # for compatibility, but keep [display] as the current source used by inky_menu.py.
    if not config["display"].get("display_width") and config["general"].get("display_width"):
        config["display"]["display_width"] = config["general"].get("display_width", "")
    if not config["display"].get("display_height") and config["general"].get("display_height"):
        config["display"]["display_height"] = config["general"].get("display_height", "")

    # Seed normal_mode paths from the legacy [paths] values if needed.
    config["normal_mode"].setdefault("python_path", config["paths"].get("python_path", "/home/pi/inky_env/bin/python3"))
    config["normal_mode"].setdefault("script_path", config["paths"].get("script_path", "/home/pi/Blackcap-Pi/inky_menu.py"))

    # Recipe mode defaults.
    config["recipe_mode"].setdefault("python_path", "/home/pi/inky_env/bin/python3")
    config["recipe_mode"].setdefault("script_path", "/home/pi/Blackcap-Pi/render_recipe_mode.py")
    config["recipe_mode"].setdefault("selected_recipe_id", "")

    # Deep clean display script. This is intentionally separate from normal/recipe
    # mode so the Admin UI button can run the same reset script used by cron.
    config["deep_clean_display"].setdefault("python_path", config["normal_mode"].get("python_path", "/home/pi/inky_env/bin/python3"))
    config["deep_clean_display"].setdefault("script_path", "/home/pi/Blackcap-Pi/inky_deep_clean.py")
    config["recipe_repository"].setdefault("repo_path", "/home/pi/Blackcap-Pi/inky_recipe_repo.json")
    config["recipe_repository"].setdefault("cache_dir", "/home/pi/Blackcap-Pi/recipe_cache")

    # Optional token for Chrome extension / API integrations. Leave blank to allow LAN-only
    # unauthenticated access, or set [api] extension_token and send:
    # Authorization: Bearer <token>
    config["api"].setdefault("extension_token", "")

    # Preview image paths. final_preview remains the normal menu image.
    # recipe_preview remains the recipe image. current_preview is the shared
    # admin preview of whatever is currently on the display.
    config["paths"].setdefault("current_preview", "/home/pi/Blackcap-Pi/current_view.png")
    config["paths"].setdefault("recipe_preview", "/home/pi/Blackcap-Pi/recipe_preview.png")
    config["paths"].setdefault("current_recipe_image", "/home/pi/Blackcap-Pi/recipe_cache/current_recipe_image.png")

    return config


def save_config(config: configparser.ConfigParser) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        config.write(f)


def cfg(config: configparser.ConfigParser, section: str, key: str) -> str:
    if not config.has_section(section):
        return ""
    return config.get(section, key, fallback="")


def get_display_mode(config: configparser.ConfigParser) -> str:
    mode = config.get("general", "display_mode", fallback="normal").strip().lower()
    if mode not in {"normal", "recipe"}:
        return "normal"
    return mode


def slugify_recipe_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return slug or "recipe"


def recipe_repo_path(config: configparser.ConfigParser) -> Path:
    raw = cfg(config, "recipe_repository", "repo_path").strip() or "/home/pi/Blackcap-Pi/inky_recipe_repo.json"
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def recipe_cache_dir(config: configparser.ConfigParser) -> Path:
    raw = cfg(config, "recipe_repository", "cache_dir").strip() or "/home/pi/Blackcap-Pi/recipe_cache"
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_recipe_repo(config: configparser.ConfigParser) -> dict:
    path = recipe_repo_path(config)
    if not path.exists():
        return {"recipes": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            repo = json.load(f)
    except Exception:
        return {"recipes": []}
    if not isinstance(repo, dict):
        return {"recipes": []}
    if not isinstance(repo.get("recipes"), list):
        repo["recipes"] = []
    return repo


def save_recipe_repo(config: configparser.ConfigParser, repo: dict) -> None:
    path = recipe_repo_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(repo, f, indent=2, ensure_ascii=False)
        f.write("\n")


def utc_now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def set_recipe_cache_build_status(
    config: configparser.ConfigParser,
    recipe_id: str,
    status: str,
    message: str = "",
    *,
    preserve_started_at: bool = False,
) -> None:
    repo = load_recipe_repo(config)
    now = utc_now_iso()
    changed = False
    for item in repo.get("recipes", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("id", "")).strip() != recipe_id:
            continue
        item["cache_build_status"] = status
        item["cache_build_message"] = message
        if status in {"pending", "building"}:
            if not preserve_started_at or not str(item.get("cache_build_started_at", "")).strip():
                item["cache_build_started_at"] = now
            item["cache_build_finished_at"] = ""
        elif status in {"ready", "error", "skipped"}:
            item["cache_build_finished_at"] = now
            if not str(item.get("cache_build_started_at", "")).strip():
                item["cache_build_started_at"] = now
        changed = True
        break
    if changed:
        save_recipe_repo(config, repo)


def configured_api_token(config: configparser.ConfigParser) -> str:
    return cfg(config, "api", "extension_token").strip()


def is_api_request_authorized(config: configparser.ConfigParser) -> bool:
    expected = configured_api_token(config)
    if not expected:
        # Token is optional for LAN-only usage. Set [api] extension_token to require it.
        return True

    auth_header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if auth_header.startswith(prefix):
        supplied = auth_header[len(prefix):].strip()
    else:
        supplied = request.headers.get("X-Blackcap-Token", "").strip()

    return supplied == expected


def recipe_payload_bool(payload, key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def add_or_update_recipe_from_payload(config: configparser.ConfigParser, payload, *, build_cache: bool = True) -> tuple[dict, bool, str]:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("Recipe name is required.")

    repo = load_recipe_repo(config)
    raw_recipes = [r for r in repo.get("recipes", []) if isinstance(r, dict)]
    existing_for_ids = [normalize_recipe_record(r) for r in raw_recipes]

    requested_id = str(payload.get("recipe_id", "")).strip()
    updating = bool(requested_id)

    recipe = normalize_recipe_record({
        "id": requested_id or name,
        "name": name,
        "description": payload.get("description", ""),
        "source": payload.get("source", "web"),
        "url": payload.get("url", ""),
        "layout": payload.get("layout", "two_page"),
        "file_type": payload.get("file_type", "html"),
        "recipe_type": payload.get("recipe_type", "Dinner"),
    })
    if recipe["source"] != "capture" and not recipe["url"]:
        raise ValueError("Recipe URL is required.")

    if updating:
        found = False
        for idx, item in enumerate(raw_recipes):
            if str(item.get("id", "")).strip() == requested_id:
                preserved = dict(item)
                preserved.update(recipe)
                preserved["id"] = requested_id
                preserve_keys = [
                    "capture_dir", "source_image_paths", "ocr_text_path", "recipe_model_path",
                    "cached_file_path", "cached_pdf_path", "cached_file_written_at", "cached_file_type",
                    "cached_source_url", "cached_source", "cached_layout", "cache_last_checked_at",
                    "cached_rendered_image_path", "cached_png_path", "cached_png_written_at",
                    "recipe_image_path", "dish_image_path", "recipe_image_written_at", "recipe_image_source_url",
                    "cache_build_status", "cache_build_message", "cache_build_started_at", "cache_build_finished_at",
                ]
                for extra_key in preserve_keys:
                    if extra_key in item and (extra_key not in recipe or not recipe.get(extra_key)):
                        preserved[extra_key] = item.get(extra_key)
                    if extra_key in payload:
                        preserved[extra_key] = payload.get(extra_key)
                raw_recipes[idx] = preserved
                recipe = normalize_recipe_record(preserved)
                found = True
                break
        if not found:
            raise LookupError("Recipe to edit was not found.")
    else:
        recipe["id"] = make_unique_recipe_id(existing_for_ids, recipe["name"])
        raw_recipes.append(recipe)

    repo["recipes"] = raw_recipes
    save_recipe_repo(config, repo)

    cache_ok = False
    cache_message = "Cache generation skipped."
    if build_cache:
        cache_ok, cache_message = build_recipe_cache(config, recipe["id"])

    repo_after_cache = load_recipe_repo(config)
    saved_recipe = next(
        (normalize_recipe_record(r) for r in repo_after_cache.get("recipes", []) if isinstance(r, dict) and str(r.get("id", "")) == recipe["id"]),
        recipe,
    )

    selected_after_add = recipe_payload_bool(payload, "select_after_add", False)
    if selected_after_add:
        if "recipe_mode" not in config:
            config["recipe_mode"] = {}
        config["recipe_mode"]["selected_recipe_id"] = recipe["id"]
        save_config(config)
        try:
            copy_recipe_image_to_current(config, saved_recipe)
        except Exception:
            pass

    return saved_recipe, cache_ok, cache_message


def normalize_recipe_record(recipe: dict) -> dict:
    recipe = recipe or {}
    name = str(recipe.get("name") or "").strip()
    source = str(recipe.get("source") or "web").strip().lower()
    layout = str(recipe.get("layout") or "two_page").strip().lower()
    file_type = str(recipe.get("file_type") or recipe.get("filetype") or "").strip().lower()
    recipe_id = str(recipe.get("id") or slugify_recipe_id(name)).strip()
    return {
        "id": recipe_id,
        "name": name or recipe_id,
        "description": str(recipe.get("description") or "").strip(),
        "source": source,
        "url": str(recipe.get("url") or "").strip(),
        "layout": layout,
        "file_type": file_type,
        "recipe_type": str(recipe.get("recipe_type") or recipe.get("meal_type") or recipe.get("type") or "Dinner").strip() or "Dinner",
        "cached_file_path": str(recipe.get("cached_file_path") or recipe.get("cached_pdf_path") or "").strip(),
        "cached_pdf_path": str(recipe.get("cached_pdf_path") or recipe.get("cached_file_path") or "").strip(),
        "cached_file_written_at": str(recipe.get("cached_file_written_at") or "").strip(),
        "cached_file_type": str(recipe.get("cached_file_type") or "").strip(),
        "cached_source_url": str(recipe.get("cached_source_url") or "").strip(),
        "cached_source": str(recipe.get("cached_source") or "").strip(),
        "cached_layout": str(recipe.get("cached_layout") or "").strip(),
        "cache_last_checked_at": str(recipe.get("cache_last_checked_at") or "").strip(),
        "recipe_image_path": str(recipe.get("recipe_image_path") or recipe.get("dish_image_path") or recipe.get("cached_image_path") or "").strip(),
        "dish_image_path": str(recipe.get("dish_image_path") or recipe.get("recipe_image_path") or recipe.get("cached_image_path") or "").strip(),
        "recipe_image_written_at": str(recipe.get("recipe_image_written_at") or "").strip(),
        "recipe_image_source_url": str(recipe.get("recipe_image_source_url") or "").strip(),
        "capture_dir": str(recipe.get("capture_dir") or "").strip(),
        "source_image_paths": recipe.get("source_image_paths") or [],
        "ocr_text_path": str(recipe.get("ocr_text_path") or "").strip(),
        "recipe_model_path": str(recipe.get("recipe_model_path") or "").strip(),
        "cache_build_status": str(recipe.get("cache_build_status") or "").strip().lower(),
        "cache_build_message": str(recipe.get("cache_build_message") or "").strip(),
        "cache_build_started_at": str(recipe.get("cache_build_started_at") or "").strip(),
        "cache_build_finished_at": str(recipe.get("cache_build_finished_at") or "").strip(),
    }


def list_all_recipes(config: configparser.ConfigParser) -> list[dict]:
    repo = load_recipe_repo(config)
    recipes = [normalize_recipe_record(r) for r in repo.get("recipes", []) if isinstance(r, dict)]
    return sorted(recipes, key=lambda r: r["name"].lower())


def recipe_has_ready_cache(recipe: dict | None) -> bool:
    if not recipe:
        return False
    pdf_path = recipe_pdf_path_for_record(recipe)
    return bool(pdf_path and pdf_path.exists() and pdf_path.is_file())


def list_recipes(config: configparser.ConfigParser, *, ready_only: bool = True) -> list[dict]:
    recipes = list_all_recipes(config)
    if ready_only:
        recipes = [r for r in recipes if recipe_has_ready_cache(r)]
    return recipes


def get_selected_recipe(config: configparser.ConfigParser) -> dict | None:
    selected_id = cfg(config, "recipe_mode", "selected_recipe_id").strip()
    if not selected_id:
        return None
    for recipe in list_recipes(config):
        if recipe["id"] == selected_id:
            return recipe
    return None


def current_recipe_image_path(config: configparser.ConfigParser) -> Path:
    raw = cfg(config, "paths", "current_recipe_image").strip() or "/home/pi/Blackcap-Pi/recipe_cache/current_recipe_image.png"
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def recipe_image_path_for_record(recipe: dict | None) -> Path | None:
    if not recipe:
        return None
    image_raw = str(recipe.get("recipe_image_path") or recipe.get("dish_image_path") or "").strip()
    if not image_raw:
        return None
    path = Path(os.path.expanduser(image_raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def recipe_image_url_for_record(recipe: dict | None) -> str:
    path = recipe_image_path_for_record(recipe)
    if not recipe or not path or not path.exists():
        return ""
    return url_for("recipe_image", recipe_id=recipe["id"]) + f"?t={int(path.stat().st_mtime)}"


def recipe_pdf_path_for_record(recipe: dict | None) -> Path | None:
    if not recipe:
        return None
    pdf_raw = str(recipe.get("cached_pdf_path") or recipe.get("cached_file_path") or "").strip()
    if not pdf_raw:
        return None
    path = Path(os.path.expanduser(pdf_raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def recipe_cache_status_for_record(recipe: dict | None) -> dict:
    pdf_path = recipe_pdf_path_for_record(recipe)
    ready = bool(pdf_path and pdf_path.exists() and pdf_path.is_file())
    status = str((recipe or {}).get("cache_build_status") or "").strip().lower()
    if ready:
        effective_status = "ready"
    elif status:
        effective_status = status
    else:
        effective_status = "not_built"
    return {
        "ready": ready,
        "status": effective_status,
        "message": str((recipe or {}).get("cache_build_message") or ""),
        "started_at": str((recipe or {}).get("cache_build_started_at") or ""),
        "finished_at": str((recipe or {}).get("cache_build_finished_at") or ""),
        "cached_pdf_path": str(pdf_path) if pdf_path else "",
        "cached_file_written_at": str((recipe or {}).get("cached_file_written_at") or ""),
    }


def copy_recipe_image_to_current(config: configparser.ConfigParser, recipe: dict | None) -> bool:
    dest = current_recipe_image_path(config)
    src = recipe_image_path_for_record(recipe)

    if not src or not src.exists() or not src.is_file():
        try:
            if dest.exists():
                dest.unlink()
        except Exception:
            pass
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return True


def make_unique_recipe_id(recipes: list[dict], preferred_id: str) -> str:
    existing_ids = {str(r.get("id", "")) for r in recipes}
    base = slugify_recipe_id(preferred_id)
    candidate = base
    counter = 2
    while candidate in existing_ids:
        candidate = f"{base}_{counter}"
        counter += 1
    return candidate


def build_recipe_cache(config: configparser.ConfigParser, recipe_id: str) -> tuple[bool, str]:
    """Pre-render and cache a recipe PDF without changing display mode or touching the display."""
    set_recipe_cache_build_status(config, recipe_id, "building", "Recipe cache build started.")
    python_path = path_from_value(cfg(config, "recipe_mode", "python_path"))
    script_path = path_from_value(cfg(config, "recipe_mode", "script_path"))

    if not python_path:
        msg = "Recipe Python Path is not set; recipe was saved but cache was not created."
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg
    if not script_path:
        msg = "Recipe Script Path is not set; recipe was saved but cache was not created."
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg
    if not python_path.exists():
        msg = f"Recipe Python Path does not exist: {python_path}"
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg
    if not script_path.exists():
        msg = f"Recipe Script Path does not exist: {script_path}"
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg

    env = os.environ.copy()
    env["INKY_CONFIG_PATH"] = str(CONFIG_PATH)

    try:
        result = subprocess.run(
            [str(python_path), str(script_path), "--cache-only", "--recipe-id", recipe_id, "--refresh-cache"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(script_path.parent),
            env=env,
        )
    except subprocess.TimeoutExpired:
        msg = "Recipe was saved, but cache generation timed out."
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg
    except Exception as exc:
        msg = f"Recipe was saved, but cache generation failed: {exc}"
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        msg = output or f"Recipe was saved, but cache generation failed with return code {result.returncode}."
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg

    msg = output or "Recipe cache created."
    set_recipe_cache_build_status(config, recipe_id, "ready", msg, preserve_started_at=True)
    return True, msg



def start_recipe_cache_build(config: configparser.ConfigParser, recipe_id: str) -> tuple[bool, str]:
    """Launch a recipe cache build in the background and return immediately."""
    set_recipe_cache_build_status(config, recipe_id, "building", "Recipe cache build queued.")
    python_path = path_from_value(cfg(config, "recipe_mode", "python_path"))
    script_path = path_from_value(cfg(config, "recipe_mode", "script_path"))

    if not python_path:
        msg = "Recipe Python Path is not set; cache build was not queued."
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg
    if not script_path:
        msg = "Recipe Script Path is not set; cache build was not queued."
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg
    if not python_path.exists():
        msg = f"Recipe Python Path does not exist: {python_path}"
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg
    if not script_path.exists():
        msg = f"Recipe Script Path does not exist: {script_path}"
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg

    env = os.environ.copy()
    env["INKY_CONFIG_PATH"] = str(CONFIG_PATH)

    try:
        proc = subprocess.Popen(
            [str(python_path), str(script_path), "--cache-only", "--recipe-id", recipe_id, "--refresh-cache"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(script_path.parent),
            env=env,
            start_new_session=True,
        )
    except Exception as exc:
        msg = f"Recipe was saved, but async cache build could not be started: {exc}"
        set_recipe_cache_build_status(config, recipe_id, "error", msg, preserve_started_at=True)
        return False, msg

    return True, f"Recipe cache build started in background. PID: {proc.pid}"


def path_from_value(raw: str) -> Path | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


def display_lock_path(config: configparser.ConfigParser) -> Path | None:
    return path_from_value(cfg(config, "paths", "lockfile"))


def display_lock_busy(config: configparser.ConfigParser) -> tuple[bool, str]:
    lock_path = display_lock_path(config)
    if lock_path and lock_path.exists():
        return True, f"Display is busy; lock file already exists: {lock_path}"
    return False, ""


def set_refresh_status_finished(mode: str, return_code: str, output: str) -> None:
    from datetime import datetime
    with refresh_lock:
        refresh_status.running = False
        refresh_status.mode = mode
        refresh_status.last_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        refresh_status.last_finished = refresh_status.last_started
        refresh_status.last_return_code = return_code
        refresh_status.last_output = output


def load_translation_rules(csv_path: Path | None) -> List[Dict[str, str]]:
    if not csv_path or not csv_path.exists():
        return []
    rows: List[Dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append({
                "label": (row.get("label") or "").strip(),
                "term": (row.get("term") or "").strip(),
                "priority": (row.get("priority") or "").strip(),
                "patterns": (row.get("patterns") or "").strip(),
            })
    return rows


def save_translation_rules(csv_path: Path, rows: List[Dict[str, str]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "term", "priority", "patterns"])
        writer.writeheader()
        for row in rows:
            label = row.get("label", "").strip()
            term = row.get("term", "").strip()
            priority = row.get("priority", "").strip()
            patterns = row.get("patterns", "").strip()
            if not (label or term or patterns):
                continue
            writer.writerow({"label": label, "term": term, "priority": priority, "patterns": patterns})


def collect_missing_required(config: configparser.ConfigParser) -> List[str]:
    mode = get_display_mode(config)

    # These are relevant in both modes.
    required_pairs = [
        ("inky_admin", "host"),
        ("inky_admin", "port"),
        ("general", "display_mode"),
        ("display", "display_width"),
        ("display", "display_height"),
        ("deep_clean_display", "python_path"),
        ("deep_clean_display", "script_path"),
    ]

    if mode == "recipe":
        required_pairs.extend([
            ("recipe_mode", "python_path"),
            ("recipe_mode", "script_path"),
            ("recipe_repository", "repo_path"),
            ("paths", "recipe_preview"),
            ("paths", "current_preview"),
        ])
    else:
        required_pairs.extend([
            ("menu", "url"), ("menu", "page_wait_seconds"),
            ("noun_project", "api_key"), ("noun_project", "secret_key"),
            ("display", "footer_height"),
            ("display", "body_x_offset"), ("display", "body_y_offset"), ("display", "icon_y_offset"),
            ("display", "text_y_offset"), ("display", "crop_left"), ("display", "crop_top"), ("display", "crop_right"),
            ("normal_mode", "python_path"), ("normal_mode", "script_path"),
            ("paths", "lockfile"), ("paths", "icon_cache_dir"),
            ("paths", "translations_csv"), ("paths", "current_snippet"), ("paths", "last_snippet"), ("paths", "temp_full"),
            ("paths", "final_preview"), ("paths", "current_preview"), ("paths", "ocr_preview"), ("paths", "menu_crop_preview"),
            ("footer", "max_icons"), ("footer", "icon_size"), ("footer", "font_path"), ("footer", "font_size"),
            ("processing", "contrast"), ("processing", "sharpness"), ("processing", "threshold"),
            ("processing", "ocr_scale"), ("processing", "ocr_threshold"), ("processing", "diff_threshold"),
        ])

    missing = []
    for section, key in required_pairs:
        if cfg(config, section, key).strip() == "":
            missing.append(f"[{section}] {key}")
    return missing


def save_url_scan_images(files, recipe_cache_dir: Path) -> list[str]:
    """Save temporary QR/footer URL photos and return normalized image paths."""
    from uuid import uuid4
    scan_dir = Path(recipe_cache_dir).expanduser().resolve() / "url_scans" / uuid4().hex
    scan_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    try:
        for index, uploaded in enumerate(files, start=1):
            if uploaded is None or not getattr(uploaded, "filename", ""):
                continue
            target = scan_dir / f"url_scan_{index:03d}.jpg"
            # Use PIL for normalization without depending on capture recipe file naming.
            from PIL import Image, ImageOps
            with Image.open(uploaded.stream) as img:
                img = ImageOps.exif_transpose(img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.thumbnail((3000, 3000), Image.LANCZOS)
                img.save(target, format="JPEG", quality=95)
            saved_paths.append(str(target))
        if not saved_paths:
            raise ValueError("Upload a QR code or URL footer photo first.")
        return saved_paths
    except Exception:
        shutil.rmtree(scan_dir, ignore_errors=True)
        raise


def create_capture_recipe_from_upload(config: configparser.ConfigParser, form, files) -> tuple[dict, bool, str]:
    name = str(form.get("name", "")).strip()
    if not name:
        raise ValueError("Recipe name is required.")

    repo = load_recipe_repo(config)
    raw_recipes = [r for r in repo.get("recipes", []) if isinstance(r, dict)]
    existing_for_ids = [normalize_recipe_record(r) for r in raw_recipes]
    recipe_id = make_unique_recipe_id(existing_for_ids, name)

    cache_dir = recipe_cache_dir(config)
    manifest = save_capture_images(files, cache_dir, recipe_id)
    capture_dir = str(manifest.get("capture_dir", ""))
    source_images = manifest.get("source_image_paths", [])
    # Keep the mobile photo-capture request lightweight: after the upload is saved,
    # OCR, dish-thumbnail detection, and PDF/image cache generation should happen in
    # the background cache process. Running save_capture_recipe_image() here can call
    # OCR helpers and make the user wait after the upload has already completed.
    preview_image_path = None

    recipe = normalize_recipe_record({
        "id": recipe_id,
        "name": name,
        "description": form.get("description", ""),
        "source": "capture",
        "url": "",
        "layout": form.get("layout", "two_page"),
        "file_type": "capture",
        "recipe_type": form.get("recipe_type", "Dinner"),
        "capture_dir": capture_dir,
        "source_image_paths": source_images,
        "ocr_text_path": str(Path(capture_dir) / "ocr_text.txt"),
        "recipe_model_path": str(Path(capture_dir) / "recipe_model.json"),
        "recipe_image_path": str(preview_image_path or ""),
        "dish_image_path": str(preview_image_path or ""),
        "cache_build_status": "pending",
        "cache_build_message": "Recipe photos uploaded; waiting for background OCR and PDF build.",
        "cache_build_started_at": utc_now_iso(),
        "cache_build_finished_at": "",
    })
    raw_recipes.append(recipe)
    repo["recipes"] = raw_recipes
    save_recipe_repo(config, repo)

    cache_ok, cache_message = start_recipe_cache_build(config, recipe_id)
    if cache_ok:
        cache_message = "Recipe photos uploaded. OCR and recipe PDF build are running in the background."
    repo_after_cache = load_recipe_repo(config)
    saved_recipe = next(
        (normalize_recipe_record(r) for r in repo_after_cache.get("recipes", []) if isinstance(r, dict) and str(r.get("id", "")) == recipe_id),
        recipe,
    )
    return saved_recipe, cache_ok, cache_message


@app.after_request
def add_api_cors_headers(response):
    # Allows a Chrome extension on the local network to call the admin API.
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Blackcap-Token")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    return response


@app.route("/api/recipes/add", methods=["POST", "OPTIONS"])
def api_add_recipe_from_extension():
    if request.method == "OPTIONS":
        return ("", 204)

    config = load_config()
    if not is_api_request_authorized(config):
        return jsonify({"ok": False, "error": "Unauthorized."}), 401

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Expected a JSON object."}), 400

    # Extension-friendly defaults. Cache builds default to async so the browser
    # extension/API call returns quickly while the Pi builds PDF/image cache in
    # the background.
    payload.setdefault("source", "web")
    payload.setdefault("file_type", "html")
    payload.setdefault("layout", "two_page")
    payload.setdefault("recipe_type", "Dinner")
    payload.setdefault("refresh_cache", True)
    payload.setdefault("cache_async", True)
    payload.setdefault("select_after_add", False)

    refresh_cache = recipe_payload_bool(payload, "refresh_cache", True)
    cache_async = recipe_payload_bool(payload, "cache_async", True)

    try:
        saved_recipe, cache_ok, cache_message = add_or_update_recipe_from_payload(
            config=config,
            payload=payload,
            build_cache=(refresh_cache and not cache_async),
        )

        cache_queued = False
        if refresh_cache and cache_async:
            queued_ok, queued_message = start_recipe_cache_build(config, saved_recipe["id"])
            cache_queued = queued_ok
            cache_ok = None if queued_ok else False
            cache_message = queued_message
        elif not refresh_cache:
            cache_ok = False
            cache_message = "Cache generation skipped."

    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not save recipe: {exc}"}), 500

    return jsonify({
        "ok": True,
        "recipe": saved_recipe,
        "recipe_image_url": recipe_image_url_for_record(saved_recipe),
        "cache_ok": cache_ok,
        "cache_queued": cache_queued,
        "cache_message": cache_message,
    })


@app.route("/api/recipes/ping", methods=["GET", "OPTIONS"])
def api_recipes_ping():
    if request.method == "OPTIONS":
        return ("", 204)
    config = load_config()
    authorized = is_api_request_authorized(config)
    return jsonify({
        "ok": authorized,
        "service": "Blackcap Pi Admin",
        "display_mode": get_display_mode(config),
        "auth_required": bool(configured_api_token(config)),
    }), (200 if authorized else 401)


@app.route("/", methods=["GET"])
def index():
    config = load_config()
    translations_path = path_from_value(cfg(config, "paths", "translations_csv"))
    current_preview = path_from_value(cfg(config, "paths", "current_preview"))
    if not current_preview:
        current_preview = path_from_value(cfg(config, "paths", "final_preview"))
    translations = load_translation_rules(translations_path)
    missing = collect_missing_required(config)
    preview_exists = bool(current_preview and current_preview.exists())
    final_preview = path_from_value(cfg(config, "paths", "final_preview"))
    final_preview_exists = bool(final_preview and final_preview.exists())
    recipes = list_recipes(config)
    selected_recipe = get_selected_recipe(config)
    recipe_image_path = recipe_image_path_for_record(selected_recipe)
    recipe_image_exists = bool(recipe_image_path and recipe_image_path.exists())
    selected_recipe_image_url = recipe_image_url_for_record(selected_recipe)
    return render_template(
        "index.html",
        config=config,
        translations=translations,
        status=refresh_status,
        config_path=str(CONFIG_PATH),
        translations_path=str(translations_path) if translations_path else "",
        missing=missing,
        preview_exists=preview_exists,
        final_preview_exists=final_preview_exists,
        display_mode=get_display_mode(config),
        recipes=recipes,
        selected_recipe=selected_recipe,
        recipe_image_exists=recipe_image_exists,
        selected_recipe_image_url=selected_recipe_image_url,
    )


@app.route("/status", methods=["GET"])
def status():
    config = load_config()
    preview_path = path_from_value(cfg(config, "paths", "current_preview")) or path_from_value(cfg(config, "paths", "final_preview"))
    preview_url = ""
    if preview_path and preview_path.exists():
        preview_url = url_for("preview_image") + f"?t={int(preview_path.stat().st_mtime)}"
    recipe_img = current_recipe_image_path(config)
    recipe_image_url = ""
    if recipe_img and recipe_img.exists():
        recipe_image_url = url_for("current_recipe_image") + f"?t={int(recipe_img.stat().st_mtime)}"
    with refresh_lock:
        return jsonify({
            "running": refresh_status.running,
            "mode": refresh_status.mode,
            "display_mode": get_display_mode(config),
            "last_started": refresh_status.last_started,
            "last_finished": refresh_status.last_finished,
            "last_return_code": refresh_status.last_return_code,
            "last_output": refresh_status.last_output,
            "preview_url": preview_url,
            "recipe_image_url": recipe_image_url,
            "final_preview_exists": bool(path_from_value(cfg(config, "paths", "final_preview")) and path_from_value(cfg(config, "paths", "final_preview")).exists()),
        })


@app.route("/preview-image", methods=["GET"])
def preview_image():
    config = load_config()
    preview_path = path_from_value(cfg(config, "paths", "current_preview")) or path_from_value(cfg(config, "paths", "final_preview"))
    if not preview_path or not preview_path.exists():
        return ("Preview image not found.", 404)
    return send_file(preview_path, mimetype="image/png")


@app.route("/current-recipe-image", methods=["GET"])
def current_recipe_image():
    config = load_config()
    image_path = current_recipe_image_path(config)
    if not image_path.exists():
        selected = get_selected_recipe(config)
        if selected:
            try:
                copy_recipe_image_to_current(config, selected)
            except Exception:
                pass
    if not image_path.exists():
        return ("Recipe image not found.", 404)
    return send_file(image_path, mimetype="image/png")


@app.route("/recipe-image/<recipe_id>", methods=["GET"])
def recipe_image(recipe_id: str):
    config = load_config()
    recipe = next((r for r in list_recipes(config) if r["id"] == recipe_id), None)
    image_path = recipe_image_path_for_record(recipe)
    if not image_path or not image_path.exists():
        return ("Recipe image not found.", 404)
    return send_file(image_path, mimetype="image/png")


@app.route("/set-display-mode", methods=["POST"])
def set_display_mode():
    mode = ""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("display_mode", "")).strip().lower()
    if not mode:
        mode = request.form.get("display_mode", "").strip().lower()

    if mode not in {"normal", "recipe"}:
        return jsonify({"ok": False, "error": "Invalid display mode."}), 400

    config = load_config()
    if "general" not in config:
        config["general"] = {}

    # Important: only update the mode here. Do not write any other form values.
    config["general"]["display_mode"] = mode
    save_config(config)

    return jsonify({
        "ok": True,
        "display_mode": mode,
        "missing": collect_missing_required(config),
    })


@app.route("/recipes", methods=["GET"])
def recipes_api():
    config = load_config()
    text = request.args.get("q", "").strip().lower()
    source = request.args.get("source", "").strip().lower()
    file_type = request.args.get("file_type", "").strip().lower()
    layout = request.args.get("layout", "").strip().lower()
    recipe_type = request.args.get("recipe_type", "").strip().lower()

    recipes = list_recipes(config)
    filtered = []
    for recipe in recipes:
        haystack = " ".join([
            recipe.get("name", ""),
            recipe.get("description", ""),
            recipe.get("source", ""),
            recipe.get("file_type", ""),
            recipe.get("layout", ""),
            recipe.get("recipe_type", ""),
            recipe.get("url", ""),
        ]).lower()
        if text and text not in haystack:
            continue
        if source and recipe.get("source") != source:
            continue
        if file_type and recipe.get("file_type") != file_type:
            continue
        if layout and recipe.get("layout") != layout:
            continue
        if recipe_type and recipe.get("recipe_type", "").lower() != recipe_type:
            continue
        recipe["recipe_image_url"] = recipe_image_url_for_record(recipe)
        recipe["cache_status"] = recipe_cache_status_for_record(recipe)
        filtered.append(recipe)

    return jsonify({"ok": True, "recipes": filtered})


@app.route("/select-recipe", methods=["POST"])
def select_recipe():
    recipe_id = ""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        recipe_id = str(payload.get("recipe_id", "")).strip()
    if not recipe_id:
        recipe_id = request.form.get("recipe_id", "").strip()

    config = load_config()
    recipe = next((r for r in list_recipes(config) if r["id"] == recipe_id), None)
    if not recipe:
        return jsonify({"ok": False, "error": "Recipe not found."}), 404

    if "recipe_mode" not in config:
        config["recipe_mode"] = {}
    config["recipe_mode"]["selected_recipe_id"] = recipe_id
    save_config(config)
    try:
        copy_recipe_image_to_current(config, recipe)
    except Exception:
        pass
    return jsonify({"ok": True, "recipe": recipe, "recipe_image_url": recipe_image_url_for_record(recipe)})


@app.route("/add-recipe", methods=["POST"])
def add_recipe():
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    else:
        payload = request.form

    try:
        saved_recipe, cache_ok, cache_message = add_or_update_recipe_from_payload(config=load_config(), payload=payload, build_cache=True)
    except LookupError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not save recipe: {exc}"}), 500

    return jsonify({
        "ok": True,
        "recipe": saved_recipe,
        "cache_ok": cache_ok,
        "cache_message": cache_message,
        "recipe_image_url": recipe_image_url_for_record(saved_recipe),
    })


@app.route("/get-recipe/<recipe_id>", methods=["GET"])
def get_recipe_api(recipe_id: str):
    config = load_config()
    recipe = next((r for r in list_recipes(config) if r["id"] == recipe_id), None)
    if not recipe:
        return jsonify({"ok": False, "error": "Recipe not found."}), 404
    recipe["recipe_image_url"] = recipe_image_url_for_record(recipe)
    return jsonify({"ok": True, "recipe": recipe})


@app.route("/delete-recipe/<recipe_id>", methods=["POST"])
def delete_recipe_api(recipe_id: str):
    config = load_config()
    repo = load_recipe_repo(config)
    recipes = [r for r in repo.get("recipes", []) if isinstance(r, dict)]
    recipe = next((r for r in recipes if str(r.get("id", "")).strip() == recipe_id), None)
    if not recipe:
        return jsonify({"ok": False, "error": "Recipe not found."}), 404
    if str(recipe.get("source", "")).strip().lower() == "capture":
        remove_capture_assets(recipe)
    repo["recipes"] = [r for r in recipes if str(r.get("id", "")).strip() != recipe_id]
    save_recipe_repo(config, repo)
    if cfg(config, "recipe_mode", "selected_recipe_id").strip() == recipe_id:
        config["recipe_mode"]["selected_recipe_id"] = ""
        save_config(config)
    return jsonify({"ok": True})


@app.route("/refresh-recipe/<recipe_id>", methods=["POST"])
def refresh_recipe_api(recipe_id: str):
    config = load_config()
    recipe = next((r for r in list_recipes(config) if r["id"] == recipe_id), None)
    if not recipe:
        return jsonify({"ok": False, "error": "Recipe not found."}), 404
    if str(recipe.get("source", "")).strip().lower() == "capture":
        return jsonify({"ok": False, "error": "Capture recipes are created from uploaded photos and cannot refresh from a web source."}), 400
    cache_ok, cache_message = build_recipe_cache(config, recipe_id)
    repo_after_cache = load_recipe_repo(config)
    saved_recipe = next(
        (normalize_recipe_record(r) for r in repo_after_cache.get("recipes", []) if isinstance(r, dict) and str(r.get("id", "")) == recipe_id),
        recipe,
    )
    if cfg(config, "recipe_mode", "selected_recipe_id").strip() == recipe_id:
        try:
            copy_recipe_image_to_current(config, saved_recipe)
        except Exception:
            pass
    return jsonify({"ok": cache_ok, "message": cache_message, "recipe": saved_recipe, "recipe_image_url": recipe_image_url_for_record(saved_recipe)})


@app.route("/save-settings", methods=["POST"])
def save_settings():
    config = load_config()

    # Ensure all sections exist before assignment.
    for section in [
        "inky_admin", "general", "normal_mode", "recipe_mode", "deep_clean_display", "recipe_repository", "api",
        "menu", "noun_project", "display", "paths", "footer", "processing",
    ]:
        if section not in config:
            config[section] = {}

    display_mode = request.form.get("display_mode", "normal").strip().lower()
    if display_mode not in {"normal", "recipe"}:
        display_mode = "normal"

    fields = {
        # Always visible / shared settings.
        ("general", "display_mode"): display_mode,
        ("inky_admin", "host"): request.form.get("host", "").strip(),
        ("inky_admin", "port"): request.form.get("port", "").strip(),
        ("display", "display_width"): request.form.get("display_width", "").strip(),
        ("display", "display_height"): request.form.get("display_height", "").strip(),

        # Keep these mirrored for any future code that reads [general].
        ("general", "display_width"): request.form.get("display_width", "").strip(),
        ("general", "display_height"): request.form.get("display_height", "").strip(),

        # Normal mode script entry points.
        ("normal_mode", "python_path"): request.form.get("normal_python_path", "").strip(),
        ("normal_mode", "script_path"): request.form.get("normal_script_path", "").strip(),

        # Legacy [paths] entries are still used by the current inky_menu.py/admin refresh code.
        ("paths", "python_path"): request.form.get("normal_python_path", "").strip(),
        ("paths", "script_path"): request.form.get("normal_script_path", "").strip(),

        # Recipe mode script entry points and repo settings.
        ("recipe_mode", "python_path"): request.form.get("recipe_python_path", "").strip(),
        ("recipe_mode", "script_path"): request.form.get("recipe_script_path", "").strip(),
        ("recipe_mode", "selected_recipe_id"): request.form.get("selected_recipe_id", "").strip(),

        # Deep clean display script entry point.
        ("deep_clean_display", "python_path"): request.form.get("deep_clean_python_path", "").strip(),
        ("deep_clean_display", "script_path"): request.form.get("deep_clean_script_path", "").strip(),
        ("deep_clean_display", "post_white_delay_seconds"): request.form.get("deep_clean_post_white_delay_seconds", "").strip(),

        ("recipe_repository", "repo_path"): request.form.get("recipe_repo_path", "").strip(),

        # Normal menu settings. These form controls are hidden in recipe mode, not disabled,
        # so they still post and will not be wiped when switching modes.
        ("menu", "url"): request.form.get("menu_url", "").strip(),
        ("menu", "page_wait_seconds"): request.form.get("page_wait_seconds", "").strip(),
        ("noun_project", "api_key"): request.form.get("api_key", "").strip(),
        ("noun_project", "secret_key"): request.form.get("secret_key", "").strip(),
        ("display", "footer_height"): request.form.get("footer_height", "").strip(),
        ("display", "body_x_offset"): request.form.get("body_x_offset", "").strip(),
        ("display", "body_y_offset"): request.form.get("body_y_offset", "").strip(),
        ("display", "icon_y_offset"): request.form.get("icon_y_offset", "").strip(),
        ("display", "text_y_offset"): request.form.get("text_y_offset", "").strip(),
        ("display", "crop_left"): request.form.get("crop_left", "").strip(),
        ("display", "crop_top"): request.form.get("crop_top", "").strip(),
        ("display", "crop_right"): request.form.get("crop_right", "").strip(),
        ("paths", "lockfile"): request.form.get("lockfile", "").strip(),
        ("paths", "icon_cache_dir"): request.form.get("icon_cache_dir", "").strip(),
        ("paths", "translations_csv"): request.form.get("translations_csv", "").strip(),
        ("paths", "current_snippet"): request.form.get("current_snippet", "").strip(),
        ("paths", "last_snippet"): request.form.get("last_snippet", "").strip(),
        ("paths", "temp_full"): request.form.get("temp_full", "").strip(),
        ("paths", "final_preview"): request.form.get("final_preview", "").strip(),
        ("paths", "recipe_preview"): request.form.get("recipe_preview", "").strip(),
        ("paths", "current_preview"): request.form.get("current_preview", "").strip(),
        ("paths", "current_recipe_image"): request.form.get("current_recipe_image", "").strip(),
        ("paths", "ocr_preview"): request.form.get("ocr_preview", "").strip(),
        ("paths", "menu_crop_preview"): request.form.get("menu_crop_preview", "").strip(),
        ("footer", "max_icons"): request.form.get("max_icons", "").strip(),
        ("footer", "icon_size"): request.form.get("icon_size", "").strip(),
        ("footer", "font_path"): request.form.get("font_path", "").strip(),
        ("footer", "font_size"): request.form.get("font_size", "").strip(),
        ("processing", "contrast"): request.form.get("contrast", "").strip(),
        ("processing", "sharpness"): request.form.get("sharpness", "").strip(),
        ("processing", "threshold"): request.form.get("threshold", "").strip(),
        ("processing", "ocr_scale"): request.form.get("ocr_scale", "").strip(),
        ("processing", "ocr_threshold"): request.form.get("ocr_threshold", "").strip(),
        ("processing", "diff_threshold"): request.form.get("diff_threshold", "").strip(),
    }

    for (section, key), value in fields.items():
        config[section][key] = value

    save_config(config)
    flash("Settings saved.", "success")
    return redirect(url_for("index"))


@app.route("/save-translations", methods=["POST"])
def save_translations():
    config = load_config()
    translations_path = path_from_value(cfg(config, "paths", "translations_csv"))
    if not translations_path:
        flash("Set [paths] translations_csv first.", "error")
        return redirect(url_for("index"))
    rows = []
    for label, term, priority, patterns in zip(
        request.form.getlist("label"),
        request.form.getlist("term"),
        request.form.getlist("priority"),
        request.form.getlist("patterns"),
    ):
        rows.append({"label": label.strip(), "term": term.strip(), "priority": priority.strip(), "patterns": patterns.strip()})
    save_translation_rules(translations_path, rows)
    flash("Rule table saved.", "success")
    return redirect(url_for("index"))


@app.route("/add-translation", methods=["POST"])
def add_translation():
    config = load_config()
    translations_path = path_from_value(cfg(config, "paths", "translations_csv"))
    if not translations_path:
        flash("Set [paths] translations_csv first.", "error")
        return redirect(url_for("index"))
    rows = load_translation_rules(translations_path)
    rows.append({
        "label": request.form.get("new_label", "").strip(),
        "term": request.form.get("new_term", "").strip(),
        "priority": request.form.get("new_priority", "").strip(),
        "patterns": request.form.get("new_patterns", "").strip(),
    })
    save_translation_rules(translations_path, rows)
    flash("Rule added.", "success")
    return redirect(url_for("index"))


@app.route("/delete-translation", methods=["POST"])
def delete_translation():
    config = load_config()
    translations_path = path_from_value(cfg(config, "paths", "translations_csv"))
    if not translations_path:
        flash("Set [paths] translations_csv first.", "error")
        return redirect(url_for("index"))
    rows = load_translation_rules(translations_path)
    row_index = int(request.form.get("row_index", "-1"))
    rows = [row for idx, row in enumerate(rows) if idx != row_index]
    save_translation_rules(translations_path, rows)
    flash("Rule deleted.", "success")
    return redirect(url_for("index"))


def append_output(text: str) -> None:
    if not text:
        return
    with refresh_lock:
        refresh_status.last_output += text


def run_refresh_thread(mode: str) -> None:
    from datetime import datetime
    config = load_config()
    display_mode = get_display_mode(config)

    busy, busy_message = display_lock_busy(config)
    if busy:
        set_refresh_status_finished(f"{display_mode}:{mode}", "busy", busy_message)
        return

    if display_mode == "recipe":
        python_path = path_from_value(cfg(config, "recipe_mode", "python_path"))
        script_path = path_from_value(cfg(config, "recipe_mode", "script_path"))
    else:
        python_path = path_from_value(cfg(config, "normal_mode", "python_path")) or path_from_value(cfg(config, "paths", "python_path"))
        script_path = path_from_value(cfg(config, "normal_mode", "script_path")) or path_from_value(cfg(config, "paths", "script_path"))

    if not python_path:
        with refresh_lock:
            refresh_status.running = False
            refresh_status.last_return_code = "missing"
            refresh_status.last_finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            refresh_status.last_output = f"Set [{display_mode}_mode] python_path in the admin UI."
        return

    if not script_path:
        with refresh_lock:
            refresh_status.running = False
            refresh_status.last_return_code = "missing"
            refresh_status.last_finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            refresh_status.last_output = f"Set [{display_mode}_mode] script_path in the admin UI."
        return

    cmd = [str(python_path), str(script_path)]
    if display_mode == "normal" and mode == "full":
        cmd.append("--full-refresh")

    env = os.environ.copy()
    env["INKY_CONFIG_PATH"] = str(CONFIG_PATH)

    with refresh_lock:
        refresh_status.running = True
        refresh_status.mode = f"{display_mode}:{mode}"
        refresh_status.last_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        refresh_status.last_finished = "Running"
        refresh_status.last_return_code = "running"
        refresh_status.last_output = ""

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(script_path.parent),
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_output(line)
        code = process.wait()
        with refresh_lock:
            refresh_status.last_return_code = str(code)
    except Exception as exc:
        with refresh_lock:
            refresh_status.last_return_code = "error"
            refresh_status.last_output += ("" if refresh_status.last_output.endswith("\n") or refresh_status.last_output == "" else "\n") + str(exc)
    finally:
        with refresh_lock:
            refresh_status.last_finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            refresh_status.running = False


def run_deep_clean_display_thread() -> None:
    from datetime import datetime
    config = load_config()

    busy, busy_message = display_lock_busy(config)
    if busy:
        set_refresh_status_finished("deep_clean_display", "busy", busy_message)
        return

    python_path = path_from_value(cfg(config, "deep_clean_display", "python_path"))
    script_path = path_from_value(cfg(config, "deep_clean_display", "script_path"))

    if not python_path:
        with refresh_lock:
            refresh_status.running = False
            refresh_status.last_return_code = "missing"
            refresh_status.last_finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            refresh_status.last_output = "Set [deep_clean_display] python_path in the admin UI."
        return

    if not script_path:
        with refresh_lock:
            refresh_status.running = False
            refresh_status.last_return_code = "missing"
            refresh_status.last_finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            refresh_status.last_output = "Set [deep_clean_display] script_path in the admin UI."
        return

    cmd = [str(python_path), str(script_path)]
    env = os.environ.copy()
    env["INKY_CONFIG_PATH"] = str(CONFIG_PATH)

    with refresh_lock:
        refresh_status.running = True
        refresh_status.mode = "deep_clean_display"
        refresh_status.last_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        refresh_status.last_finished = "Running"
        refresh_status.last_return_code = "running"
        refresh_status.last_output = ""

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(script_path.parent),
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_output(line)
        code = process.wait()
        with refresh_lock:
            refresh_status.last_return_code = str(code)
    except Exception as exc:
        with refresh_lock:
            refresh_status.last_return_code = "error"
            refresh_status.last_output += ("" if refresh_status.last_output.endswith("\n") or refresh_status.last_output == "" else "\n") + str(exc)
    finally:
        with refresh_lock:
            refresh_status.last_finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            refresh_status.running = False


def restore_last_menu_image_to_display(config: configparser.ConfigParser) -> tuple[bool, str, int]:
    """Copy/display the last rendered menu image without running a new menu refresh."""
    busy, busy_message = display_lock_busy(config)
    if busy:
        return False, busy_message, 409

    preview_path = path_from_value(cfg(config, "paths", "final_preview"))
    if not preview_path or not preview_path.exists():
        return False, "Last menu image not found. Run a normal refresh first.", 404

    current_preview_path = path_from_value(cfg(config, "paths", "current_preview")) or Path("/home/pi/Blackcap-Pi/current_view.png")

    python_path = path_from_value(cfg(config, "normal_mode", "python_path")) or path_from_value(cfg(config, "paths", "python_path"))
    if not python_path:
        return False, "Normal Python Path is not set.", 400

    inline_code = """
import sys
from pathlib import Path
from PIL import Image
from waveshare_epd import epd13in3k

image_path = Path(sys.argv[1])
current_preview_path = Path(sys.argv[2])
img = Image.open(image_path).convert("1")
current_preview_path.parent.mkdir(parents=True, exist_ok=True)
img.save(current_preview_path)
epd = epd13in3k.EPD()
epd.init()
epd.display(epd.getbuffer(img))
epd.sleep()
print(f"Restored image to display: {image_path}")
print(f"Current display preview saved to: {current_preview_path}")
"""

    lock_path = display_lock_path(config)
    lock_file_created = False
    try:
        if lock_path:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                return False, f"Display is busy; lock file already exists: {lock_path}", 409
            with os.fdopen(fd, "w") as lock_file:
                lock_file.write(str(os.getpid()))
            lock_file_created = True

        result = subprocess.run(
            [str(python_path), "-c", inline_code, str(preview_path), str(current_preview_path)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(preview_path.parent),
        )
    except subprocess.TimeoutExpired:
        return False, "Timed out while restoring the last menu image.", 500
    except Exception as exc:
        return False, str(exc), 500
    finally:
        if lock_path and lock_file_created:
            lock_path.unlink(missing_ok=True)

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return False, output or f"Restore failed with return code {result.returncode}.", 500

    return True, output or "Last menu image restored to display.", 200


def return_to_normal_menu_mode(config: configparser.ConfigParser) -> tuple[bool, str, int]:
    """Restore the last menu image and switch back to Normal Menu mode only after success."""
    if get_display_mode(config) != "recipe":
        return False, "Back to Menu is only available after a recipe has been rendered.", 400

    ok, message, status_code = restore_last_menu_image_to_display(config)
    if not ok:
        return ok, message, status_code

    if "general" not in config:
        config["general"] = {}
    config["general"]["display_mode"] = "normal"
    save_config(config)
    return True, message or "Returned to Normal Menu mode.", 200


@app.route("/restore-last-menu-image", methods=["POST"])
def restore_last_menu_image():
    config = load_config()
    if get_display_mode(config) != "normal":
        return jsonify({"ok": False, "error": "Restore Last Menu Image is only available in Normal Menu mode."}), 400

    ok, message, status_code = restore_last_menu_image_to_display(config)
    return jsonify({"ok": ok, "message" if ok else "error": message}), status_code


@app.route("/back-to-menu", methods=["POST"])
def back_to_menu():
    from datetime import datetime

    with refresh_lock:
        if refresh_status.running:
            return jsonify({
                "ok": False,
                "error": "A display action is already running.",
                "display_mode": get_display_mode(load_config()),
            }), 409

        refresh_status.running = True
        refresh_status.mode = "back_to_menu"
        refresh_status.last_started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        refresh_status.last_finished = "Running"
        refresh_status.last_return_code = "running"
        refresh_status.last_output = "Restoring last menu image and returning to Normal Menu mode..."

    config = load_config()
    ok, message, status_code = return_to_normal_menu_mode(config)

    with refresh_lock:
        refresh_status.running = False
        refresh_status.mode = "back_to_menu"
        refresh_status.last_finished = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        refresh_status.last_return_code = "0" if ok else ("busy" if status_code == 409 else "error")
        refresh_status.last_output = message

    return jsonify({
        "ok": ok,
        "message" if ok else "error": message,
        "display_mode": get_display_mode(load_config()),
    }), status_code


@app.route("/run-refresh/<mode>", methods=["POST"])
def run_refresh(mode: str):
    if mode not in {"smart", "full", "recipe"}:
        flash("Invalid refresh mode.", "error")
        return redirect(url_for("index"))
    with refresh_lock:
        if refresh_status.running:
            flash("A refresh is already running.", "error")
            return redirect(url_for("index"))
    config = load_config()
    busy, busy_message = display_lock_busy(config)
    if busy:
        set_refresh_status_finished(mode, "busy", busy_message)
        flash(busy_message, "error")
        return redirect(url_for("index"))
    thread = threading.Thread(target=run_refresh_thread, args=(mode,), daemon=True)
    thread.start()
    flash(f"{mode.title()} refresh started.", "success")
    return redirect(url_for("index"))



@app.route("/run-display-reset", methods=["POST"])
def run_display_reset():
    with refresh_lock:
        if refresh_status.running:
            flash("A display action is already running.", "error")
            return redirect(url_for("index"))
    config = load_config()
    busy, busy_message = display_lock_busy(config)
    if busy:
        set_refresh_status_finished("deep_clean_display", "busy", busy_message)
        flash(busy_message, "error")
        return redirect(url_for("index"))
    thread = threading.Thread(target=run_deep_clean_display_thread, daemon=True)
    thread.start()
    flash("Deep clean display started.", "success")
    return redirect(url_for("index"))



def _start_refresh_if_idle(mode: str) -> tuple[bool, str]:
    """Start a display refresh in the background if one is not already running."""
    with refresh_lock:
        if refresh_status.running:
            return False, "A refresh is already running."
    config = load_config()
    busy, busy_message = display_lock_busy(config)
    if busy:
        set_refresh_status_finished(mode, "busy", busy_message)
        return False, busy_message
    thread = threading.Thread(target=run_refresh_thread, args=(mode,), daemon=True)
    thread.start()
    return True, f"{mode.title()} refresh started."


@app.route("/api/recipes/<recipe_id>/cache-status", methods=["GET"])
def recipe_cache_status_api(recipe_id: str):
    config = load_config()
    recipe = next((r for r in list_all_recipes(config) if r["id"] == recipe_id), None)
    if not recipe:
        return jsonify({"ok": False, "error": "Recipe not found."}), 404
    status = recipe_cache_status_for_record(recipe)
    return jsonify({
        "ok": True,
        "recipe_id": recipe_id,
        "ready": status["ready"],
        "status": status["status"],
        "message": status["message"],
        "started_at": status["started_at"],
        "finished_at": status["finished_at"],
        "cached_pdf_path": status["cached_pdf_path"],
        "cached_file_written_at": status["cached_file_written_at"],
        "recipe_image_url": recipe_image_url_for_record(recipe),
    })


@app.route("/mobile/extract-url", methods=["POST"])
def mobile_extract_url_from_photo():
    config = load_config()
    temp_paths: list[str] = []
    temp_root: Path | None = None
    try:
        app.logger.info("[URL SCAN] Request received; content_length=%s", request.content_length)
        files = request.files.getlist("url_photos")
        app.logger.info("[URL SCAN] Files received: %s", len(files))
        if not files:
            return jsonify({"ok": False, "error": "No URL or QR photo was uploaded."}), 400

        temp_paths = save_url_scan_images(files, recipe_cache_dir(config))
        if temp_paths:
            temp_root = Path(temp_paths[0]).parent
        app.logger.info("[URL SCAN] Saved temp paths: %s", temp_paths)

        app.logger.info("[URL SCAN] Starting QR-first URL extraction")
        result = extract_url_from_images(temp_paths)
        url = str(result.get("url") or "").strip()
        urls = result.get("urls", []) or []
        ocr_text = str(result.get("ocr_text") or "").strip()
        app.logger.info("[URL SCAN] Extraction complete; url=%r candidates=%s ocr_text_len=%s", url, urls, len(ocr_text))

        if not url:
            return jsonify({"ok": False, "error": "No URL found. Try retaking the photo closer to the QR code or footer URL."}), 200

        return jsonify({"ok": True, "url": url, "urls": urls})
    except ClientDisconnected:
        app.logger.warning("[URL SCAN] Client disconnected before upload completed")
        return jsonify({"ok": False, "error": "The photo upload was interrupted. Try again with a smaller or closer photo."}), 400
    except RequestEntityTooLarge:
        app.logger.warning("[URL SCAN] Upload too large")
        return jsonify({"ok": False, "error": "The photo is too large. Try a smaller photo or lower camera resolution."}), 413
    except (CaptureRecipeError, ValueError) as exc:
        app.logger.info("[URL SCAN] Could not extract URL: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 200
    except Exception as exc:
        app.logger.exception("[URL SCAN] Unexpected error")
        return jsonify({"ok": False, "error": f"Could not extract URL: {exc}"}), 500
    finally:
        if temp_root:
            app.logger.info("[URL SCAN] Cleaning temp dir: %s", temp_root)
            shutil.rmtree(temp_root, ignore_errors=True)


@app.route("/mobile/add-recipe", methods=["GET"])
def mobile_add_recipe_page():
    config = load_config()
    recipes = list_recipes(config)
    recipe_types = recipe_type_options(recipes)
    return render_template("mobile_add_recipe.html", recipe_types=recipe_types)


@app.route("/mobile/add-recipe", methods=["POST"])
def mobile_add_recipe_submit():
    config = load_config()
    add_method = str(request.form.get("add_method", "url")).strip().lower()
    try:
        if add_method == "capture":
            saved_recipe, cache_ok, cache_message = create_capture_recipe_from_upload(
                config,
                request.form,
                request.files.getlist("photos"),
            )
        else:
            payload = {
                "name": request.form.get("name", ""),
                "description": request.form.get("description", ""),
                "source": "web",
                "file_type": "html",
                "layout": request.form.get("layout", "two_page"),
                "recipe_type": request.form.get("recipe_type", "Dinner"),
                "url": request.form.get("url", ""),
                "refresh_cache": True,
                "cache_async": True,
                "select_after_add": False,
            }
            saved_recipe, _, _ = add_or_update_recipe_from_payload(config=config, payload=payload, build_cache=False)
            cache_ok, cache_message = start_recipe_cache_build(config, saved_recipe["id"])
    except (CaptureRecipeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Could not add recipe: {exc}"}), 500

    cache_status = recipe_cache_status_for_record(saved_recipe)
    return jsonify({
        "ok": True,
        "recipe": saved_recipe,
        "cache_queued": bool(cache_ok),
        "cache_message": cache_message,
        "cache_ready": cache_status["ready"],
        "cache_status": cache_status["status"],
        "cache_status_message": cache_status["message"],
        "cached_pdf_path": cache_status["cached_pdf_path"],
        "recipe_image_url": recipe_image_url_for_record(saved_recipe),
    })


@app.route("/mobile", methods=["GET"])
def mobile_control():
    config = load_config()
    recipes = list_recipes(config)
    for recipe in recipes:
        recipe["recipe_image_url"] = recipe_image_url_for_record(recipe)
    selected_recipe = get_selected_recipe(config)
    if selected_recipe:
        selected_recipe["recipe_image_url"] = recipe_image_url_for_record(selected_recipe)
    recipe_types = recipe_type_options(recipes)
    return render_template(
        "mobile.html",
        display_mode=get_display_mode(config),
        recipes=recipes,
        recipe_types=recipe_types,
        selected_recipe=selected_recipe,
        status=refresh_status,
    )


@app.route("/mobile/render-recipe", methods=["POST"])
def mobile_render_recipe():
    payload = request.get_json(silent=True) or {}
    recipe_id = str(payload.get("recipe_id", "")).strip() or request.form.get("recipe_id", "").strip()
    if not recipe_id:
        return jsonify({"ok": False, "error": "Choose a recipe first."}), 400

    config = load_config()
    recipe = next((r for r in list_recipes(config) if r["id"] == recipe_id), None)
    if not recipe:
        return jsonify({"ok": False, "error": "Recipe not found."}), 404

    cache_status = recipe_cache_status_for_record(recipe)
    if not cache_status["ready"]:
        return jsonify({
            "ok": False,
            "error": "Recipe cache is still building. Please wait until the PDF is ready before rendering.",
            "cache_ready": False,
        }), 409

    # Selection/preview should not switch modes. Only switch to Recipe Mode
    # once we know the recipe render can actually start.
    with refresh_lock:
        if refresh_status.running:
            return jsonify({
                "ok": False,
                "error": "A refresh is already running.",
                "display_mode": get_display_mode(config),
            }), 409

    busy, busy_message = display_lock_busy(config)
    if busy:
        set_refresh_status_finished("recipe", "busy", busy_message)
        return jsonify({
            "ok": False,
            "error": busy_message,
            "display_mode": get_display_mode(config),
        }), 409

    if "recipe_mode" not in config:
        config["recipe_mode"] = {}
    if "general" not in config:
        config["general"] = {}

    config["recipe_mode"]["selected_recipe_id"] = recipe_id
    config["general"]["display_mode"] = "recipe"
    save_config(config)

    try:
        copy_recipe_image_to_current(config, recipe)
    except Exception:
        pass

    thread = threading.Thread(target=run_refresh_thread, args=("recipe",), daemon=True)
    thread.start()
    return jsonify({
        "ok": True,
        "message": "Recipe refresh started.",
        "display_mode": "recipe",
        "recipe": recipe,
        "recipe_image_url": recipe_image_url_for_record(recipe),
    })


@app.route("/mobile/normal-mode", methods=["POST"])
def mobile_normal_mode():
    config = load_config()
    ok, message, status_code = return_to_normal_menu_mode(config)
    return jsonify({
        "ok": ok,
        "message" if ok else "error": message,
        "display_mode": get_display_mode(load_config()),
    }), status_code


if __name__ == "__main__":
    config = load_config()
    host = cfg(config, "inky_admin", "host") or "0.0.0.0"
    port = int(cfg(config, "inky_admin", "port") or "8080")
    app.run(host=host, port=port, debug=False)
