async function getCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function cleanTitle(title) {
  if (!title) return "";

  let cleaned = title.trim();

  // Remove site suffixes like:
  // "Cheesy Asparagus Orzotto Recipe - Delish"
  // "Something | Food Network"
  cleaned = cleaned.replace(/\s*[-|–]\s*.*$/, "").trim();

  // Remove common trailing recipe wording.
  cleaned = cleaned.replace(/\bRecipe\b/gi, "").trim();

  // Normalize spacing.
  cleaned = cleaned.replace(/\s+/g, " ");

  return cleaned;
}

function extractSiteName(url) {
  try {
    const hostname = new URL(url).hostname.replace(/^www\./, "");
    const domain = hostname.split(".")[0];

    const knownNames = {
      delish: "Delish",
      foodnetwork: "Food Network",
      thepioneerwoman: "The Pioneer Woman",
      allrecipes: "Allrecipes",
      eatingwell: "EatingWell",
      seriouseats: "Serious Eats",
      simplyrecipes: "Simply Recipes",
      bonappetit: "Bon Appétit",
      foodandwine: "Food & Wine"
    };

    if (knownNames[domain]) return knownNames[domain];

    return domain
      .split("-")
      .map(part => part.charAt(0).toUpperCase() + part.slice(1))
      .join(" ");
  } catch {
    return "";
  }
}

function buildDescription(name, siteName) {
  if (name && siteName) return `${name} from ${siteName}`;
  if (name) return name;
  if (siteName) return `Recipe from ${siteName}`;
  return "";
}

async function loadDefaults() {
  const saved = await chrome.storage.sync.get(["apiBase"]);

  if (saved.apiBase) {
    document.getElementById("apiBase").value = saved.apiBase;
  }

  const tab = await getCurrentTab();

  const cleanName = cleanTitle(tab.title);
  const siteName = extractSiteName(tab.url);

  document.getElementById("name").value = cleanName;
  document.getElementById("description").value = buildDescription(cleanName, siteName);
}

async function sendRecipe() {
  const status = document.getElementById("status");
  const button = document.getElementById("sendBtn");

  status.className = "status";
  status.textContent = "Sending...";
  button.disabled = true;

  try {
    const tab = await getCurrentTab();

    const apiBase = document.getElementById("apiBase").value.trim().replace(/\/$/, "");
    await chrome.storage.sync.set({ apiBase });

    const payload = {
      name: document.getElementById("name").value.trim(),
      description: document.getElementById("description").value.trim(),
      url: tab.url,
      source: "web",
      file_type: "html",
      layout: document.getElementById("layout").value,
      recipe_type: document.getElementById("recipeType").value,
      select_after_add: document.getElementById("selectAfterAdd").checked,
      refresh_cache: true,
      cache_async: document.getElementById("cacheAsync").checked
    };

    if (!payload.name) {
      throw new Error("Recipe name is required.");
    }

    if (!payload.url) {
      throw new Error("Recipe URL is required.");
    }

    const response = await fetch(`${apiBase}/api/recipes/add`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify(payload)
    });

    const data = await response.json().catch(() => ({}));

    if (!response.ok || !data.ok) {
      throw new Error(data.error || data.message || "Recipe send failed.");
    }

    status.className = "status ok";

    if (data.cache_queued) {
      status.textContent = "Recipe saved. Cache build started.";
    } else if (data.cache_ok === true) {
      status.textContent = "Recipe saved and cached.";
    } else {
      status.textContent = "Recipe saved.";
    }
  } catch (err) {
    console.error(err);
    status.className = "status error";
    status.textContent = `Error: ${err.message}`;
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", loadDefaults);
document.getElementById("sendBtn").addEventListener("click", sendRecipe);