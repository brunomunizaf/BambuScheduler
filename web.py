#!/usr/bin/env python3
"""BambuTiming backend - Upload, schedule, and control prints via MQTT + FTP (LAN only)."""

import os
import ssl
import json
import time
import zipfile
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from flask import Flask, request, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100MB max upload

CONFIG_FILE = Path.home() / "Library" / "Application Support" / "BambuTiming" / "config.json"
MQTT_PORT = 8883
FTP_PORT = 990


def _load_config():
    """Load printer config from GUI config file, falling back to .env."""
    global PRINTER_IP, ACCESS_CODE, SERIAL, PRINTER_NAME
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            PRINTER_IP = cfg.get("printerIP") or os.getenv("PRINTER_IP")
            ACCESS_CODE = cfg.get("accessCode") or os.getenv("PRINTER_ACCESS_CODE")
            SERIAL = cfg.get("serial") or os.getenv("PRINTER_SERIAL")
            PRINTER_NAME = cfg.get("printerName") or os.getenv("PRINTER_NAME")
            return
        except (json.JSONDecodeError, OSError):
            pass
    PRINTER_IP = os.getenv("PRINTER_IP")
    ACCESS_CODE = os.getenv("PRINTER_ACCESS_CODE")
    SERIAL = os.getenv("PRINTER_SERIAL")
    PRINTER_NAME = os.getenv("PRINTER_NAME")


PRINTER_IP = None
ACCESS_CODE = None
SERIAL = None
PRINTER_NAME = None
_load_config()
PROJECT_DIR = Path(__file__).parent
UPLOAD_DIR = PROJECT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
JOBS_FILE = PROJECT_DIR / "scheduled_jobs.json"

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

# Cached printer state
printer_state = {
    "gcode_state": "unknown",
    "progress": 0,
    "remaining_time": 0,
    "nozzle_temp": 0,
    "bed_temp": 0,
    "subtask_name": "",
    "last_update": None,
}


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
    client = _make_mqtt_client()
    result = {"done": False}

    def on_connect(cli, ud, flags, rc, props):
        if rc == 0:
            cli.publish(f"device/{SERIAL}/request", json.dumps(payload))
            result["done"] = True

    client.on_connect = on_connect
    client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
    client.loop_start()

    deadline = time.time() + 10
    while time.time() < deadline and not result["done"]:
        time.sleep(0.2)

    client.loop_stop()
    client.disconnect()
    return result["done"]


def mqtt_query(timeout=8):
    """Query printer status and AMS via pushall."""
    client = _make_mqtt_client()
    data_out = {"status": None, "ams_trays": [], "printer_name": PRINTER_NAME}

    def on_connect(cli, ud, flags, rc, props):
        if rc == 0:
            cli.subscribe(f"device/{SERIAL}/report")
            cmd = {"pushing": {"sequence_id": "0", "command": "pushall"}}
            cli.publish(f"device/{SERIAL}/request", json.dumps(cmd))
            # Also request version info which contains the printer name
            ver_cmd = {"info": {"sequence_id": "1", "command": "get_version"}}
            cli.publish(f"device/{SERIAL}/request", json.dumps(ver_cmd))

    def on_message(cli, ud, msg):
        try:
            data = json.loads(msg.payload)

            # Extract printer model from get_version as fallback
            if "info" in data and not data_out["printer_name"]:
                modules = data["info"].get("module", [])
                for mod in modules:
                    if mod.get("product_name"):
                        data_out["printer_name"] = mod["product_name"]
                        break

            if "print" not in data:
                return
            p = data["print"]

            if p.get("machine_name"):
                data_out["printer_name"] = p["machine_name"]

            if p.get("gcode_state") is not None:
                error_code = p.get("print_error", 0)
                hms = p.get("hms", [])
                error_msg = ""
                if error_code and error_code != 0:
                    error_msg = f"Error: 0x{error_code:08X}"
                elif hms:
                    # HMS (Health Management System) messages
                    error_msg = "; ".join(
                        h.get("msg", h.get("code", "")) for h in hms[:3]
                    )
                data_out["status"] = {
                    "gcode_state": p.get("gcode_state", "unknown"),
                    "progress": p.get("mc_percent", 0),
                    "remaining_time": p.get("mc_remaining_time", 0),
                    "nozzle_temp": round(p.get("nozzle_temper", 0), 1),
                    "bed_temp": round(p.get("bed_temper", 0), 1),
                    "subtask_name": p.get("subtask_name", ""),
                    "error_msg": error_msg,
                    "print_error": error_code,
                }
                printer_state.update(data_out["status"])
                printer_state["last_update"] = datetime.now().isoformat()

            if data_out["ams_trays"]:
                printer_state["ams_trays"] = data_out["ams_trays"]

            if "ams" in p:
                ams_units = p["ams"].get("ams", [])
                trays_by_slot = {}
                for unit in ams_units:
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
                data_out["ams_trays"] = sorted(trays_by_slot.values(), key=lambda t: t["slot"])
        except (json.JSONDecodeError, KeyError):
            pass

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
    client.loop_start()
    time.sleep(timeout)
    client.loop_stop()
    client.disconnect()
    return data_out


def validate_3mf(filepath: Path):
    with zipfile.ZipFile(filepath) as z:
        if "Metadata/plate_1.gcode" not in z.namelist():
            raise ValueError("File not sliced (no gcode). Slice it in Bambu Studio first.")


def upload_to_printer(filepath: Path) -> str:
    filename = filepath.name
    result = subprocess.run(
        [
            "curl", "--ssl-reqd", "--insecure",
            "--user", f"bblp:{ACCESS_CODE}",
            "-T", str(filepath),
            f"ftps://{PRINTER_IP}:{FTP_PORT}/{filename}",
            "--connect-timeout", "15",
            "--max-time", "300",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FTP upload failed: {result.stderr}")
    return filename


def start_print(filename: str, use_ams: bool, ams_slot: int, timelapse: bool):
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
    return mqtt_publish(cmd)


def run_scheduled_print(filepath: str, use_ams: bool, ams_slot: int, timelapse: bool):
    """Background job for scheduled prints."""
    try:
        fname = upload_to_printer(Path(filepath))
        start_print(fname, use_ams, ams_slot, timelapse)
        app.logger.info(f"Scheduled print started: {fname}")
    except Exception as e:
        app.logger.error(f"Scheduled print failed: {e}")


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
    return render_template("index.html", files=filenames, jobs=jobs)


@app.route("/api/status")
def api_status():
    data = mqtt_query(timeout=5)
    return jsonify(data)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    if not f.filename or not f.filename.endswith(".3mf"):
        return jsonify({"error": "File must be .3mf"}), 400

    dest = UPLOAD_DIR / f.filename
    f.save(dest)

    try:
        validate_3mf(dest)
    except (ValueError, zipfile.BadZipFile) as e:
        dest.unlink()
        return jsonify({"error": str(e)}), 400

    return jsonify({"ok": True, "filename": f.filename})


@app.route("/api/print", methods=["POST"])
def api_print():
    data = request.json
    filename = data.get("filename")
    if not filename:
        return jsonify({"error": "filename required"}), 400

    filepath = UPLOAD_DIR / filename
    if not filepath.exists():
        return jsonify({"error": "File not found"}), 404

    use_ams = data.get("use_ams", False)
    ams_slot = int(data.get("ams_slot", 0))
    timelapse = data.get("timelapse", False)
    schedule_time = data.get("schedule_time")

    if schedule_time:
        try:
            run_at = datetime.fromisoformat(schedule_time)
        except ValueError:
            return jsonify({"error": "Invalid date format"}), 400

        if run_at <= datetime.now():
            return jsonify({"error": "Date is in the past"}), 400

        job = scheduler.add_job(
            run_scheduled_print,
            "date",
            run_date=run_at,
            args=[str(filepath), use_ams, ams_slot, timelapse],
            name=filename,
        )
        save_jobs()
        return jsonify({"ok": True, "scheduled": str(run_at), "job_id": job.id})

    # Print now
    try:
        fname = upload_to_printer(filepath)
        start_print(fname, use_ams, ams_slot, timelapse)
        return jsonify({"ok": True, "message": "Print started"})
    except Exception as e:
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
    for tray in printer_state.get("ams_trays", []):
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


@app.route("/api/cancel-job", methods=["POST"])
def api_cancel_job():
    job_id = request.json.get("job_id")
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
    log_file = PROJECT_DIR / "bambu-scheduler.log"
    return jsonify({"path": str(log_file)})


@app.route("/api/reload-config", methods=["POST"])
def api_reload_config():
    _load_config()
    return jsonify({"ok": True, "printer_ip": PRINTER_IP, "printer_name": PRINTER_NAME})


@app.route("/api/delete-file", methods=["POST"])
def api_delete_file():
    filename = request.json.get("filename")
    if not filename:
        return jsonify({"error": "filename required"}), 400
    filepath = UPLOAD_DIR / filename
    if filepath.exists():
        filepath.unlink()
    return jsonify({"ok": True})


if __name__ == "__main__":
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    with app.app_context():
        load_jobs()
    app.run(host="0.0.0.0", port=8080, debug=False)
