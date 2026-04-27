# Inky Pi Display

A Raspberry Pi project for rendering menus and recipes to a Waveshare e-ink display, powered by a smart rendering pipeline and a web-based admin UI.

---

## 📸 Overview

Inky Pi turns a Raspberry Pi + e-ink display into a dynamic information panel for:

* 📅 School or weekly menus
* 🍽 Recipes from the web, Dropbox, or Google Drive
* 🧠 Smart visual enhancements (icons, formatting, layout)

All managed through a clean browser-based admin interface.

---

## 🚀 Features

### 📅 Smart Menu Mode

* Pulls menu data from a web source
* Uses OCR + image diff detection to avoid unnecessary updates
* Adds contextual icons via Noun Project
* Automatically formats for e-ink readability

---

### 🍽 Recipe Mode

* Switch display to show a selected recipe
* Supports:

  * Web pages
  * Dropbox shared links
  * Google Drive links
* Automatically extracts:

  * Recipe text
  * Recipe image
* Generates and caches:

  * PDF render
  * Preview image
* Smart auto-fit text layout

---

### 🖥 Admin Web UI (Port 8080)

* Toggle between Menu and Recipe modes
* Search, add, edit, delete recipes
* Filter recipes by:

  * Type (Breakfast, Dinner, etc.)
  * Source
  * File type
  * Layout
* Preview current display output
* Trigger refresh jobs
* Restore last menu image

---

### ⚙️ Smart Rendering Pipeline

* Lockfile protection (prevents concurrent runs)
* Image diff detection
* Cached rendering for performance
* Playwright fallback for complex websites
* Automatic font scaling for layout fit

---

## 🏗 Project Structure

```text
inky-pi-project/
├── inky_menu.py                 # Menu rendering engine
├── render_recipe_mode.py        # Recipe rendering engine
├── inky_menu_config.ini         # Local config (NOT committed)
├── requirements.txt
│
├── inky_admin/
│   ├── inky_admin_app.py        # Flask web server
│   ├── templates/
│   │   └── index.html           # Admin UI
│   └── static/
│       ├── style.css
│       └── icons/images
│
├── recipe_cache/                # Cached PDFs & images (ignored)
├── noun_cache/                  # Cached icons (ignored)
```

---

## ⚙️ Requirements

* Raspberry Pi (tested on Pi 4 / Zero 2 W)
* Waveshare 13.3" e-ink display (or similar)
* Python **3.13**
* Raspberry Pi OS

---

## 🛠 Installation

### 1. Clone the repository

```bash
git clone https://github.com/rbolinger/Blackcap-Pi.git
cd Blackcap-Pi
```

---

### 2. Create virtual environment

```bash
python3 -m venv inky_env
source inky_env/bin/activate
```

---

### 3. Install dependencies

```bash
pip install -r requirements.txt
playwright install
```

---

### 4. Configure the system

```bash
cp inky_menu_config.ini.example inky_menu_config.ini
nano inky_menu_config.ini
```

Set:

* API keys (Noun Project)
* Display dimensions
* Script paths

---

### 5. Run the admin UI

```bash
cd inky_admin
python inky_admin_app.py
```

Open in browser:

```text
http://<raspberry-pi-ip>:8080
```

---

## 🔄 Usage

### Menu Mode

* Displays menu data
* Adds icons automatically
* Only updates when content changes

---

### Recipe Mode

1. Open admin UI
2. Search or add a recipe
3. Select it
4. Click **Render Recipe**

The system:

* Fetches content
* Builds cached PDF
* Extracts image
* Displays it on e-ink

---

## 🧠 Key Concepts

### Smart Diff Rendering

Avoids unnecessary display refreshes (important for e-ink longevity)

### Recipe Caching

Stored in `/recipe_cache`
Reduces load times and avoids repeated scraping

### Lockfile System

Prevents multiple scripts from running simultaneously

---

## 🔐 Security Notes

The following are intentionally excluded from Git:

* `inky_menu_config.ini`
* API keys
* Cached images / PDFs
* Tokens or credentials

---

## 🛠 Troubleshooting

### Display not updating

* Check lockfile
* Verify GPIO permissions
* Restart script

### Recipe image not loading

* Some sites block direct requests
* Playwright fallback is used automatically

---

## 🚧 Roadmap / Ideas

* Automatic recipe rotation
* Scheduled mode switching
* Mobile-optimized UI
* Git-based auto-update
* Multi-display support

---

## 📸 Screenshots

*Add screenshots here:*

* Admin UI
* Menu display
* Recipe display

---

## 🧑‍💻 Author

Ryan Bolinger

---

## 📄 License

Currently unlicensed. Consider MIT or Apache 2.0 for public use.
