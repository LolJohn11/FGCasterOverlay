from flask import Flask, render_template, request, jsonify, url_for, abort, send_from_directory, send_file, g
from flask_socketio import SocketIO, emit
from pathlib import Path
import json, os, sys, time, logging, re, click, threading, subprocess, runpy

# ---------- Rich logging setup ----------
from rich.logging import RichHandler
from rich.highlighter import NullHighlighter
from rich.console import Console
from rich.table import Table
from rich.traceback import install as rich_traceback
from werkzeug.serving import WSGIRequestHandler

# Toggle for verbose request logging
ENABLE_REQUEST_LOGGING = False  # Set to True to show GET/POST logs

class QuietRequestHandler(WSGIRequestHandler):
    def log(self, type, message, *args):         # noqa: N802
        pass
    def log_request(self, *args, **kwargs):      # noqa: N802
        pass
    def log_error(self, *args, **kwargs):        # noqa: N802
        # Or pass to drop errors too
        super().log_error(*args, **kwargs)

DATA_LOCK = threading.Lock()
SCRAPER_LOCK = threading.Lock()

CHAR_SCRAPER_STATE = {"running": False, "slug": "", "started_at": 0.0}
CHARLIST_LOCK = threading.Lock()

def _search_bases():
    """Prefer the EXE folder for external assets; fall back to _MEIPASS; dev uses script folder."""
    exe_dir   = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    meipass   = getattr(sys, "_MEIPASS", None)
    bases = [exe_dir]
    if meipass and meipass not in bases:
        bases.append(meipass)
    return bases

def resource_path(*parts):
    # return the first existing candidate
    for base in _search_bases():
        p = os.path.join(base, *parts)
        if os.path.exists(p) or parts[-1] == 'data.json':  # allow creating data.json
            return p
    # fallback: use the EXE/script directory even if not present yet
    return os.path.join(_search_bases()[0], *parts)

IS_FROZEN = getattr(sys, "frozen", False)
ASYNC_MODE = 'threading' if IS_FROZEN else None  # None = auto (dev), threading in EXE

TEMPLATES_ROOT = resource_path("templates")
STATIC_ROOT    = resource_path("static")
DATA_FILE      = resource_path("data.json")
GAME_TAG_RE = re.compile(r'id=["\']overlayGame["\']\s+value=["\']([^"\']+)["\']', re.IGNORECASE)

rich_traceback(show_locals=False, width=220)
console = Console(highlight=False)

logging.getLogger('werkzeug').setLevel(logging.ERROR)
click.echo = lambda *args, **kwargs: None

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(
        rich_tracebacks=True,
        markup=True,
        highlighter=NullHighlighter(),  # disable automatic number highlighting
        show_path=False                 # hide "controller.py:118" suffix
    )]
)
log = logging.getLogger("overlay")

# Silence werkzeug access logs entirely (we print our own pretty access logs)
werk = logging.getLogger("werkzeug")
werk.handlers.clear()
werk.propagate = False
werk.disabled = True

# Silence python-socketio / engineio debug noise
app = Flask(__name__, template_folder=TEMPLATES_ROOT, static_folder=STATIC_ROOT)
socketio = SocketIO(app, async_mode=ASYNC_MODE, logger=False, engineio_logger=False)

# ---- logging ----
def _summarize_payload(data: dict) -> str:
    """incoming payload info"""
    if not isinstance(data, dict):
        return "[dim]Payload not a dict[/dim]"
    p1 = data.get("player1", {}) or {}
    p2 = data.get("player2", {}) or {}
    t1 = data.get("team1", {}) or {}
    t2 = data.get("team2", {}) or {}
    stage = data.get("stage", "")
    mtype = data.get("match_type", "")
    top = data.get("toptext", "")
    parts = []
    if p1 or p2:
        parts.append(f"[yellow]Players: [/yellow][b]{(p1.get('name') or 'None')}[/b] {p1.get('score',0)} vs [b]{(p2.get('name') or 'None')}[/b] {p2.get('score',0)}")
    if t1 or t2:
        parts.append(f"[yellow]Teams:[/yellow] [b]{(t1.get('name') or 'None')}[/b] {t1.get('score',0)} vs [b]{(t2.get('name') or 'None')}[/b] {t2.get('score',0)}")
    if stage:
        parts.append(f"[yellow]Event Stage: [/yellow][b]{stage}[/b]")
    if mtype:
        parts.append(f"[yellow]Match Type: [/yellow][b]{mtype}[/b]")
    if top:
        parts.append(f"[yellow]Top text: [/yellow][b]{top}[/b]")
    return " • ".join(parts) or "[dim]Empty scoreboard state[/dim]"

DATA_URL_RE = re.compile(
    r'^data:(?P<mime>[-\w.+/]+)(?:;name=(?P<name>[^;]+))?(?:;charset=[^;]+)?(?:;base64)?,',
    re.IGNORECASE
)

# Read the <input id="overlayGame" value="..."> from template.html
GAME_TAG_RE = re.compile(r'id=["\']overlayGame["\']\s+value=["\']([^"\']+)["\']', re.IGNORECASE)

def _extract_game_from_template(template_name: str) -> str | None:
    tpl_path = os.path.join(TEMPLATES_ROOT, template_name, "template.html")
    try:
        with open(tpl_path, "r", encoding="utf-8") as f:
            head = f.read(4096)
        m = GAME_TAG_RE.search(head)
        if m:
            return (m.group(1) or "").strip()
    except Exception:
        pass
    return None

def _charlist_exists(slug: str) -> bool:
    if not slug:
        return False
    chars_path = Path(app.static_folder) / "characters" / f"characters_{slug}.json"
    return chars_path.exists()
    
def _charlist_path_for(slug: str) -> str:
    # UI loads from /static/characters/characters_{slug}.json
    return os.path.join(STATIC_ROOT, "characters", f"characters_{slug}.json")

def _charlist_exists(slug: str) -> bool:
    return os.path.isfile(_charlist_path_for(slug))
    
def _run_char_scraper_for_slug(slug: str):
    """
    Run /static/scripts/scraper_gamechars.py to produce /static/characters/characters_{slug}.json.
    The scraper reads a small gamename.json with {"game": "<slug>"}.
    """
    if not slug:
        log.warning("No game slug provided; skipping scraper.")
        return

    static_dir    = Path(app.static_folder)             # e.g. .../static
    scripts_dir   = static_dir / "scripts"
    chars_dir     = static_dir / "characters"
    script_path   = scripts_dir / "scraper_gamechars.py"
    out_path      = chars_dir / f"characters_{slug}.json"
    gamename_json = static_dir / "gamename.json"

    if not script_path.exists():
        log.error(f"Script not found: {script_path}")
        return

    chars_dir.mkdir(parents=True, exist_ok=True)
    try:
        gamename_json.write_text(
            json.dumps({"game": slug}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception as ex:
        log.error(f"Failed writing gamename.json: {ex}")
        return

    # avoid concurrent runs
    if not SCRAPER_LOCK.acquire(blocking=False):
        #log.warning("Scraper already running; skipping duplicate.")
        try:
            gamename_json.unlink(missing_ok=True)
        except Exception:
            pass
        return

    # mark running & notify clients
    CHAR_SCRAPER_STATE.update({"running": True, "slug": slug, "started_at": time.time()})
    try:
        socketio.emit("charlist_status", {"running": True, "slug": slug})
    except Exception:
        pass

    try:
        log.info(f"Downloading characters for '{slug}'")
        
        # Handle frozen vs non-frozen execution
        if IS_FROZEN:
            # When frozen, execute the scraper module directly in the same process
            # This avoids the infinite recursion issue
            import runpy
            import sys as sys_module
            import io
            from contextlib import redirect_stdout, redirect_stderr
            
            # Save original argv and cwd
            original_argv = sys_module.argv[:]
            original_cwd = os.getcwd()
            
            # Capture stdout/stderr
            captured_output = io.StringIO()
            
            try:
                # Set up argv as if we called it from command line
                sys_module.argv = [
                    str(script_path),
                    "--gamename-json", str(gamename_json)
                ]
                
                # Change to static dir for relative paths to work
                os.chdir(str(static_dir))
                
                # Run the scraper script with captured output
                #log.info(f"Running scraper in-process (frozen mode)")
                
                with redirect_stdout(captured_output), redirect_stderr(captured_output):
                    runpy.run_path(str(script_path), run_name="__main__")
                
                rc = 0  # Assume success if no exception
                
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (1 if e.code else 0)
            except Exception as e:
                log.error(f"Scraper execution failed: {e}")
                rc = 1
            finally:
                # Restore original state
                sys_module.argv = original_argv
                os.chdir(original_cwd)
                
                # Process captured output line by line with the same formatting logic
                output = captured_output.getvalue()
                for raw in output.split('\n'):
                    line = raw.rstrip().strip()
                    if not line:
                        continue

                    lower = line.lower().lstrip()
                    level = "INFO"
                    msg = line

                    for prefix, tag in (
                        ("[error]", "ERROR"),
                        ("[err]",   "ERROR"),
                        ("[warning]", "WARN"),
                        ("[warn]",   "WARN"),
                        ("[info]",   "INFO"),
                        ("[ok]",     "INFO"),
                    ):
                        if lower.startswith(prefix):
                            level = tag
                            msg = line[len(prefix):].lstrip()
                            break

                    if level == "ERROR":
                        log.error(msg)
                    elif level == "WARN":
                        log.warning(msg)
                    else:
                        log.info(msg)
        else:
            # Non-frozen: use subprocess as before
            cmd = [sys.executable, "-u", str(script_path), "--gamename-json", str(gamename_json)]
            proc = subprocess.Popen(
                cmd,
                cwd=str(static_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.rstrip("\n").strip()
                if not line:
                    continue

                lower = line.lower().lstrip()
                level = "INFO"
                msg = line

                for prefix, tag in (
                    ("[error]", "ERROR"),
                    ("[err]",   "ERROR"),
                    ("[warning]", "WARN"),
                    ("[warn]",   "WARN"),
                    ("[info]",   "INFO"),
                ):
                    if lower.startswith(prefix):
                        level = tag
                        msg = line[len(prefix):].lstrip()
                        break

                if level == "ERROR":
                    log.error(msg)
                elif level == "WARN":
                    log.warning(msg)
                else:
                    log.info(msg)

            rc = proc.wait()
        
        # Common exit code handling
        if rc != 0:
            log.error(f"Scraper exited with code {rc}")
        else:
            if out_path.exists():
                try:
                    payload = json.loads(out_path.read_text(encoding="utf-8"))
                    n = len(payload.get("characters", []))
                    #log.info(f"Successfully fetched {n} characters.")
                except Exception:
                    n = "?"
            else:
                log.warning(f"Scraper finished but {out_path.name} was not created.")
                
    except Exception as ex:
        log.error(f"Error while running scraper: {ex}")
    finally:
        # mark done
        CHAR_SCRAPER_STATE.update({"running": False})
        #log.info(f"[bold cyan]Scraper finished for '{slug}'[/bold cyan] - notifying clients...")
        
        time.sleep(0.2)
        
        payload = {"running": False, "slug": slug}
        #log.info(f"Payload prepared: {payload}")
        
        try:
            socketio.emit("charlist_status", payload, namespace='/')
            #log.info("Socket emission complete")
        except Exception as e:
            log.error(f"Failed to emit socket event: {e!r}")
        
        try:
            gamename_json.unlink(missing_ok=True)
        except Exception:
            pass

        SCRAPER_LOCK.release()

@app.route("/characters/status")
def characters_status():
    # Lightweight status so UI can poll once on load
    return jsonify({
        "running": bool(CHAR_SCRAPER_STATE.get("running")),
        "slug": CHAR_SCRAPER_STATE.get("slug") or "",
        "started_at": CHAR_SCRAPER_STATE.get("started_at") or 0.0,
    })

# --- preserve config keys when saving UI updates ---
PRESERVE_KEYS = ('port', 'active_template')
def save_data_preserving(update: dict, preserve_keys=PRESERVE_KEYS):
    current = load_data() or {}
    # hard-override preserved keys from current file, no matter what UI sent
    for k in preserve_keys:
        if k in current:
            update[k] = current[k]
    save_data(update)

def _short(v):
    """Make values log-friendly: empty → None, long paths → basename, trim long strings."""
    if v is None or v == "":
        return "[bright_black]None[/bright_black]"
    if isinstance(v, (int, float)):
        return v
    
    s = str(v)
    
    # Data URL?
    m = DATA_URL_RE.match(s)
    if m:
        name = m.group('name')
        if name:
            return name  # honor explicit filename if provided
        mime = (m.group('mime') or '').lower()
        # map common mimes to file extensions
        ext = {
            'image/png': 'png',
            'image/jpeg': 'jpg',
            'image/svg+xml': 'svg',
            'image/webp': 'webp',
            'image/gif': 'gif',
        }.get(mime, mime or '?')
        return f"Custom image ({ext})"

    # blob URLs etc.
    if s.startswith('blob:'):
        return "Custom image (blob)"

    # Regular path or URL: show basename
    if '/' in s or '\\' in s:
        base = os.path.basename(s.rstrip('/\\'))
        return base if base else s

    # Fallback: trim
    return s if len(s) <= 40 else s[:37] + "…"

def _fmt_change(label, old, new):
    if old == new:
        return ""
    return f"{label} [b]{_short(old)}[/b] → [bold]{_short(new)}[/bold]"

def _diff_section(title, prev: dict, curr: dict, fields):
    """Return a formatted change line for a dict section if any field changed."""
    if not isinstance(prev, dict): prev = {}
    if not isinstance(curr, dict): curr = {}
    parts = []
    for key, label in fields:
        ch = _fmt_change(f"{label}:", prev.get(key), curr.get(key))
        if ch: parts.append(ch)
    if not parts:
        return ""
    return f"[yellow]{title}[/yellow] " + ", ".join(parts)

def _diff_scalar(label, prev, curr):
    ch = _fmt_change(f"{label}:", prev, curr)
    return ch

def _fmt_img_change(label, prev_img, curr_img, prev_name=None, curr_name=None):
    """Image change with filename preference."""
    if prev_img == curr_img and (prev_name or "") == (curr_name or ""):
        return ""
    old_disp = prev_name or _short(prev_img)
    new_disp = curr_name or _short(curr_img)
    return f"{label} [b]{old_disp}[/b] → [bold]{new_disp}[/bold]"

def _diff_payload(prev: dict, curr: dict) -> str:
    """
    Build a diff between prev and curr payloads.
    Only shows changed parts; returns '' if no visible changes.
    """
    prev = prev or {}
    curr = curr or {}
    lines = []

    # Players
    lines.append(_diff_section("Player 1", prev.get("player1", {}), curr.get("player1", {}),
                               [("name", "Name"), ("id", "ID"), ("clan", "Clan Tag"), ("wl", "W/L"), ("score", "Score"), ("character", "Character"), ("img", "Img")]))
    lines.append(_diff_section("Player 2", prev.get("player2", {}), curr.get("player2", {}),
                               [("name", "Name"), ("id", "ID"), ("clan", "Clan Tag"), ("wl", "W/L"), ("score", "Score"), ("character", "Character"), ("img", "Img")]))

    # Teams
    lines.append(_diff_section("Team 1", prev.get("team1", {}), curr.get("team1", {}),
                               [("name", "Name"), ("score", "Score"), ("img", "Img")]))
    lines.append(_diff_section("Team 2", prev.get("team2", {}), curr.get("team2", {}),
                               [("name", "Name"), ("score", "Score"), ("img", "Img")]))

    # Top bar & meta
    st = _diff_scalar("Event Stage", prev.get("stage"), curr.get("stage"))
    if st: lines.append(st)
    mt = _diff_scalar("Match Type", prev.get("match_type"), curr.get("match_type"))
    if mt: lines.append(mt)
    tt = _diff_scalar("Top Text", prev.get("toptext"), curr.get("toptext"))
    if tt: lines.append(tt)

    # Casters (if present)
    lines.append(_diff_section("Caster 1", prev.get("caster1", {}), curr.get("caster1", {}),
                               [("name", "Name"), ("twitch", "Twitch"), ("twitter", "Twitter")]))
    lines.append(_diff_section("Caster 2", prev.get("caster2", {}), curr.get("caster2", {}),
                               [("name", "Name"), ("twitch", "Twitch"), ("twitter", "Twitter")]))

    # UI scale and active template
    us = _diff_scalar("UI scale", prev.get("ui_scale"), curr.get("ui_scale"))
    if us: lines.append(us)
    at = _diff_scalar("Active template", prev.get("active_template"), curr.get("active_template"))
    if at: lines.append(at)

    # Keep only non-empty lines
    lines = [ln for ln in lines if ln]
    return " • ".join(lines)

# ---- port for the app ----
def _validate_port(value, default=8008):
    try:
        p = int(value)
        if 1 <= p <= 65535:
            return p
    except Exception:
        pass
    return default

def ensure_port_in_data(default_port=8008):
    """Ensure data.json has a 'port' key; if missing, set to default_port."""
    data = load_data() or {}
    if "port" not in data:
        data["port"] = default_port
        save_data(data)
        log.info(f"No port in data.json — set to [bold]{default_port}[/bold]")
    return data["port"]

def get_configured_port():
    """Read port from data.json (validated), falling back to 8080."""
    data = load_data() or {}
    return _validate_port(data.get("port", 8008), 8008)

@app.route('/config/port', methods=['POST'])
def set_port_config():
    payload = request.get_json(force=True) or {}
    new_port = _validate_port(payload.get("port", 0))
    data = load_data() or {}
    old_port = _validate_port(data.get("port", 8008))
    if new_port == old_port:
        return jsonify(ok=True, message="Port unchanged", port=old_port)
    data["port"] = new_port
    save_data(data)
    log.info(f"Port updated in data.json → [bold]{new_port}[/bold] "
             f"[dim](restart the app to apply)[/dim]")
    return jsonify(ok=True, message="Port saved. Restart app to apply.", port=new_port)

# ---- banner with the template and title ----
def _banner():
    console.rule("[bold magenta]FGCaster Overlay")
    log.info(f"Active template: [bold]{get_active_template()}[/bold]")

# ---- active template helpers ----
def get_active_template():
    data = load_data() or {}
    name = (data.get("active_template") or "").strip()
    return name if name else "default"

def set_active_template(name):
    # ensure folder exists
    folder = os.path.join(TEMPLATES_ROOT, name)
    if not os.path.isdir(folder):
        raise ValueError(f"Unknown template: {name}")
    # persist in data.json as the single source of truth
    data = load_data() or {}
    data["active_template"] = name
    save_data(data)

def ensure_active_template(default_name="default"):
    data = load_data() or {}
    if "active_template" not in data:
        data["active_template"] = default_name
        save_data(data)
        log.info(f":frame_photo: No active_template found — set to [bold]{default_name}[/bold]")
    return data["active_template"]

def _extract_game_from_template(template_name: str) -> str | None:
    """Read templates/<template_name>/template.html and extract the overlayGame hidden input."""
    tpl_path = os.path.join(TEMPLATES_ROOT, template_name, "template.html")
    try:
        with open(tpl_path, "r", encoding="utf-8") as f:
            # we don't need the whole file, game tag will be near the top
            head = f.read(4096)
        m = GAME_TAG_RE.search(head)
        if m:
            val = m.group(1).strip()
            return val or None
    except OSError:
        pass
    return None

def maybe_run_scraper_on_startup():
    try:
        data = load_data() or {}
        active = get_active_template()
        slug = _extract_game_from_template(active) or ""
        if not slug:
            return
        should_force = bool(data.get("char_override"))
        if should_force or not _charlist_exists(slug):
            threading.Thread(target=_run_char_scraper_for_slug, args=(slug,), daemon=True).start()
    except Exception as ex:
        log.warning(f"[dim]Startup scraper skipped[/dim] — {ex!r}")

# ---- data helpers ----
def save_data(data):
    """Atomically write data.json so readers never see a partial file."""
    tmp = DATA_FILE + ".tmp"
    with DATA_LOCK:  # serialize writers with readers
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DATA_FILE)  # atomic on Windows & POSIX

def load_data():
    """Read data.json safely; tolerate concurrent writes with a brief retry."""
    if not os.path.exists(DATA_FILE):
        return {}
    # a tiny retry loop in case we hit the file mid-replace (very rare)
    for attempt in range(3):
        with DATA_LOCK:  # serialize with writers
            try:
                with open(DATA_FILE, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                # file could be in the middle of being replaced/written; wait and retry
                time.sleep(0.02)  # 20 ms
            except OSError:
                # possible transient sharing violation during replace on Windows
                time.sleep(0.02)
    # If we still fail, treat as empty rather than crashing; log once
    log.warning("[dim]data.json read was transiently invalid; returning empty state[/dim]")
    return {}

# ---- request logging (pretty + timed) ----
@app.before_request
def _start_timer():
    g._t0 = time.perf_counter()

@app.after_request
def _log_req(resp):
    if not ENABLE_REQUEST_LOGGING:
        return resp  # Skip logging entirely if disabled
    
    try:
        dt = (time.perf_counter() - getattr(g, "_t0", time.perf_counter())) * 1000
        method = request.method
        path = request.full_path if request.query_string else request.path
        status = resp.status_code
        color = "green" if status < 400 else ("yellow" if status < 500 else "red")
        log.info(f"{method} [bold]{path}[/bold] • [bold {color}]{status}[/bold {color}] • {dt:.1f}ms")
    except Exception:
        pass
    return resp

# ---- assets route: /assets/<template>/<path> ----
@app.route("/assets/<template>/<path:filename>")
def template_asset(template, filename):
    folder = os.path.join(TEMPLATES_ROOT, template)
    full_path = os.path.join(folder, filename)
    # naive security: keep access inside the template folder
    if not os.path.abspath(full_path).startswith(os.path.abspath(folder)):
        abort(404)
    if not os.path.exists(full_path):
        abort(404)
    return send_from_directory(folder, filename)
    
# ---- inject helper into Jinja ----
@app.context_processor
def inject_asset_url():
    def asset_url(template, path):
        return url_for("template_asset", template=template, filename=path)
    return {"asset_url": asset_url}

# ---- controller UI ----
@app.route('/', methods=['GET'])
def controller():
    return send_file(os.path.join(STATIC_ROOT, 'controller.html'))

@app.route('/data.json')
def serve_data_json():
    # Ensure the file exists so first-run EXE doesn’t 404
    if not os.path.exists(DATA_FILE):
        save_data({})  # or populate with defaults
    # Conditional sends proper 304s; explicit mimetype helps some clients
    return send_file(DATA_FILE, mimetype='application/json', conditional=True)

# ---- sanitize and check template ----
def is_valid_template(name: str) -> bool:
    if not name: return False
    folder = os.path.join(TEMPLATES_ROOT, name)
    return os.path.isdir(folder) and os.path.isfile(os.path.join(folder, "template.html"))

def sanitize_incoming(update: dict):
    #if "active_template" in update and not is_valid_template(update["active_template"]):
    # UI must not change the active template here; use /set-template for that.
    update.pop("active_template", None)

# ---- POST that emits overlay data ----
@app.route('/emit', methods=['POST'])
def emit_data():
    new_data = request.get_json(silent=True) or {}
    
    sanitize_incoming(new_data)
    
    # Load previous state for diff BEFORE saving
    try:
        prev = load_data() or {}
    except Exception:
        prev = {}

    # Persist while preserving config keys like "port"
    save_data_preserving(new_data)
    socketio.emit('update_scoreboard', new_data)

    # Human-friendly diff log
    try:
        diff = _diff_payload(prev, new_data)
        if diff:
            log.info(f"[bold cyan]Broadcast update[/bold cyan] → {diff}")
        #else:
            # nothing visible changed (may still include hidden/internal keys)
            #log.info("Broadcast update → [dim]no visible changes[/dim]")
    except Exception as e:
        # fall back to the old summary if something went wrong diffing
        log.info(f"[bold cyan]Broadcast update[/bold cyan] → {_summarize_payload(new_data)}  [dim](diff error {e!r})[/dim]")

    return jsonify(success=True)

# ---- single, static overlay URL ----
@app.route('/scoreboard')
def scoreboard():
    active = get_active_template()
    # Render that template’s "template.html" file
    #log.info(f"Render overlay with template [bold]{active}[/bold]")
    return render_template(f"{active}/template.html", template_name=active)

# ---- list available templates ----
def list_templates():
    names = []
    if not os.path.isdir(TEMPLATES_ROOT):
        return names
    for entry in os.listdir(TEMPLATES_ROOT):
        folder = os.path.join(TEMPLATES_ROOT, entry)
        if not os.path.isdir(folder):
            continue
        if os.path.isfile(os.path.join(folder, "template.html")):
            names.append(entry)
    names.sort()
    return names
    
# --- new template route ---
@app.route("/templates/list")
def templates_list():
    templates = list_templates()
    active = get_active_template()
    meta = {}

    for name in templates:
        game = _extract_game_from_template(name)
        if game:
            meta[name] = {"game": game}

    return jsonify({
        "templates": templates,
        "active": active,
        "meta": meta,
    })

# ---- switch template from the controller UI ----
@app.route('/set-template', methods=['POST'])
def set_template():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("template") or "").strip()
    set_active_template(name)

    # also store in data.json so the controller reloads with it selected
    data = load_data() or {}
    data["active_template"] = name
    if "char_override" in payload:
        data["char_override"] = bool(payload["char_override"])
    save_data(data)
    
    # derive game slug and run scraper in background
    slug = _extract_game_from_template(name) or ""
    should_force = bool(data.get("char_override"))
    if slug and (should_force or not _charlist_exists(slug)):
        threading.Thread(target=_run_char_scraper_for_slug, args=(slug,), daemon=True).start()
    
    # notify overlays to reload
    socketio.emit('template_changed', {"template": name})
    log.info(f"[bold cyan]Switched active template[/bold cyan] → [bold]{name}[/bold]")
    return jsonify(ok=True)

# ---- new reset functions ----
@app.route('/reset/players', methods=['POST'])
def reset_players():
    """Reset only players (names, scores, clans, imgs, W/L), keep everything else."""
    current = load_data() or {}
    # keep non-player fields as-is
    cleared = dict(current)
    cleared["player1"] = {
        "name": "",
        "id": "",
        "clan": "",
        "wl": "",
        "score": 0,
        "character": "",
        "img": ""
    }
    cleared["player2"] = {
        "name": "",
        "id": "",
        "clan": "",
        "wl": "",
        "score": 0,
        "character": "",
        "img": ""
    }

    # Persist while preserving server-owned keys no matter what
    save_data_preserving(cleared)

    socketio.emit('update_scoreboard', cleared)
    log.info("[bold cyan]Player values reset")
    return jsonify(ok=True)

@app.route('/reset/teams', methods=['POST'])
def reset_teams():
    """Reset only teams (names, scores, imgs), keep everything else."""
    current = load_data() or {}
    # keep non-player fields as-is
    cleared = dict(current)
    cleared["team1"] = {
        "name": "",
        "score": 0,
        "img": ""
    }
    cleared["team2"] = {
        "name": "",
        "score": 0,
        "img": ""
    }

    # Persist while preserving server-owned keys no matter what
    save_data_preserving(cleared)

    socketio.emit('update_scoreboard', cleared)
    log.info("[bold cyan]Team values reset")
    return jsonify(ok=True)

@app.route('/reset/all', methods=['POST'])
def reset_all():
    """Reset the scoreboard data, preserving server-owned config keys (port/template)
       and optional UI config like ui_scale."""
    current = load_data() or {}

    # Build a fresh, cleared scoreboard payload
    cleared = {
        "player1": {
            "name": "",
            "id": "",
            "clan": "",
            "wl": "",
            "score": 0,
            "character": "",
            "img": ""
        },
        "player2": {
            "name": "",
            "id": "",
            "clan": "",
            "wl": "",
            "score": 0,
            "character": "",
            "img": ""
        },
        "team1": {
            "name": "",
            "score": 0,
            "img": ""
        },
        "team2": {
            "name": "",
            "score": 0,
            "img": ""
        },
        "stage": "",
        "match_type": "",
        "toptext": "",
        "caster1": {
            "name": "",
            "twitch": "",
            "twitter": ""
        },
        "caster2": {
            "name": "",
            "twitch": "",
            "twitter": ""
        }
    }

    # Carry over any non-scoreboard, UI-level settings you want to persist
    for k in ("ui_scale", "char_override"):  # add more keys here if you want to keep them across "Reset All"
        if k in current:
            cleared[k] = current[k]

    # Persist while preserving server-owned keys no matter what
    save_data_preserving(cleared)

    socketio.emit('update_scoreboard', cleared)
    log.info("[bold cyan]All values reset")
    return jsonify(ok=True)

# ----------------------------------

# Track roles for pretty logging and targeted emits
client_roles = {}  # sid -> "overlay" | "controller" | "unknown"

def _infer_role_from_headers():
    ref = (request.headers.get('Referer') or '').lower()
    # Heuristic: overlay page path contains '/scoreboard'; controller UI is '/'.
    if '/scoreboard' in ref:
        return 'overlay'
    # When controller UI loads from '/', referer typically ends with "/"
    return 'controller' if ref.endswith('/') or ref.endswith('/#') or ref.rstrip('/').endswith(':{}'.format(get_configured_port())) else 'unknown'

@socketio.on('connect')
def on_connect():
    role = _infer_role_from_headers()
    client_roles[request.sid] = role

    if role == 'overlay':
        # Send current state only to overlays
        try:
            with open(DATA_FILE, 'r') as f:
                emit('update_scoreboard', json.load(f), to=request.sid)
        except Exception as e:
            log.warning(f"[dim]No data.json yet or read error[/dim] — {e!r}")
        log.info("[bold green]Overlay connected[/bold green]")
    elif role == 'controller':
        log.info("[bold green]Controller UI connected[/bold green]")
    else:
        log.info("Client connected")

@socketio.on('disconnect')
def on_disconnect():
    role = client_roles.pop(request.sid, 'client')
    if role == 'overlay':
        log.info("[red]Overlay disconnected[/red]")
    elif role == 'controller':
        log.info("[red]Controller UI disconnected[/red]")
    else:
        log.info("Client disconnected")

if __name__ == '__main__':
    USE_RELOADER = False if IS_FROZEN else True

    run_kwargs = dict(debug=not IS_FROZEN, use_reloader=USE_RELOADER)
    
    # Pick the right knobs per async mode (to avoid raw access logs)
    mode = getattr(socketio, "async_mode", None)
    if mode == 'eventlet':
        run_kwargs.update(log_output=False)
    elif mode == 'gevent':
        run_kwargs.update(log=None)
    else:
        # Werkzeug threaded server
        from werkzeug.serving import WSGIRequestHandler
        class QuietRequestHandler(WSGIRequestHandler):
            def log(self, *a, **k): pass
            def log_request(self, *a, **k): pass
            def log_error(self, *a, **k): super().log_error(*a, **k)
        run_kwargs.update(request_handler=QuietRequestHandler)

    # Only the serving child should print startup logs when reloader is ON
    def _should_print_startup():
        if USE_RELOADER:
            return os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
        return True

    # Ensure a port exists in data.json (writes 8008 if missing)
    ensure_port_in_data(8008)
    port = get_configured_port()
    
    ensure_active_template("default")

    if _should_print_startup():
        _banner() 
        try:
            _data = load_data() or {}
            log.info(f"[bold cyan]Loaded saved state[/bold cyan] → {_summarize_payload(_data)}")
        except Exception as e:
            log.warning(f"[dim]No saved state found or could not read data.json[/dim] — {e!r}")
        log.info(f"[bold green]Server loaded successfully![/bold green] "
             f"Listening on [bold]http://127.0.0.1:{port}[/bold] "
             f"(to change port, edit data.json and restart)")
        try:
            threading.Timer(0.10, maybe_run_scraper_on_startup).start()
        except Exception as e:
            log.error(f"Startup scraper trigger failed — {e!r}")
    
    socketio.run(app, port=port, **run_kwargs)
