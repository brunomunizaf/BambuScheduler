#!/usr/bin/env python3
"""BambuScheduler backend - Upload, schedule, and control prints via MQTT + FTP (LAN only)."""

import os
import sys
import ssl
import json
import time
import zipfile
import logging
import subprocess
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import paho.mqtt.client as mqtt
from flask import Flask, request, jsonify, render_template, Response
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()


def resource_path(relative: str) -> Path:
    """Resolve a bundled resource (e.g. templates/) whether running from
    source or from a PyInstaller onefile executable."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / relative


app = Flask(__name__, template_folder=str(resource_path("templates")))
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB max upload

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "BambuScheduler"
APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE = APP_SUPPORT_DIR / "config.json"
LOG_FILE = APP_SUPPORT_DIR / "bambu-scheduler.log"
MQTT_PORT = 8883
FTP_PORT = 990

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(funcName)s] %(message)s"
logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format=_LOG_FORMAT,
)
# Mirror everything to stdout too, so logs are visible both in the log file and
# when the backend is run directly (python3 web.py).
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.getLogger().addHandler(_console)

log = logging.getLogger("bambu")


def _mask(secret) -> str:
    """Render a secret for logs: show length only, never the value."""
    if not secret:
        return "<empty>"
    return f"<{len(str(secret))} chars>"


def _load_config():
    """Load printer config from GUI config file, falling back to .env."""
    global PRINTER_IP, ACCESS_CODE, SERIAL, PRINTER_NAME, LANGUAGE
    if CONFIG_FILE.exists():
        log.info(f"Loading config from {CONFIG_FILE}")
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            PRINTER_IP = cfg.get("printerIP") or os.getenv("PRINTER_IP")
            ACCESS_CODE = cfg.get("accessCode") or os.getenv("PRINTER_ACCESS_CODE")
            SERIAL = cfg.get("serial") or os.getenv("PRINTER_SERIAL")
            PRINTER_NAME = cfg.get("printerName") or os.getenv("PRINTER_NAME")
            LANGUAGE = cfg.get("language") or "en"
            log.info(
                f"Config loaded: printerIP={PRINTER_IP!r}, serial={SERIAL!r}, "
                f"accessCode={_mask(ACCESS_CODE)}, printerName={PRINTER_NAME!r}"
            )
            _warn_missing_config()
            return
        except (json.JSONDecodeError, OSError) as e:
            log.error(f"Failed to read config file {CONFIG_FILE}: {e!r} — falling back to environment variables")
    else:
        log.info(f"No config file at {CONFIG_FILE} — falling back to environment variables")
    PRINTER_IP = os.getenv("PRINTER_IP")
    ACCESS_CODE = os.getenv("PRINTER_ACCESS_CODE")
    SERIAL = os.getenv("PRINTER_SERIAL")
    PRINTER_NAME = os.getenv("PRINTER_NAME")
    LANGUAGE = "en"
    log.info(
        f"Config from env: printerIP={PRINTER_IP!r}, serial={SERIAL!r}, "
        f"accessCode={_mask(ACCESS_CODE)}, printerName={PRINTER_NAME!r}"
    )
    _warn_missing_config()


def _warn_missing_config():
    """Log a clear warning for each required setting that is missing."""
    for name, value in (("printer IP", PRINTER_IP), ("access code", ACCESS_CODE), ("serial number", SERIAL)):
        if not value:
            log.warning(f"Printer {name} is NOT set — the app cannot reach the printer until it is configured in the setup screen")


PRINTER_IP = None
ACCESS_CODE = None
SERIAL = None
PRINTER_NAME = None
LANGUAGE = "en"
_load_config()
UPLOAD_DIR = APP_SUPPORT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
JOBS_FILE = APP_SUPPORT_DIR / "scheduled_jobs.json"

scheduler = BackgroundScheduler()
scheduler.start()


# --- Job persistence ---

def save_jobs():
    """Save scheduled jobs to disk so they survive restarts."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "run_date": job.next_run_time.isoformat() if job.next_run_time else None,
            "args": list(job.args),
        })
    JOBS_FILE.write_text(json.dumps(jobs, indent=2))


def load_jobs():
    """Reload persisted jobs on startup."""
    if not JOBS_FILE.exists():
        return
    try:
        jobs = json.loads(JOBS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return
    now = datetime.now()
    for job in jobs:
        run_date = datetime.fromisoformat(job["run_date"]) if job.get("run_date") else None
        if not run_date:
            continue
        # Strip timezone info for comparison
        run_date_naive = run_date.replace(tzinfo=None) if run_date.tzinfo else run_date
        if run_date_naive <= now:
            continue  # Skip past jobs
        args = job["args"]  # [filepath, use_ams, ams_slot, timelapse]
        if not Path(args[0]).exists():
            continue  # Skip if file was deleted
        scheduler.add_job(
            run_scheduled_print,
            "date",
            run_date=run_date,
            args=args,
            name=job["name"],
            id=job["id"],
        )
        app.logger.info(f"Restored job: {job['name']} @ {run_date}")

# Cached printer state, kept fresh by the background poller thread and read by
# the API routes. Guarded by _state_lock since the poller and Flask worker
# threads touch it concurrently.
printer_state = {
    "status": None,        # dict with gcode_state/progress/temps/... or None
    "ams_trays": [],       # list of {slot, type, color, empty}
    "printer_name": PRINTER_NAME,
    "last_update": None,
}
_state_lock = threading.Lock()
POLL_INTERVAL = 5  # seconds between pushall refreshes


# --- MQTT helpers ---

def _make_mqtt_client():
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"bambu-web-{int(time.time())}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set("bblp", ACCESS_CODE)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ctx)
    return client


def mqtt_publish(payload: dict):
    """Publish a single MQTT command and disconnect."""
    command = payload.get("print", {}).get("command", "?")
    log.info(f"MQTT publish: command={command!r} to {PRINTER_IP}:{MQTT_PORT} (serial={SERIAL!r})")
    if not (PRINTER_IP and SERIAL and ACCESS_CODE):
        log.error("MQTT publish aborted: printer IP, serial, or access code is not configured")
        return False
    client = _make_mqtt_client()
    result = {"done": False}

    def on_connect(cli, ud, flags, rc, props):
        if rc == 0:
            topic = f"device/{SERIAL}/request"
            cli.publish(topic, json.dumps(payload))
            log.info(f"MQTT connected (rc=0), published command={command!r} to topic {topic}")
            result["done"] = True
        else:
            log.error(f"MQTT connect refused: rc={rc} (check access code and that LAN/Developer mode are on)")

    client.on_connect = on_connect
    try:
        client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
    except OSError as e:
        log.error(f"MQTT connection to {PRINTER_IP}:{MQTT_PORT} failed: {e!r} (is the printer on the same network and reachable?)")
        return False
    client.loop_start()

    deadline = time.time() + 10
    while time.time() < deadline and not result["done"]:
        time.sleep(0.2)

    client.loop_stop()
    client.disconnect()
    if not result["done"]:
        log.error(f"MQTT publish timed out after 10s waiting to connect to {PRINTER_IP}:{MQTT_PORT}")
    return result["done"]


def _apply_state(updates: dict):
    """Merge parsed fields into the shared printer_state under the lock."""
    if not updates:
        return
    with _state_lock:
        printer_state.update(updates)
        printer_state["last_update"] = datetime.now().isoformat()


def _handle_report(payload: bytes):
    """Parse an MQTT report from the printer and update the cached state."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return

    updates = {}

    # get_version carries the printer model, used as a name fallback
    if "info" in data:
        for mod in data["info"].get("module", []):
            if mod.get("product_name"):
                updates["printer_name"] = mod["product_name"]
                break

    p = data.get("print")
    if not isinstance(p, dict):
        _apply_state(updates)
        return

    if p.get("machine_name"):
        updates["printer_name"] = p["machine_name"]

    if p.get("gcode_state") is not None:
        error_code = p.get("print_error", 0)
        hms = p.get("hms", [])
        error_msg = ""
        if error_code and error_code != 0:
            error_msg = f"Error: 0x{error_code:08X}"
        elif hms:
            # HMS (Health Management System) messages
            error_msg = "; ".join(h.get("msg", h.get("code", "")) for h in hms[:3])
        updates["status"] = {
            "gcode_state": p.get("gcode_state", "unknown"),
            "progress": p.get("mc_percent", 0),
            "remaining_time": p.get("mc_remaining_time", 0),
            "nozzle_temp": round(p.get("nozzle_temper", 0), 1),
            "bed_temp": round(p.get("bed_temper", 0), 1),
            "subtask_name": p.get("subtask_name", ""),
            "error_msg": error_msg,
            "print_error": error_code,
            # Fine-grained current stage (heating, changing filament, calibrating,
            # …). -1 means no special stage — just normal printing.
            "stg_cur": p.get("stg_cur", -1),
        }

    if "ams" in p:
        trays_by_slot = {}
        for unit in p["ams"].get("ams", []):
            unit_id = int(unit.get("id", 0))
            for tray in unit.get("tray", []):
                tray_id = int(tray.get("id", 0))
                slot = unit_id * 4 + tray_id
                filament = tray.get("tray_type", "")
                color = tray.get("tray_color", "")[:6]
                trays_by_slot[slot] = {
                    "slot": slot,
                    "type": filament,
                    "color": f"#{color}" if color else "",
                    "empty": not filament,
                }
        if trays_by_slot:
            updates["ams_trays"] = sorted(trays_by_slot.values(), key=lambda t: t["slot"])

    _apply_state(updates)


def _status_poller():
    """Maintain a single persistent MQTT connection to the printer and keep
    printer_state fresh. Reconnects automatically and picks up config changes.
    Replaces the old connect-per-request model that blocked each /api/status
    call for several seconds."""
    while True:
        ip, code, serial = PRINTER_IP, ACCESS_CODE, SERIAL
        if not (ip and code and serial):
            time.sleep(2)
            continue

        client = None
        try:
            def on_connect(cli, ud, flags, rc, props):
                if rc == 0:
                    cli.subscribe(f"device/{serial}/report")
                    cli.publish(f"device/{serial}/request",
                                json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))
                    cli.publish(f"device/{serial}/request",
                                json.dumps({"info": {"sequence_id": "1", "command": "get_version"}}))

            client = _make_mqtt_client()
            client.on_connect = on_connect
            client.on_message = lambda cli, ud, msg: _handle_report(msg.payload)
            client.connect(ip, MQTT_PORT, keepalive=60)
            client.loop_start()

            # Refresh periodically until the config changes under us.
            while (PRINTER_IP, ACCESS_CODE, SERIAL) == (ip, code, serial):
                time.sleep(POLL_INTERVAL)
                try:
                    client.publish(f"device/{serial}/request",
                                   json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}}))
                except Exception:
                    break
        except Exception as e:
            app.logger.warning(f"Status poller reconnecting: {e}")
            time.sleep(3)
        finally:
            if client is not None:
                try:
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass


def safe_upload_path(filename: str) -> Path | None:
    """Resolve a client-supplied filename to a path *inside* UPLOAD_DIR,
    stripping any directory components so it can't traverse out (e.g.
    "../../etc/passwd" collapses to "passwd"). Returns None if invalid."""
    name = Path(filename or "").name
    if not name or name in (".", ".."):
        return None
    return UPLOAD_DIR / name


def validate_3mf(filepath: Path):
    with zipfile.ZipFile(filepath) as z:
        if "Metadata/plate_1.gcode" not in z.namelist():
            raise ValueError("File not sliced (no gcode). Slice it in Bambu Studio first.")


def read_3mf_thumbnail(filepath: Path) -> bytes | None:
    """Return the embedded plate render PNG from a sliced .3mf, trying the
    higher-quality plates first and falling back to the small thumbnails."""
    candidates = [
        "Metadata/plate_1.png",
        "Metadata/plate_no_light_1.png",
        "Auxiliaries/.thumbnails/thumbnail_middle.png",
        "Auxiliaries/.thumbnails/thumbnail_3mf.png",
        "Metadata/plate_1_small.png",
    ]
    with zipfile.ZipFile(filepath) as z:
        names = set(z.namelist())
        for name in candidates:
            if name in names:
                return z.read(name)
    return None


def upload_to_printer(filepath: Path) -> str:
    filename = filepath.name
    log.info(f"FTP upload starting: file={filename!r}, path={filepath}")
    if not PRINTER_IP:
        log.error("FTP upload aborted: printer IP is not configured")
        raise RuntimeError("Printer is not configured — set the IP in the app's setup screen.")
    if not filepath.exists():
        log.error(f"FTP upload aborted: file does not exist on disk: {filepath}")
        raise RuntimeError(f"File not found on disk: {filepath}")

    size = filepath.stat().st_size
    # Percent-encode the filename so spaces/special characters produce a valid
    # URL; curl decodes it back before issuing the FTP STOR command.
    remote_path = quote(filename)
    url = f"ftps://{PRINTER_IP}:{FTP_PORT}/{remote_path}"
    log.info(f"FTP upload: {size} bytes -> {url} (user=bblp, accessCode={_mask(ACCESS_CODE)})")

    started = time.monotonic()
    result = subprocess.run(
        [
            "curl", "--ssl-reqd", "--insecure",
            "--user", f"bblp:{ACCESS_CODE}",
            "-T", str(filepath),
            url,
            "--connect-timeout", "15",
            "--max-time", "300",
        ],
        capture_output=True, text=True,
    )
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        stderr = result.stderr.strip()
        log.error(
            f"FTP upload FAILED after {elapsed:.1f}s: curl exit {result.returncode}. "
            f"stderr: {stderr or '<none>'}"
        )
        raise RuntimeError(f"FTP upload failed (curl exit {result.returncode}): {stderr}")
    log.info(f"FTP upload OK: {filename!r} ({size} bytes) in {elapsed:.1f}s")
    return filename


def start_print(filename: str, use_ams: bool, ams_slot: int, timelapse: bool):
    log.info(
        f"Sending print command: file={filename!r}, use_ams={use_ams}, "
        f"ams_slot={ams_slot}, timelapse={timelapse}"
    )
    cmd = {
        "print": {
            "sequence_id": str(int(time.time())),
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "subtask_name": Path(filename).stem,
            "file": "",
            "url": f"ftp:///{filename}",
            "md5": "",
            "bed_type": "auto",
            "timelapse": timelapse,
            "bed_levelling": True,
            "flow_cali": True,
            "vibration_cali": True,
            "layer_inspect": False,
            "use_ams": use_ams,
            "ams_mapping": [ams_slot] if use_ams else "",
        }
    }
    ok = mqtt_publish(cmd)
    if ok:
        log.info(f"Print command published to printer for {filename!r}")
    else:
        log.error(f"Print command FAILED to publish for {filename!r} (MQTT publish returned false)")
    return ok


def run_scheduled_print(filepath: str, use_ams: bool, ams_slot: int, timelapse: bool):
    """Background job for scheduled prints."""
    log.info(f"Scheduled print firing now: {filepath!r}")
    try:
        fname = upload_to_printer(Path(filepath))
        start_print(fname, use_ams, ams_slot, timelapse)
        log.info(f"Scheduled print started successfully: {fname!r}")
    except Exception as e:
        log.exception(f"Scheduled print FAILED for {filepath!r}: {e}")


# --- Routes ---

@app.route("/")
def index():
    files = sorted(UPLOAD_DIR.glob("*.3mf"), key=lambda f: f.stat().st_mtime, reverse=True)
    filenames = [f.name for f in files]
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")) if job.next_run_time else "?",
            "args": job.args,
        })
    return render_template("index.html", files=filenames, jobs=jobs, lang=LANGUAGE)


@app.route("/api/status")
def api_status():
    # Served instantly from the poller-maintained cache (no per-request MQTT).
    with _state_lock:
        return jsonify({
            "status": printer_state["status"],
            "ams_trays": printer_state["ams_trays"],
            "printer_name": printer_state["printer_name"],
            "language": LANGUAGE,
        })


@app.route("/api/logs")
def api_logs():
    """Return the tail of the log file for the live-logs panel in the web UI.

    ?lines=N  (default 200, max 2000) — how many trailing lines to return.
    """
    try:
        lines = min(max(int(request.args.get("lines", 200)), 1), 2000)
    except (TypeError, ValueError):
        lines = 200
    try:
        with open(LOG_FILE, "r", errors="replace") as fh:
            tail = deque(fh, maxlen=lines)
    except FileNotFoundError:
        return jsonify({"lines": [], "path": str(LOG_FILE)})
    return jsonify({"lines": [ln.rstrip("\n") for ln in tail], "path": str(LOG_FILE)})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    log.info("Upload request received")
    if "file" not in request.files:
        log.warning("Upload rejected: no 'file' part in request")
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".3mf"):
        log.warning(f"Upload rejected: filename {f.filename!r} is not a .3mf")
        return jsonify({"error": "File must be .3mf"}), 400

    dest = safe_upload_path(f.filename)
    if dest is None:
        log.warning(f"Upload rejected: filename {f.filename!r} resolved to an invalid path")
        return jsonify({"error": "Invalid filename"}), 400
    f.save(dest)
    log.info(f"Upload saved: {dest.name!r} ({dest.stat().st_size} bytes) at {dest}")

    try:
        validate_3mf(dest)
    except (ValueError, zipfile.BadZipFile) as e:
        dest.unlink()
        log.warning(f"Upload rejected: {dest.name!r} failed validation: {e} (deleted from disk)")
        return jsonify({"error": str(e)}), 400

    log.info(f"Upload validated OK: {dest.name!r}")
    return jsonify({"ok": True, "filename": dest.name})


@app.route("/api/print", methods=["POST"])
def api_print():
    data = request.get_json(silent=True) or {}
    filename = data.get("filename")
    log.info(f"Print request received: {data}")
    if not filename:
        log.warning("Print rejected: no filename in request")
        return jsonify({"error": "filename required"}), 400

    filepath = safe_upload_path(filename)
    if filepath is None or not filepath.exists():
        log.warning(f"Print rejected: file {filename!r} not found (resolved to {filepath})")
        return jsonify({"error": "File not found"}), 404

    use_ams = data.get("use_ams", False)
    ams_slot = int(data.get("ams_slot", 0))
    timelapse = data.get("timelapse", False)
    schedule_time = data.get("schedule_time")

    if schedule_time:
        log.info(f"Print requested as SCHEDULED for {schedule_time!r}: {filename!r}")
        try:
            run_at = datetime.fromisoformat(schedule_time)
        except ValueError:
            log.warning(f"Schedule rejected: invalid date format {schedule_time!r}")
            return jsonify({"error": "Invalid date format"}), 400

        if run_at <= datetime.now():
            log.warning(f"Schedule rejected: {run_at} is in the past")
            return jsonify({"error": "Date is in the past"}), 400

        job = scheduler.add_job(
            run_scheduled_print,
            "date",
            run_date=run_at,
            args=[str(filepath), use_ams, ams_slot, timelapse],
            name=filename,
        )
        save_jobs()
        log.info(f"Print scheduled: {filename!r} at {run_at} (job_id={job.id})")
        return jsonify({"ok": True, "scheduled": str(run_at), "job_id": job.id})

    # Print now
    log.info(f"Print requested as PRINT NOW: {filename!r}")
    try:
        fname = upload_to_printer(filepath)
        start_print(fname, use_ams, ams_slot, timelapse)
        log.info(f"Print-now completed for {fname!r}")
        return jsonify({"ok": True, "message": "Print started"})
    except Exception as e:
        log.exception(f"Print-now FAILED for {filename!r}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok = mqtt_publish({"print": {"sequence_id": "0", "command": "stop"}})
    return jsonify({"ok": ok})


@app.route("/api/pause", methods=["POST"])
def api_pause():
    ok = mqtt_publish({"print": {"sequence_id": "0", "command": "pause"}})
    return jsonify({"ok": ok})


@app.route("/api/resume", methods=["POST"])
def api_resume():
    ok = mqtt_publish({"print": {"sequence_id": "0", "command": "resume"}})
    return jsonify({"ok": ok})


@app.route("/api/jobs")
def api_jobs():
    # Get current AMS tray colors for cross-reference
    tray_colors = {}
    with _state_lock:
        trays = list(printer_state.get("ams_trays", []))
    for tray in trays:
        tray_colors[tray["slot"]] = tray.get("color", "")

    jobs = []
    for job in scheduler.get_jobs():
        entry = {
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.strftime("%Y-%m-%d %H:%M:%S") if job.next_run_time else "?",
        }
        # args = [filepath, use_ams, ams_slot, timelapse]
        if job.args and len(job.args) >= 3:
            use_ams = job.args[1]
            ams_slot = int(job.args[2])
            if use_ams:
                entry["ams_slot"] = ams_slot
                entry["ams_color"] = tray_colors.get(ams_slot, "")
        jobs.append(entry)
    return jsonify(jobs)


@app.route("/api/thumbnail")
def api_thumbnail():
    """Serve the embedded plate render of an uploaded .3mf for the preview modal."""
    filepath = safe_upload_path(request.args.get("file", ""))
    if filepath is None or not filepath.exists():
        return jsonify({"error": "File not found"}), 404
    try:
        png = read_3mf_thumbnail(filepath)
    except zipfile.BadZipFile:
        return jsonify({"error": "Invalid .3mf file"}), 422
    if png is None:
        return jsonify({"error": "No preview image in this file"}), 404
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route("/api/cancel-job", methods=["POST"])
def api_cancel_job():
    job_id = (request.get_json(silent=True) or {}).get("job_id")
    if not job_id:
        return jsonify({"error": "job_id required"}), 400
    try:
        scheduler.remove_job(job_id)
        save_jobs()
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"error": "Job not found"}), 404


@app.route("/api/log-path")
def api_log_path():
    return jsonify({"path": str(LOG_FILE)})


@app.route("/api/reload-config", methods=["POST"])
def api_reload_config():
    _load_config()
    return jsonify({"ok": True, "printer_ip": PRINTER_IP, "printer_name": PRINTER_NAME})


@app.route("/api/delete-file", methods=["POST"])
def api_delete_file():
    filename = (request.get_json(silent=True) or {}).get("filename")
    if not filename:
        return jsonify({"error": "filename required"}), 400
    filepath = safe_upload_path(filename)
    if filepath is not None and filepath.exists():
        filepath.unlink()
    return jsonify({"ok": True})


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    with app.app_context():
        load_jobs()
    threading.Thread(target=_status_poller, daemon=True).start()
    # Bind to loopback only: the UI and menu bar app both connect locally, and
    # the API can start/stop prints and read/write files without auth, so it
    # must not be reachable from other devices on the LAN.
    app.run(host="127.0.0.1", port=8080, debug=False)
