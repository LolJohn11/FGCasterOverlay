import sys
import json
import re
import time
import requests
import keyring
from pathlib import Path
from dotenv import load_dotenv
import os

# Config

KEYRING_SERVICE = "FGCasterOverlay"

API_URL     = "https://api.start.gg/gql/alpha"
API_KEY_VAR = "START_API"
DATA_KEY    = "keyStartgg"
LINK_KEY    = "linkStartgg"
PER_PAGE    = 500

ENV_FILE    = Path(__file__).resolve().parent.parent.parent / "dev.env"
DATA_FILE   = Path(__file__).resolve().parent.parent.parent / "data.json"

def die(msg: str):
    """Print an error message and exit, ensuring output is captured in frozen mode."""
    print(msg)
    sys.exit(1)

def _read_data_json() -> dict:
    """Load data.json from the app root, returning {} on any failure."""
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def load_api_key() -> str:
    """Return the Start.gg API key, checking keyring → data.json → dev.env."""
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

    die(
        "[ERROR] Start.gg API key not found.\n"
        "  Set it in the Controller UI (Brackets panel).\n"
    )

def parse_url(raw: str) -> dict:
    """
    Parse a start.gg URL and return a dict with:
      - mode:           "event" | "phase" | "phase_group"
      - slug:           "tournament/<t>/event/<e>"
      - phase_id:       int or None
      - phase_group_id: int or None

    Accepted formats:
      .../tournament/<t>/event/<e>
      .../tournament/<t>/event/<e>/brackets/<phaseId>
      .../tournament/<t>/event/<e>/brackets/<phaseId>/<phaseGroupId>
    """
    cleaned = re.sub(r'^https?://(?:www\.)?start\.gg/', '', raw).strip('/')

    # Phase group: .../brackets/<phaseId>/<phaseGroupId>
    m = re.search(
        r'(tournament/[^/]+/event/[^/]+)/brackets/(\d+)/(\d+)', cleaned
    )
    if m:
        return {
            "mode":           "phase_group",
            "slug":           m.group(1),
            "phase_id":       int(m.group(2)),
            "phase_group_id": int(m.group(3)),
        }

    # Phase: .../brackets/<phaseId>
    m = re.search(
        r'(tournament/[^/]+/event/[^/]+)/brackets/(\d+)', cleaned
    )
    if m:
        return {
            "mode":           "phase",
            "slug":           m.group(1),
            "phase_id":       int(m.group(2)),
            "phase_group_id": None,
        }

    # Event: .../tournament/<t>/event/<e>
    m = re.search(r'(tournament/[^/]+/event/[^/?#]+)', cleaned)
    if m:
        return {
            "mode":           "event",
            "slug":           m.group(1),
            "phase_id":       None,
            "phase_group_id": None,
        }

    die(
        f"[ERROR] Could not parse a valid start.gg event URL from: '{raw}'\n"
        "  Make sure the link starts with https://www.start.gg/tournament/"
    )

def load_event_link() -> str:
    """Return the Start.gg event link from data.json, or '' if not set."""
    data = _read_data_json()
    return (data.get("bracket") or {}).get(LINK_KEY, "").strip()

def gql_request(api_key: str, query: str, variables: dict) -> dict:
    """POST a GraphQL query with retry on rate-limit (429)."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type":  "application/json",
    }
    for attempt in range(3):
        try:
            response = requests.post(
                API_URL,
                json={"query": query, "variables": variables},
                headers=headers,
                timeout=20,
            )
        except requests.exceptions.ConnectionError as e:
            die(f"[ERROR] Connection failed: {e}")
        except requests.exceptions.Timeout:
            die("[ERROR] Request timed out after 20 s.")

        if response.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"[WARN] Rate limited — waiting {wait}s before retry...")
            time.sleep(wait)
            continue
        if response.status_code == 401:
            die(
                "[ERROR] 401 Unauthorized — API token is invalid or expired."
            )
        if not response.ok:
            die(f"[ERROR] HTTP {response.status_code}:\n{response.text}")

        data = response.json()
        if "errors" in data:
            messages = [e.get("message", str(e)) for e in data["errors"]]
            die("[ERROR] GraphQL error(s):\n  " + "\n  ".join(messages))

        return data

    die("[ERROR] Request failed after 3 attempts (rate limit).")

# Queries

GET_EVENT_QUERY = """
query getEventId($slug: String) {
  event(slug: $slug) {
    id
    name
  }
}
"""

GET_ENTRANTS_QUERY = """
query EventEntrants($eventId: ID!, $page: Int!, $perPage: Int!) {
  event(id: $eventId) {
    id
    name
    entrants(query: {page: $page, perPage: $perPage}) {
      pageInfo {
        total
        totalPages
      }
      nodes {
        id
        participants {
          id
          prefix
          gamerTag
        }
      }
    }
  }
}
"""

GET_PHASE_SETS_QUERY = """
query PhaseSets($phaseId: ID!, $page: Int!, $perPage: Int!) {
  phase(id: $phaseId) {
    id
    name
    sets(page: $page, perPage: $perPage, sortType: STANDARD) {
      pageInfo {
        total
        totalPages
      }
      nodes {
        id
        slots {
          entrant {
            id
            name
          }
        }
      }
    }
  }
}
"""

GET_PHASE_NAME_QUERY = """
query PhaseName($phaseId: ID!) {
  phase(id: $phaseId) {
    name
  }
}
"""

GET_PHASE_GROUP_SETS_QUERY = """
query PhaseGroupSets($phaseGroupId: ID!, $page: Int!, $perPage: Int!) {
  phaseGroup(id: $phaseGroupId) {
    id
    displayIdentifier
    sets(page: $page, perPage: $perPage, sortType: STANDARD) {
      pageInfo {
        total
        totalPages
      }
      nodes {
        id
        slots {
          entrant {
            id
            name
          }
        }
      }
    }
  }
}
"""

# Data fetchers

def get_event(slug: str, api_key: str) -> tuple[int, str]:
    """Resolve slug → (event_id, event_name)."""
    #print(f"[INFO] Fetching event: {slug}")
    data  = gql_request(api_key, GET_EVENT_QUERY, {"slug": slug})
    event = data.get("data", {}).get("event")
    if not event:
        die(
            f"[ERROR] No event found for '{slug}'.\n"
            "  • Check the URL — the tournament or event name may be wrong."
        )
    print(f"[INFO] Event found! (ID: {event['id']})")
    return event["id"], event["name"]

def format_entrant_dict(prefix: str | None, gamer_tag: str | None) -> dict:
    return {
        "tag":  (prefix    or "").strip(),
        "name": (gamer_tag or "").strip(),
    }

def get_event_entrants(event_id: int, api_key: str) -> list[dict]:
    """Paginate through all event entrants and return {tag, name} dicts."""
    participants = []
    page         = 1
    total_pages  = None

    while True:
        #print(f"[INFO] Fetching participants  — page {page}"
        #      + (f"/{total_pages}" if total_pages else "") + "...")
        data = gql_request(
            api_key, GET_ENTRANTS_QUERY,
            {"eventId": event_id, "page": page, "perPage": PER_PAGE},
        )
        entrants_data = data["data"]["event"]["entrants"]

        if total_pages is None:
            pi          = entrants_data["pageInfo"]
            total_pages = pi["totalPages"]
            #print(f"[INFO] Total entrants : {pi['total']} across {total_pages} page(s)")

        for entrant in entrants_data["nodes"]:
            for p in entrant["participants"]:
                entry = format_entrant_dict(p.get("prefix"), p.get("gamerTag"))
                if entry["name"]:
                    participants.append(entry)

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.75)

    return participants

def _extract_players_from_sets(sets_nodes: list) -> dict[int, dict]:
    """
    Walk set slots and collect unique entrants as {entrant_id: {tag, name}}.
    The entrant.name from sets is a pre-merged string ("TAG | GamerTag" or
    just "GamerTag"), so we split on " | " to separate them.
    Skips slots where entrant is None.
    """
    seen = {}
    for node in sets_nodes:
        for slot in node.get("slots", []):
            entrant = slot.get("entrant")
            if entrant and entrant.get("id") and entrant.get("name"):
                parts = entrant["name"].split(" | ", 1)
                if len(parts) == 2:
                    tag, name = parts[0].strip(), parts[1].strip()
                else:
                    tag, name = "", parts[0].strip()
                seen[entrant["id"]] = {"tag": tag, "name": name}
    return seen

def get_phase_players(phase_id: int, api_key: str) -> tuple[list[dict], str]:
    """Paginate through all sets in a phase and return (participants, phase_name)."""
    seen        = {}
    page        = 1
    total_pages = None
    phase_name  = ""

    while True:
        data = gql_request(
            api_key, GET_PHASE_SETS_QUERY,
            {"phaseId": phase_id, "page": page, "perPage": PER_PAGE},
        )
        phase_data = data["data"]["phase"]
        sets_data  = phase_data["sets"]

        if total_pages is None:
            phase_name  = phase_data.get("name") or ""
            pi          = sets_data["pageInfo"]
            total_pages = pi.get("totalPages") or 1

        seen.update(_extract_players_from_sets(sets_data["nodes"]))

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.75)

    return list(seen.values()), phase_name

def get_phase_group_players(phase_group_id: int, api_key: str) -> list[dict]:
    """Paginate through all sets in a phase group and return unique {tag, name} dicts."""
    seen        = {}
    page        = 1
    total_pages = None

    while True:
        #print(f"[INFO] Fetching phase group sets — page {page}"
        #      + (f"/{total_pages}" if total_pages else "") + "...")
        data = gql_request(
            api_key, GET_PHASE_GROUP_SETS_QUERY,
            {"phaseGroupId": phase_group_id, "page": page, "perPage": PER_PAGE},
        )
        sets_data = data["data"]["phaseGroup"]["sets"]

        if total_pages is None:
            pi          = sets_data["pageInfo"]
            total_pages = pi.get("totalPages") or 1
            #print(f"[INFO] Total sets : {pi['total']} across {total_pages} page(s)")

        seen.update(_extract_players_from_sets(sets_data["nodes"]))

        if page >= total_pages:
            break
        page += 1
        time.sleep(0.75)

    return list(seen.values())

def get_phase_name(phase_id: int, api_key: str) -> str:
    """Fetch just the phase name for a given phase ID."""
    data = gql_request(api_key, GET_PHASE_NAME_QUERY, {"phaseId": phase_id})
    return (data.get("data", {}).get("phase") or {}).get("name") or ""

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
            die(
                "[ERROR] No Start.gg event link found.\n"
                "  • Set it in the Controller UI (Brackets panel)."
            )

    api_key   = load_api_key()
    url_parts = parse_url(raw_input)
    mode      = url_parts["mode"]
    slug      = url_parts["slug"]

    #print(f"[INFO] Mode : {mode.replace('_', ' ')}")

    event_id, event_name = get_event(slug, api_key)
    print()

    phase_name = ""

    if mode == "event":
        participants = get_event_entrants(event_id, api_key)
    elif mode == "phase":
        participants, phase_name = get_phase_players(url_parts["phase_id"], api_key)
    else:  # phase_group
        participants = get_phase_group_players(url_parts["phase_group_id"], api_key)
        phase_name   = get_phase_name(url_parts["phase_id"], api_key)

    folder_name = f"{event_name} - {phase_name}" if phase_name else event_name

    # Create profiles
    print()
    create_player_profiles(folder_name, participants)

    #print(f"\n  Event        : {event_name}")
    #print(f"  Mode         : {mode.replace('_', ' ')}")
    #print(f"  Participants : {len(participants)}")
    #for p in participants[:10]:
    #    tag_str = f"[{p['tag']}] " if p["tag"] else ""
    #    print(f"    - {tag_str}{p['name']}")
    #if len(participants) > 10:
    #    print(f"    ... and {len(participants) - 10} more")

if __name__ == "__main__":
    main()
