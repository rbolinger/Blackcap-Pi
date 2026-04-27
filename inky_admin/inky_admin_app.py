from __future__ import annotations

import configparser
import csv
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for, flash

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("INKY_CONFIG_PATH", str(Path.home() / "inky_menu_config.ini")))

app = Flask(__name__)
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
    config["normal_mode"].setdefault("script_path", config["paths"].get("script_path", "/home/pi/inky_menu.py"))

    # Recipe mode defaults.
    config["recipe_mode"].setdefault("python_path", "/home/pi/inky_env/bin/python3")
    config["recipe_mode"].setdefault("script_path", "/home/pi/render_recipe_mode.py")
    config["recipe_mode"].setdefault("selected_recipe_id", "")
    config["recipe_repository"].setdefault("repo_path", "/home/pi/inky_recipe_repo.json")

    # Optional token for Chrome extension / API integrations. Leave blank to allow LAN-only
    # unauthenticated access, or set [api] extension_token and send:
    # Authorization: Bearer <token>
    config["api"].setdefault("extension_token", "")

    # Preview image paths. final_preview remains the normal menu image.
    # recipe_preview remains the recipe image. current_preview is the shared
    # admin preview of whatever is currently on the display.
    config["paths"].setdefault("current_preview", "/home/pi/current_view.png")
    config["paths"].setdefault("recipe_preview", "/home/pi/recipe_preview.png")
    config["paths"].setdefault("current_recipe_image", "/home/pi/recipe_cache/current_recipe_image.png")

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
    raw = cfg(config, "recipe_repository", "repo_path").strip() or "/home/pi/inky_recipe_repo.json"
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
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
    if not recipe["url"]:
        raise ValueError("Recipe URL is required.")

    if updating:
        found = False
        for idx, item in enumerate(raw_recipes):
            if str(item.get("id", "")).strip() == requested_id:
                preserved = dict(item)
                preserved.update(recipe)
                preserved["id"] = requested_id
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
    }


def list_recipes(config: configparser.ConfigParser) -> list[dict]:
    repo = load_recipe_repo(config)
    recipes = [normalize_recipe_record(r) for r in repo.get("recipes", []) if isinstance(r, dict)]
    return sorted(recipes, key=lambda r: r["name"].lower())


def get_selected_recipe(config: configparser.ConfigParser) -> dict | None:
    selected_id = cfg(config, "recipe_mode", "selected_recipe_id").strip()
    if not selected_id:
        return None
    for recipe in list_recipes(config):
        if recipe["id"] == selected_id:
            return recipe
    return None


def current_recipe_image_path(config: configparser.ConfigParser) -> Path:
    raw = cfg(config, "paths", "current_recipe_image").strip() or "/home/pi/recipe_cache/current_recipe_image.png"
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
    python_path = path_from_value(cfg(config, "recipe_mode", "python_path"))
    script_path = path_from_value(cfg(config, "recipe_mode", "script_path"))

    if not python_path:
        return False, "Recipe Python Path is not set; recipe was saved but cache was not created."
    if not script_path:
        return False, "Recipe Script Path is not set; recipe was saved but cache was not created."
    if not python_path.exists():
        return False, f"Recipe Python Path does not exist: {python_path}"
    if not script_path.exists():
        return False, f"Recipe Script Path does not exist: {script_path}"

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
        return False, "Recipe was saved, but cache generation timed out."
    except Exception as exc:
        return False, f"Recipe was saved, but cache generation failed: {exc}"

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return False, output or f"Recipe was saved, but cache generation failed with return code {result.returncode}."

    return True, output or "Recipe cache created."



def start_recipe_cache_build(config: configparser.ConfigParser, recipe_id: str) -> tuple[bool, str]:
    """Launch a recipe cache build in the background and return immediately."""
    python_path = path_from_value(cfg(config, "recipe_mode", "python_path"))
    script_path = path_from_value(cfg(config, "recipe_mode", "script_path"))

    if not python_path:
        return False, "Recipe Python Path is not set; cache build was not queued."
    if not script_path:
        return False, "Recipe Script Path is not set; cache build was not queued."
    if not python_path.exists():
        return False, f"Recipe Python Path does not exist: {python_path}"
    if not script_path.exists():
        return False, f"Recipe Script Path does not exist: {script_path}"

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
        return False, f"Recipe was saved, but async cache build could not be started: {exc}"

    return True, f"Recipe cache build started in background. PID: {proc.pid}"


def path_from_value(raw: str) -> Path | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path


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
        "inky_admin", "general", "normal_mode", "recipe_mode", "recipe_repository", "api",
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


@app.route("/restore-last-menu-image", methods=["POST"])
def restore_last_menu_image():
    config = load_config()
    if get_display_mode(config) != "normal":
        return jsonify({"ok": False, "error": "Restore Last Menu Image is only available in Normal Menu mode."}), 400

    preview_path = path_from_value(cfg(config, "paths", "final_preview"))
    if not preview_path or not preview_path.exists():
        return jsonify({"ok": False, "error": "Last menu image not found. Run a normal refresh first."}), 404

    current_preview_path = path_from_value(cfg(config, "paths", "current_preview")) or Path("/home/pi/current_view.png")

    python_path = path_from_value(cfg(config, "normal_mode", "python_path")) or path_from_value(cfg(config, "paths", "python_path"))
    if not python_path:
        return jsonify({"ok": False, "error": "Normal Python Path is not set."}), 400

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

    try:
        result = subprocess.run(
            [str(python_path), "-c", inline_code, str(preview_path), str(current_preview_path)],
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(preview_path.parent),
        )
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Timed out while restoring the last menu image."}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        return jsonify({"ok": False, "error": output or f"Restore failed with return code {result.returncode}."}), 500

    return jsonify({"ok": True, "message": output or "Last menu image restored to display."})


@app.route("/run-refresh/<mode>", methods=["POST"])
def run_refresh(mode: str):
    if mode not in {"smart", "full", "recipe"}:
        flash("Invalid refresh mode.", "error")
        return redirect(url_for("index"))
    with refresh_lock:
        if refresh_status.running:
            flash("A refresh is already running.", "error")
            return redirect(url_for("index"))
    thread = threading.Thread(target=run_refresh_thread, args=(mode,), daemon=True)
    thread.start()
    flash(f"{mode.title()} refresh started.", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    config = load_config()
    host = cfg(config, "inky_admin", "host") or "0.0.0.0"
    port = int(cfg(config, "inky_admin", "port") or "8080")
    app.run(host=host, port=port, debug=False)
