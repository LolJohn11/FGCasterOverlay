from flask import Flask, render_template, request, jsonify, url_for, abort, send_from_directory, send_file, g
from flask_socketio import SocketIO, emit
import json, os, sys, time, logging, re, click, threading

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
        parts.append(f"[yellow]Players: [/yellow][b]{(p1.get('name') or '—')}[/b] {p1.get('score',0)} — [b]{(p2.get('name') or '—')}[/b] {p2.get('score',0)}")
    if t1 or t2:
        parts.append(f"[yellow]Teams:[/yellow] [b]{(t1.get('name') or '—')}[/b] {t1.get('score',0)} vs [b]{(t2.get('name') or '—')}[/b] {t2.get('score',0)}")
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
                               [("name", "Name"), ("clan", "Clan Tag"), ("wl", "W/L"), ("score", "Score"), ("img", "Img")]))
    lines.append(_diff_section("Player 2", prev.get("player2", {}), curr.get("player2", {}),
                               [("name", "Name"), ("clan", "Clan Tag"), ("wl", "W/L"), ("score", "Score"), ("img", "Img")]))

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
    return jsonify({
        "templates": list_templates(),
        "active": get_active_template()
    })

# ---- switch template from the controller UI ----
@app.route('/set-template', methods=['POST'])
def set_template():
    payload = request.get_json(force=True)
    name = payload.get("template")
    set_active_template(name)

    # also store in data.json so the controller reloads with it selected
    data = load_data() or {}
    data["active_template"] = name
    save_data(data)

    # notify overlays to reload
    socketio.emit('template_changed', {"template": name})
    log.info(f"[bold cyan]Switched active template[/bold cyan] → [bold]{name}[/bold]")
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
        #log.info("Waiting for overlay to connect...")

    socketio.run(app, port=port, **run_kwargs)
