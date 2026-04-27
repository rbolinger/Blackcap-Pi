#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import configparser
import csv
import fcntl
import hashlib
import hmac
import io
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageStat
from playwright.sync_api import sync_playwright
from waveshare_epd import epd13in3k

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("INKY_CONFIG_PATH", str(Path.home() / "inky_menu_config.ini")))

REQUIRED_FIELDS = {
    "display": ["display_width","display_height","footer_height","body_x_offset","body_y_offset","icon_y_offset","text_y_offset","crop_left","crop_top","crop_right"],
    "paths": ["lockfile","icon_cache_dir","translations_csv","current_snippet","last_snippet","temp_full","final_preview","ocr_preview","menu_crop_preview"],
    "menu": ["url","page_wait_seconds"],
    "noun_project": ["api_key","secret_key"],
    "footer": ["max_icons","icon_size","font_path","font_size"],
    "processing": ["contrast","sharpness","threshold","ocr_scale","ocr_threshold","diff_threshold"],
}

lock_file = None

class ConfigError(Exception):
    pass

def admin_error(message: str) -> ConfigError:
    return ConfigError(f"{message} Set this value in the admin UI and save settings.")

def require(config: configparser.ConfigParser, section: str, key: str) -> str:
    if not config.has_section(section) or not config.has_option(section, key):
        raise admin_error(f"Missing required config value [{section}] {key}.")
    value = config.get(section, key).strip()
    if value == "":
        raise admin_error(f"Blank required config value [{section}] {key}.")
    return value

def require_int(config: configparser.ConfigParser, section: str, key: str) -> int:
    value = require(config, section, key)
    try:
        return int(value)
    except ValueError:
        raise admin_error(f"Invalid integer for [{section}] {key}: {value}")

def require_float(config: configparser.ConfigParser, section: str, key: str) -> float:
    value = require(config, section, key)
    try:
        return float(value)
    except ValueError:
        raise admin_error(f"Invalid number for [{section}] {key}: {value}")

def require_path(config: configparser.ConfigParser, section: str, key: str) -> Path:
    raw = require(config, section, key)
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return path

def ensure_config_complete(config: configparser.ConfigParser) -> None:
    for section, keys in REQUIRED_FIELDS.items():
        if not config.has_section(section):
            raise admin_error(f"Missing required config section [{section}].")
        for key in keys:
            require(config, section, key)

def load_config():
    if not CONFIG_PATH.exists():
        raise ConfigError(f"Config file not found: {CONFIG_PATH}. Create it through the admin UI or place it at this path.")
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    ensure_config_complete(config)

    runtime = {}
    runtime["display_mode"] = config.get("general", "display_mode", fallback="normal").strip().lower()
    runtime["display_width"] = require_int(config, "display", "display_width")
    runtime["display_height"] = require_int(config, "display", "display_height")
    runtime["footer_height"] = require_int(config, "display", "footer_height")
    runtime["body_x_offset"] = require_int(config, "display", "body_x_offset")
    runtime["body_y_offset"] = require_int(config, "display", "body_y_offset")
    runtime["icon_y_offset"] = require_int(config, "display", "icon_y_offset")
    runtime["text_y_offset"] = require_int(config, "display", "text_y_offset")
    runtime["crop_left"] = require_int(config, "display", "crop_left")
    runtime["crop_top"] = require_int(config, "display", "crop_top")
    runtime["crop_right"] = require_int(config, "display", "crop_right")
    runtime["main_content_height"] = runtime["display_height"] - runtime["footer_height"]
    runtime["crop_bottom"] = runtime["crop_top"] + runtime["main_content_height"]

    if runtime["footer_height"] <= 0 or runtime["footer_height"] >= runtime["display_height"]:
        raise admin_error("[display] footer_height must be greater than 0 and less than display_height.")

    runtime["lock_path"] = require_path(config, "paths", "lockfile")
    runtime["icon_cache_dir"] = require_path(config, "paths", "icon_cache_dir")
    runtime["words_csv_path"] = require_path(config, "paths", "translations_csv")
    runtime["current_snippet"] = require_path(config, "paths", "current_snippet")
    runtime["last_snippet"] = require_path(config, "paths", "last_snippet")
    runtime["temp_full"] = require_path(config, "paths", "temp_full")
    runtime["final_preview"] = require_path(config, "paths", "final_preview")
    runtime["current_preview"] = require_path(config, "paths", "current_preview") if config.has_option("paths", "current_preview") else Path("/home/pi/current_view.png")
    runtime["ocr_preview"] = require_path(config, "paths", "ocr_preview")
    runtime["menu_crop_preview"] = require_path(config, "paths", "menu_crop_preview")

    runtime["menu_url"] = require(config, "menu", "url")
    runtime["page_wait_seconds"] = require_float(config, "menu", "page_wait_seconds")
    runtime["noun_key"] = require(config, "noun_project", "api_key")
    runtime["noun_secret"] = require(config, "noun_project", "secret_key")
    runtime["max_icons"] = require_int(config, "footer", "max_icons")
    runtime["icon_size"] = require_int(config, "footer", "icon_size")
    runtime["font_path"] = require(config, "footer", "font_path")
    runtime["font_size"] = require_int(config, "footer", "font_size")
    runtime["contrast"] = require_float(config, "processing", "contrast")
    runtime["sharpness"] = require_float(config, "processing", "sharpness")
    runtime["threshold"] = require_int(config, "processing", "threshold")
    runtime["ocr_scale"] = require_int(config, "processing", "ocr_scale")
    runtime["ocr_threshold"] = require_int(config, "processing", "ocr_threshold")
    runtime["diff_threshold"] = require_float(config, "processing", "diff_threshold")

    runtime["icon_cache_dir"].mkdir(parents=True, exist_ok=True)
    for path_key in ["lock_path","words_csv_path","current_snippet","last_snippet","temp_full","final_preview","current_preview","ocr_preview","menu_crop_preview"]:
        runtime[path_key].parent.mkdir(parents=True, exist_ok=True)

    return runtime

RUNTIME = load_config()

if RUNTIME.get("display_mode", "normal") != "normal":
    print(f"Blackcap Pi is in {RUNTIME['display_mode']} display mode; normal menu update skipped.")
    sys.exit(1)

def acquire_lock():
    global lock_file
    lock_file = open(RUNTIME["lock_path"], "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.write(str(os.getpid()))
        lock_file.flush()
    except BlockingIOError:
        print("Another instance is already running. Exiting.")
        sys.exit(0)

def load_icon_rules(csv_path: Path):
    if not csv_path.exists():
        raise admin_error(f"Translations CSV not found: {csv_path}")
    rules = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required_columns = {"label", "term", "priority", "patterns"}
        if not reader.fieldnames or not required_columns.issubset(set(reader.fieldnames)):
            raise admin_error(f"{csv_path} must contain columns: label, term, priority, patterns.")
        for row in reader:
            label = (row.get("label") or "").strip()
            term = (row.get("term") or "").strip()
            priority_raw = (row.get("priority") or "").strip()
            patterns_raw = (row.get("patterns") or "").strip()
            if not label or not term or not priority_raw or not patterns_raw:
                continue
            try:
                priority = int(priority_raw)
            except ValueError:
                print(f"Skipping row with invalid priority: {row}")
                continue
            patterns = []
            for pattern in patterns_raw.split("|"):
                pattern = pattern.strip().lower()
                if pattern:
                    patterns.append(r"\b" + re.escape(pattern) + r"\b")
            if patterns:
                rules.append({"label": label, "term": term, "priority": priority, "patterns": patterns})
    if not rules:
        raise admin_error("No valid rules found in the translations CSV.")
    return rules

ICON_RULES = load_icon_rules(RUNTIME["words_csv_path"])

def oauth_percent_encode(value: str) -> str:
    return urllib.parse.quote(str(value), safe="~-._")

def build_oauth1_header(method: str, url: str, query_params: dict, consumer_key: str, consumer_secret: str) -> str:
    nonce = hashlib.md5(f"{time.time()}-{random.random()}".encode()).hexdigest()
    timestamp = str(int(time.time()))
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": timestamp,
        "oauth_version": "1.0",
    }
    all_params = {**query_params, **oauth_params}
    param_string = "&".join(f"{oauth_percent_encode(k)}={oauth_percent_encode(v)}" for k, v in sorted(all_params.items()))
    base_string = "&".join([method.upper(), oauth_percent_encode(url), oauth_percent_encode(param_string)])
    signing_key = f"{oauth_percent_encode(consumer_secret)}&"
    digest = hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()
    signature = base64.b64encode(digest).decode()
    oauth_params["oauth_signature"] = signature
    return "OAuth " + ", ".join(f'{oauth_percent_encode(k)}="{oauth_percent_encode(v)}"' for k, v in sorted(oauth_params.items()))

def search_noun_icon(term: str, size: int = 128):
    url = "https://api.thenounproject.com/v2/icon"
    params = {"query": term, "limit": 1, "thumbnail_size": size}
    auth_header = build_oauth1_header("GET", url, params, RUNTIME["noun_key"], RUNTIME["noun_secret"])
    response = requests.get(url, params=params, headers={"Authorization": auth_header}, timeout=30)
    response.raise_for_status()
    icons = response.json().get("icons", [])
    return icons[0] if icons else None

def get_icon_cache_path(term: str, target_size: int) -> Path:
    safe_name = "".join(c for c in term.lower() if c.isalnum() or c in ("_", "-"))
    return RUNTIME["icon_cache_dir"] / f"{safe_name}_{target_size}.png"

def get_icon_image(term: str, target_size: int):
    cache_path = get_icon_cache_path(term, target_size)
    if cache_path.exists():
        img = Image.open(cache_path).convert("L")
    else:
        print(f"Downloading icon for '{term}' from Noun Project...")
        result = search_noun_icon(term, size=128)
        if not result:
            print(f"No icon found for '{term}'")
            return None
        icon_url = result.get("thumbnail_url") or result.get("preview_url") or result.get("icon_url")
        if not icon_url:
            print(f"No downloadable icon URL for '{term}'")
            return None
        response = requests.get(icon_url, timeout=30)
        response.raise_for_status()
        img = Image.open(io.BytesIO(response.content)).convert("RGBA")
        background = Image.new("RGBA", img.size, "WHITE")
        img = Image.alpha_composite(background, img).convert("L")
        img.thumbnail((target_size, target_size), Image.LANCZOS)
        img.save(cache_path)
    img = ImageEnhance.Contrast(img).enhance(2.0)
    img = img.point(lambda p: 0 if p < 220 else 255).convert("1")
    return img

def normalize_text(text: str) -> str:
    text = text.lower().replace("\n", " ").replace("&", " and ")
    return re.sub(r"\s+", " ", text).strip()

def prepare_image_for_ocr(img: Image.Image) -> Image.Image:
    ocr_img = img.convert("L")
    ocr_img = ocr_img.resize((ocr_img.width * RUNTIME["ocr_scale"], ocr_img.height * RUNTIME["ocr_scale"]), Image.LANCZOS)
    ocr_img = ImageEnhance.Contrast(ocr_img).enhance(2.5)
    ocr_img = ImageEnhance.Sharpness(ocr_img).enhance(2.0)
    ocr_img = ocr_img.filter(ImageFilter.MedianFilter(size=3))
    ocr_img = ocr_img.point(lambda p: 0 if p < RUNTIME["ocr_threshold"] else 255)
    return ocr_img

def run_ocr_on_image(img: Image.Image) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        img_path = os.path.join(tmpdir, "ocr_input.png")
        out_base = os.path.join(tmpdir, "ocr_output")
        img.save(img_path)
        result = subprocess.run(["tesseract", img_path, out_base, "--psm", "6", "-l", "eng"], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Tesseract failed: {result.stderr.strip()}")
        txt_path = out_base + ".txt"
        if not os.path.exists(txt_path):
            return ""
        with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
            return normalize_text(f.read())

def detect_footer_items(ocr_text: str):
    matches = []
    for rule in ICON_RULES:
        for pattern in rule["patterns"]:
            if re.search(pattern, ocr_text, flags=re.IGNORECASE):
                matches.append({"label": rule["label"], "term": rule["term"], "priority": rule["priority"]})
                break
    unique = {}
    for item in matches:
        if item["label"] not in unique:
            unique[item["label"]] = item
    selected = sorted(unique.values(), key=lambda x: x["priority"], reverse=True)[:RUNTIME["max_icons"]]
    return [{"term": item["term"], "label": item["label"]} for item in selected]

def get_body_image_from_full(img_path: Path) -> Image.Image:
    full_img = Image.open(img_path).convert("L")
    return full_img.crop((RUNTIME["crop_left"], RUNTIME["crop_top"], RUNTIME["crop_right"], RUNTIME["crop_bottom"]))

def get_display_body_image(body_img: Image.Image) -> Image.Image:
    display_img = body_img.copy()
    display_img = ImageEnhance.Contrast(display_img).enhance(RUNTIME["contrast"])
    display_img = ImageEnhance.Sharpness(display_img).enhance(RUNTIME["sharpness"])
    display_img = display_img.point(lambda p: 0 if p < RUNTIME["threshold"] else 255).convert("1")
    return display_img

def build_footer(items):
    footer = Image.new("1", (RUNTIME["display_width"], RUNTIME["footer_height"]), 255)
    draw = ImageDraw.Draw(footer)
    try:
        font = ImageFont.truetype(RUNTIME["font_path"], RUNTIME["font_size"])
    except Exception as exc:
        raise admin_error(f"Could not load font at {RUNTIME['font_path']}: {exc}")
    if not items:
        return footer
    draw.line((0, 0, RUNTIME["display_width"], 0), fill=0, width=1)
    num_items = min(len(items), RUNTIME["max_icons"])
    cell_width = RUNTIME["display_width"] // max(num_items, 1)
    for idx, item in enumerate(items[:RUNTIME["max_icons"]]):
        x0 = idx * cell_width
        cell_center_x = x0 + cell_width // 2
        icon = None
        try:
            icon = get_icon_image(item["term"], RUNTIME["icon_size"])
        except Exception as exc:
            print(f"Error fetching icon '{item['term']}': {exc}")
        if icon:
            icon_x = cell_center_x - icon.width // 2
            icon_y = 5 + RUNTIME["icon_y_offset"]
            footer.paste(icon, (icon_x, icon_y))
        label = item["label"]
        bbox = draw.textbbox((0, 0), label, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = cell_center_x - text_width // 2
        text_y = RUNTIME["footer_height"] - text_height - 14 + RUNTIME["text_y_offset"]
        draw.text((text_x, text_y), label, font=font, fill=0)
    return footer

def compose_final_image(body_display_img: Image.Image, footer_img: Image.Image) -> Image.Image:
    final_img = Image.new("1", (RUNTIME["display_width"], RUNTIME["display_height"]), 255)
    final_img.paste(body_display_img, (RUNTIME["body_x_offset"], RUNTIME["body_y_offset"]))
    final_img.paste(footer_img, (0, RUNTIME["main_content_height"]))
    return final_img

def capture_full_image(output_path: Path):
    browser = None
    try:
        with sync_playwright() as p:
            print("1. Launching browser...")
            browser = p.chromium.launch(args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": 960, "height": 800})
            print("2. Loading menu page...")
            page.goto(RUNTIME["menu_url"], wait_until="networkidle", timeout=60000)
            print(f"3. Waiting {RUNTIME['page_wait_seconds']} seconds for render...")
            time.sleep(RUNTIME["page_wait_seconds"])
            print("4. Capturing screenshot...")
            page.screenshot(path=str(output_path))
    finally:
        if browser:
            try:
                browser.close()
                print("Browser closed successfully.")
            except Exception:
                pass

def update_display(full_img_path: Path):
    print("5. Preparing body image...")
    body_img = get_body_image_from_full(full_img_path)
    body_img.save(RUNTIME["menu_crop_preview"])
    print("6. Preparing OCR image...")
    ocr_img = prepare_image_for_ocr(body_img)
    ocr_img.save(RUNTIME["ocr_preview"])
    print("7. Running OCR...")
    ocr_text = run_ocr_on_image(ocr_img)
    print("OCR text sample:")
    print(ocr_text[:1000])
    footer_items = detect_footer_items(ocr_text)
    print(f"Footer items: {footer_items}")
    print("8. Processing body for e-ink...")
    display_body = get_display_body_image(body_img)
    print("9. Building footer...")
    footer = build_footer(footer_items)
    print("10. Compositing final image...")
    final_img = compose_final_image(display_body, footer)
    final_img.save(RUNTIME["final_preview"])
    final_img.save(RUNTIME["current_preview"])
    print(f"Current display preview saved to: {RUNTIME['current_preview']}")
    print("11. Initializing display...")
    epd = epd13in3k.EPD()
    epd.init()
    epd.display(epd.getbuffer(final_img))
    epd.sleep()
    print("12. Update complete.")

def run_full_refresh():
    capture_full_image(RUNTIME["temp_full"])
    update_display(RUNTIME["temp_full"])
    body_img = get_body_image_from_full(RUNTIME["temp_full"])
    body_img.save(RUNTIME["last_snippet"])
    if RUNTIME["current_snippet"].exists():
        RUNTIME["current_snippet"].unlink(missing_ok=True)

def run_smart_refresh():
    capture_full_image(RUNTIME["temp_full"])
    full_img = Image.open(RUNTIME["temp_full"]).convert("L")
    snippet = full_img.crop((RUNTIME["crop_left"], RUNTIME["crop_top"], RUNTIME["crop_right"], RUNTIME["crop_bottom"]))
    snippet.save(RUNTIME["current_snippet"])
    print("5. Body snippet saved.")
    if not RUNTIME["last_snippet"].exists():
        print("6. First run. Performing initial display update...")
        update_display(RUNTIME["temp_full"])
        os.replace(RUNTIME["current_snippet"], RUNTIME["last_snippet"])
        return
    curr_img = Image.open(RUNTIME["current_snippet"])
    last_img = Image.open(RUNTIME["last_snippet"])
    curr_blur = curr_img.filter(ImageFilter.GaussianBlur(radius=1))
    last_blur = last_img.filter(ImageFilter.GaussianBlur(radius=1))
    diff = ImageChops.difference(curr_blur, last_blur)
    stat = ImageStat.Stat(diff)
    diff_score = stat.mean[0]
    print(f"6. Change Score: {diff_score:.4f} (Threshold: {RUNTIME['diff_threshold']})")
    if diff_score > RUNTIME["diff_threshold"]:
        print("   >>> CHANGE DETECTED. Refreshing display...")
        update_display(RUNTIME["temp_full"])
        os.replace(RUNTIME["current_snippet"], RUNTIME["last_snippet"])
    else:
        print("   >>> NO SIGNIFICANT CHANGE. Skipping refresh.")
        RUNTIME["current_snippet"].unlink(missing_ok=True)

def parse_args():
    parser = argparse.ArgumentParser(description="Inky menu renderer. Runs smart refresh by default; use --full-refresh to force an update.")
    parser.add_argument("--full-refresh", action="store_true", help="Force a full refresh.")
    return parser.parse_args()

def main():
    args = parse_args()
    acquire_lock()
    if args.full_refresh:
        print("Running full refresh mode...")
        run_full_refresh()
    else:
        print("Running smart refresh mode...")
        run_smart_refresh()

if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        sys.exit(2)
