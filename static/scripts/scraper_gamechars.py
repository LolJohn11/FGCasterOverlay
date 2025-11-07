#print("Initializing character fetching...")

import json
import re
from pathlib import Path
from typing import List
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page

BASE_URL = "https://www.fightersgeneration.com/"

# --- minimal logging for parent process to parse ---
def info(msg: str):    print(f"[info] {msg}")
def warn(msg: str):    print(f"[warn] {msg}")
def error(msg: str):   print(f"[error] {msg}")
def success(msg: str): print(f"[ok] {msg}")

def fetch_html_fast(url: str) -> str:
    resp = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.text


def extract_characters_from_game_page(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")

    # find the TD that has an underlined "Characters"
    label_td = None
    for td in soup.find_all("td"):
        text = td.get_text(" ", strip=True)
        if not text:
            continue
        if "characters" in text.lower() and "related games" not in text.lower():
            if td.find("u", string=lambda s: s and s.strip().lower() == "characters"):
                label_td = td
                break

    if not label_td:
        raise RuntimeError("Could not find the 'Characters:' block on the game page.")

    list_td = label_td.find_next_sibling("td")
    if not list_td:
        raise RuntimeError("Found 'Characters:' label but no sibling TD with the list.")

    names = []
    for a in list_td.find_all("a", href=True):
        raw = a.get_text(" ", strip=True)
        if raw:
            names.append(" ".join(raw.split()))  # collapse newlines/extra spaces

    return sorted(set(names), key=lambda s: s.lower())


def get_html_playwright(url: str) -> str:
    # fallback only when requests fails
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(java_script_enabled=False)

        def block_media(route, request):
            if request.resource_type in ("image", "media", "font"):
                return route.abort()
            u = request.url.lower()
            if u.endswith(
                (
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".gif",
                    ".webp",
                    ".svg",
                    ".mp4",
                    ".webm",
                    ".mov",
                    ".avi",
                    ".mkv",
                    ".mp3",
                    ".wav",
                    ".ogg",
                    ".woff",
                    ".woff2",
                    ".ttf",
                )
            ):
                return route.abort()
            return route.continue_()

        context.route("**/*", block_media)
        page: Page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        html = page.content()
        browser.close()
    return html


def load_game_slug(path: str = "gamename.json") -> str:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    slug = (
        data.get("game")
        or data.get("slug")
        or data.get("name")  # allow a bit of flexibility
    )
    if not isinstance(slug, str) or not slug.strip():
        raise RuntimeError("gamename.json must contain a non-empty 'game' slug (e.g. {'game':'tekken8'}).")
    return slug.strip()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Scrape FightersGeneration characters from a specific game page")
    parser.add_argument("--gamename-json", default="gamename.json", help="JSON file with {'game': '<slug>'}")
    parser.add_argument("--output-dir", default="characters", help="Directory to write output JSON")
    parser.add_argument(
        "--use-local-html",
        action="store_true",
        help="Use website_gamepage.html from current directory instead of live site (for testing)",
    )
    args = parser.parse_args()

    slug = load_game_slug(args.gamename_json)
    game_url = f"{BASE_URL}games/{slug}.html"
    info(f"Fetching game page...")

    # fetch HTML
    if args.use_local_html and Path("website_gamepage.html").exists():
        html = Path("website_gamepage.html").read_text(encoding="utf-8")
    else:
        try:
            html = fetch_html_fast(game_url)
        except Exception:
            html = get_html_playwright(game_url)

    characters = extract_characters_from_game_page(html)

    # write output
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"characters_{slug}.json"
    payload = {"game": slug, "characters": characters}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    info(f"[bold green]Successfully fetched[/bold green] [bold cyan]{len(characters)}[/bold cyan] [bold green]characters.[/bold green]")


if __name__ == "__main__":
    main()
