import sys
import json
import re
import requests
import keyring
from pathlib import Path
from dotenv import load_dotenv
import os

# Config

KEYRING_SERVICE = "FGCasterOverlay"

API_BASE    = "https://api.challonge.com/v1"
API_KEY_VAR = "CHAL_API1"
DATA_KEY    = "keyChallonge"
LINK_KEY    = "linkChallonge"

ENV_FILE    = Path(__file__).resolve().parent.parent.parent / "dev.env"
DATA_FILE   = Path(__file__).resolve().parent.parent.parent / "data.json"

# Spoof a browser User-Agent to avoid Cloudflare bot detection
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

def _read_data_json() -> dict:
    """Load data.json from the app root, returning {} on any failure."""
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def load_api_key() -> str:
    """Return the Challonge API key, checking keyring → data.json → dev.env."""
    # 1) OS keyring
    try:
        api_key = (keyring.get_password(KEYRING_SERVICE, API_KEY_VAR) or "").strip()
        if api_key:
            return api_key
    except Exception:
        pass

    # 2) data.json
    data = _read_data_json()
    api_key = (data.get("bracket") or {}).get(DATA_KEY, "").strip()
    if api_key:
        return api_key

    # 3) dev.env
    if ENV_FILE.exists():
        load_dotenv(dotenv_path=ENV_FILE)
        api_key = os.getenv(API_KEY_VAR, "").strip()
        if api_key:
            return api_key

    sys.exit(
        "[ERROR] Challonge API key not found.\n"
        "  Set it in the Controller UI (Brackets panel).\n"
    )

def load_event_link() -> str:
    """Return the Challonge event link from data.json, or '' if not set."""
    data = _read_data_json()
    return (data.get("bracket") or {}).get(LINK_KEY, "").strip()

def extract_tournament_id(raw: str) -> str:
    """
    Convert any Challonge URL or bare slug into the ID the v1 API expects.

    https://challonge.com/y6asfcat        ->  y6asfcat
    https://foo.challonge.com/mytourney  ->  foo-mytourney
    challonge.com/org/mytourney          ->  org-mytourney
    y6asfcat  /  123456                  ->  (unchanged)
    """
    cleaned = re.sub(r'^https?://', '', raw).rstrip('/')

    # Subdomain URL: foo.challonge.com/slug
    m = re.match(r'([^.]+)\.challonge\.com/([^/?#]+)', cleaned)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # Standard URL: (www.)challonge.com/[org/]slug
    m = re.match(r'(?:www\.)?challonge\.com/(?:([^/?#]+)/)?([^/?#]+)', cleaned)
    if m:
        org, slug = m.group(1), m.group(2)
        return f"{org}-{slug}" if org else slug

    return raw  # bare slug or numeric ID

def api_get(url: str, api_key: str, context: str) -> requests.Response:
    """Shared GET request with error handling."""
    try:
        response = requests.get(
            url,
            params={"api_key": api_key},
            headers=HEADERS,
            timeout=15,
        )
    except requests.exceptions.ConnectionError as e:
        sys.exit(f"[ERROR] Connection failed ({context}): {e}")
    except requests.exceptions.Timeout:
        sys.exit(f"[ERROR] Request timed out ({context}).")

    #print(f"[INFO] HTTP status   : {response.status_code}")

    if response.status_code == 401:
        sys.exit(
            "[ERROR] 401 Unauthorized — API key is invalid."
        )
    if response.status_code == 404:
        sys.exit(
            f"[ERROR] 404 Not Found ({context}).\n"
            "  • Double-check the tournament URL or slug.\n"
            "  • Private tournaments require the key of the owning account."
        )
    if response.status_code == 520:
        sys.exit(
            "[ERROR] 520 — Cloudflare blocked the request.\n"
            "  • Try disabling any VPN or proxy and run again."
        )
    if not response.ok:
        sys.exit(f"[ERROR] Unexpected HTTP {response.status_code} ({context}):\n{response.text}")

    return response

def get_tournament(tournament_id: str, api_key: str) -> dict:
    """GET /v1/tournaments/{id}.json"""
    url = f"{API_BASE}/tournaments/{tournament_id}.json"
    #print(f"[INFO] Event found: {tournament_id}")
    print(f"[INFO] Event found! (ID: {tournament_id})")
    #print(f"[INFO] Endpoint              : {url}")
    return api_get(url, api_key, "tournament").json()

def get_participants(tournament_id: str, api_key: str) -> list[dict]:
    """GET /v1/tournaments/{id}/participants.json — returns {tag, name} dicts."""
    url = f"{API_BASE}/tournaments/{tournament_id}/participants.json"
    #print(f"[INFO] Fetching participants : {tournament_id}")
    #print(f"[INFO] Fetching participants...")
    #print(f"[INFO] Endpoint              : {url}")
    raw = api_get(url, api_key, "participants").json()
    result = []
    for entry in raw:
        if "participant" not in entry:
            continue
        # Challonge has no separate tag field — split "TAG | Name" if present
        full_name = (entry["participant"].get("name") or "").strip()
        parts     = full_name.split(" | ", 1)
        if len(parts) == 2:
            tag, name = parts[0].strip(), parts[1].strip()
        else:
            tag, name = "", parts[0].strip()
        result.append({"tag": tag, "name": name})
    return result

def sanitize_filename(name: str) -> str:
    """Strip characters that are illegal in filenames."""
    return re.sub(r'[\\/:*?"<>|]', '', name).strip()

def create_player_profiles(event_name: str, participants: list[dict]) -> None:
    """Create a folder for the event and a profile JSON for each participant."""
    profiles_root = DATA_FILE.parent / "profiles" / "players"
    folder_name   = sanitize_filename(event_name) or "Unknown Event"
    event_dir     = profiles_root / folder_name
    event_dir.mkdir(parents=True, exist_ok=True)
    #print(f"[INFO] Saving profiles to '{event_dir}'")

    saved = 0
    for p in participants:
        player_name = p.get("name", "").strip()
        if not player_name:
            continue
        profile = {
            "name":      player_name,
            "clan":      p.get("tag", "").strip(),
            "id":        "",
            "img":       "",
            "characters": "",
        }
        filename = sanitize_filename(player_name) + ".json"
        out_path = event_dir / filename
        out_path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        saved += 1

    print(f"[OK]   Created {saved} profile(s) in '{folder_name}'")

def main():
    # Event link: prefer data.json, fall back to sys.argv for CLI use
    raw_input = load_event_link()
    if not raw_input:
        if len(sys.argv) >= 2:
            raw_input = sys.argv[1]
        else:
            sys.exit(
                "[ERROR] No Challonge event link found.\n"
                "  • Set it in the Controller UI (Brackets panel)."
            )

    api_key       = load_api_key()
    tournament_id = extract_tournament_id(raw_input)

    # Fetch data
    tournament_data = get_tournament(tournament_id, api_key)
    t = tournament_data.get("tournament", {})

    print()
    participants = get_participants(tournament_id, api_key)
    event_name   = t.get("name", "") or tournament_id

    # Create profiles
    print()
    create_player_profiles(event_name, participants)

    #print(f"\n  Name         : {event_name}")
    #print(f"  Participants : {len(participants)}")
    #for p in participants:
    #    tag_str = f"[{p['tag']}] " if p["tag"] else ""
    #    print(f"    - {tag_str}{p['name']}")

if __name__ == "__main__":
    main()