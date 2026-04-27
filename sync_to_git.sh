#!/bin/bash
set -e

PROJECT_DIR="/home/pi/inky-pi-project"

echo "Syncing Blackcap Pi files into Git repo..."

cd "$PROJECT_DIR"

# Main scripts
cp /home/pi/inky_menu.py "$PROJECT_DIR/"
cp /home/pi/render_recipe_mode.py "$PROJECT_DIR/"

# Admin app
mkdir -p "$PROJECT_DIR/inky_admin/templates"
mkdir -p "$PROJECT_DIR/inky_admin/static"

cp /home/pi/inky_admin/inky_admin_app.py "$PROJECT_DIR/inky_admin/"
cp /home/pi/inky_admin/templates/index.html "$PROJECT_DIR/inky_admin/templates/"

# Static files: CSS, icons, images, etc.
cp -r /home/pi/inky_admin/static/. "$PROJECT_DIR/inky_admin/static/"

# Optional: config example only, never live config
if [ -f /home/pi/inky_menu_config.ini.example ]; then
  cp /home/pi/inky_menu_config.ini.example "$PROJECT_DIR/"
fi

echo "Checking git status..."
git status

echo "Adding changes..."
git add .

echo "Committing changes..."
if git diff --cached --quiet; then
  echo "No changes to commit."
else
  git commit -m "Sync latest Blackcap Pi updates"
  git push
fi

echo "Pushing to GitHub..."
git push

echo "Done."
