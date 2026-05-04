#!/usr/bin/env python3

import os
import sys
import time
import configparser
import subprocess
from pathlib import Path
from PIL import Image, ImageEnhance
from waveshare_epd import epd13in3k

# --- CONFIG PATH ---
CONFIG_PATH = os.environ.get("INKY_CONFIG_PATH", "/home/pi/inky_menu_config.ini")

# --- LOAD CONFIG ---
config = configparser.ConfigParser()
config.read(CONFIG_PATH)

# --- DISPLAY SETTINGS ---
DISPLAY_WIDTH = 960
DISPLAY_HEIGHT = 680

# --- LOCK FILE ---
LOCK_FILE = Path(config.get("general", "lockfile", fallback="/tmp/inky_menu_display.lock"))

# --- PATHS ---
PROJECT_ROOT = Path("/home/pi")

CURRENT_VIEW = PROJECT_ROOT / "current_view.png"
MENU_FALLBACK = PROJECT_ROOT / "final_render_preview.png"
RECIPE_FALLBACK = PROJECT_ROOT / "recipe_preview.png"

# --- MODE DETECTION ---
DISPLAY_MODE = config.get("general", "display_mode", fallback="menu").lower()

if CURRENT_VIEW.exists():
    restore_image_path = CURRENT_VIEW
elif DISPLAY_MODE == "recipe":
    restore_image_path = RECIPE_FALLBACK
else:
    restore_image_path = MENU_FALLBACK


# ================================
# LOCK HANDLING
# ================================
def acquire_lock():
    print(f"[LOCK] Checking for existing lock: {LOCK_FILE}")

    if LOCK_FILE.exists():
        print(f"[LOCK] Display is already in use. Lock file exists: {LOCK_FILE}")
        print("[LOCK] Exiting without running deep clean.")
        sys.exit(0)

    print("[LOCK] Acquiring lock")
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock():
    if LOCK_FILE.exists():
        print("[LOCK] Releasing lock")
        LOCK_FILE.unlink()


# ================================
# RESTORE-ONLY MODE
# ================================
def restore_only():
    acquire_lock()

    try:
        print("[MODE] Restore-only mode")
        print(f"[INFO] Restoring image: {restore_image_path}")

        epd = epd13in3k.EPD()
        epd.init()

        if restore_image_path.exists():
            img = Image.open(restore_image_path).convert("L")
            img = ImageEnhance.Contrast(img).enhance(1.4)
            img = img.convert("1")

            epd.display(epd.getbuffer(img))
            time.sleep(1)
            epd.display(epd.getbuffer(img))

            print(f"[SUCCESS] Restored image: {restore_image_path}")
        else:
            print(f"[WARNING] Restore image not found: {restore_image_path}")

        epd.sleep()

    finally:
        release_lock()


# ================================
# MAIN CLEAN + SPAWN RESTORE
# ================================
def main():
    acquire_lock()

    try:
        print(f"[INFO] Display mode: {DISPLAY_MODE}")
        print(f"[INFO] Restore image: {restore_image_path}")

        epd = epd13in3k.EPD()
        epd.init()

        black_img = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 0)
        white_img = Image.new("1", (DISPLAY_WIDTH, DISPLAY_HEIGHT), 255)

        print("Stage 1: Full refresh waveform cycle")

        print("  -> White")
        epd.display(epd.getbuffer(white_img))
        time.sleep(1)

        print("  -> Black")
        epd.display(epd.getbuffer(black_img))
        time.sleep(1)

        print("  -> White")
        epd.display(epd.getbuffer(white_img))
        time.sleep(1)

        epd.sleep()

        print("[INFO] Clean cycle complete")

    finally:
        release_lock()

    # --- SPAWN RESTORE AS NEW PROCESS ---
    print("[INFO] Spawning restore process")

    python_path = config.get("general", "python_path", fallback="/home/pi/inky_env/bin/python3")

    subprocess.Popen([
        python_path,
        __file__,
        "--restore-only"
    ])


# ================================
# ENTRY POINT
# ================================
if __name__ == "__main__":
    if "--restore-only" in sys.argv:
        restore_only()
    else:
        main()
