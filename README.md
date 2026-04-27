# 🫐 Blackcap Pi

A purpose-built Raspberry Pi + e-ink display system for beautifully simple, distraction-free content.

Blackcap Pi is designed to render **recipes** and **daily menus** in a clean, readable format—perfect for kitchens, family hubs, or anywhere you want useful information without screens screaming for attention.

---

## ✨ Features

* 🖥️ Optimized for Waveshare e-ink displays
* 🍽️ Dedicated **Recipe Mode** (clean, readable layouts)
* 📅 Automated **Menu Mode** (set it and forget it)
* 🌐 Chrome extension for one-click recipe capture
* ⚡ **Background recipe caching (default)**
* 🧠 Smart parsing (JSON-LD → fallback scraping → rendering)
* 🔄 Easy switching between modes
* 🛠️ Lightweight Admin UI (no bloat, just control)

---

## 🧰 Tech Stack

* Python (Flask-based admin + services)
* Beautiful Soup (HTML parsing)
* Playwright (for stubborn JS-heavy sites)
* PIL / Pillow (image processing)
* Raspberry Pi (Zero 2 W works great)
* Waveshare 13.3" e-ink display

---

## 📦 Project Structure

<details>
<summary>Click to expand</summary>

```bash
Blackcap-Pi/
├── Blackcap-Pi-Extension/      # 🌐 Chrome extension
├── inky_admin/                 # 🛠 Admin UI
│   └── inky_admin_app.py       #   └── Flask server
├── inky_menu.py                # 📅 Menu rendering logic
├── render_recipe_mode.py       # 🍽️ Recipe display renderer
├── config.ini                  # ⚙️ Configuration
├── inky_env/                   # 🐍 Python virtual environment (local)
└── README.md                   # 📖 Project documentation
```

</details>

---

## 🚀 Getting Started

### 1. Clone the Repo

git clone https://github.com/<your-repo>/Blackcap-Pi.git
cd Blackcap-Pi

---

### 2. 🐍 Create the Python Environment

Blackcap Pi expects a dedicated virtual environment at:

/home/pi/inky_env

Create it:

python3 -m venv /home/pi/inky_env

Activate it:

source /home/pi/inky_env/bin/activate

Install dependencies:

pip install -r requirements.txt

---

### 3. ⚙️ Configure

Edit:

config.ini

Set things like:

* Menu source URL
* Display preferences
* API settings

---

### 4. ▶️ Run the Admin UI

/home/pi/inky_env/bin/python3 inky_admin/inky_admin_app.py

Open:

http://<raspberry-pi-ip>:8080

---

## 🔌 Chrome Extension (Recipe Capture)

Because copying recipes manually is a crime.

---

### 📦 Location

Blackcap-Pi-Extension/

---

### 🛠 Install (Developer Mode)

1. Go to:
   chrome://extensions/

2. Enable **Developer Mode**

3. Click **Load unpacked**

4. Select:
   Blackcap-Pi/Blackcap-Pi-Extension

---

### ⚙️ Configure

Click the extension and set:

http://<raspberry-pi-ip>:8080

(Or your HTTPS endpoint if you’ve secured it 🔒)

---

## ⚡ How Recipe Capture Works

### 🧠 Smart Extraction

When you click the extension:

* **Name** → Page title
* **Description** → <Recipe Title> from <Site Name>
* **Source** → URL

---

### ⚡ Default Behavior: Background Caching

👉 This is important:

When you hit **Send to Blackcap Pi**:

* The recipe is fetched
* Parsed
* Images extracted
* Stored locally

🧊 **It does NOT immediately render to the display**

---

### 🎯 Why?

* Faster later rendering ⚡
* Works offline 📴
* Avoids re-scraping sites 🌐
* Keeps display transitions intentional

---

### 🖥️ To Show It

1. Open Admin UI
2. Select recipe
3. Click:
   Render Recipe

Boom. Kitchen-ready.

---

## 🔄 Display Modes

### 📅 Menu Mode (Default)

* Passive display
* Auto-updating
* Great for school menus / schedules

---

### 🍽️ Recipe Mode

* Clean, high-contrast recipe layout
* Built for actual cooking (not scrolling)

---

### 🔁 Switching Modes

In Admin UI:

* Select recipe → Render Recipe
* Exit → Back to Menu

---

## 🛠 Admin UI

http://<raspberry-pi-ip>:8080

From here you can:

* 📚 View cached recipes
* 🍽️ Render recipes
* 🔄 Switch modes
* ⚙️ Adjust settings
* 👀 Monitor system

---

## 🧠 Parsing Strategy (Under the Hood)

Blackcap Pi tries multiple approaches:

1. JSON-LD (cleanest)
2. Beautiful Soup scraping
3. Playwright fallback (for JS-heavy sites)
4. Image extraction + caching

Basically: it tries really hard to make messy websites usable.

---

## 📱 Mobile Control (Next Up)

Coming soon:

* Tap-to-render recipes
* Toggle modes
* Minimal mobile UI (no app install needed)

---

## 🖨 Hardware

* Raspberry Pi Zero 2 W
* Waveshare 13.3" e-ink
* Custom 3D-printed case (Instructables coming 👀)

---

## 🚧 Roadmap

* 📱 Mobile UI
* 🔐 Authentication
* ☁️ Secure remote access
* 🔄 Scheduled recipe rotation
* 🎨 Color display version (Blackcap Pi Spectrum?)

---

## 💡 Philosophy

Blackcap Pi is built to be:

* Calm
* Focused
* Useful
* Invisible when it should be

No notifications. No distractions. Just the right information at the right time.

---

## 🙌 Contributions

Ideas, tweaks, improvements — all welcome.
