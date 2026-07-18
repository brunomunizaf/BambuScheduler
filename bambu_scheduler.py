#!/usr/bin/env python3
"""BambuScheduler CLI - Schedule .3mf prints at specific times.
100% local control via MQTT + FTP (no cloud).
"""

import os
import ssl
import json
import time
import zipfile
import logging
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

import paho.mqtt.client as mqtt
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bambu-scheduler")

PRINTER_IP = os.getenv("PRINTER_IP")
ACCESS_CODE = os.getenv("PRINTER_ACCESS_CODE")
SERIAL = os.getenv("PRINTER_SERIAL")
MQTT_PORT = 8883
FTP_PORT = 990


def validate_3mf(filepath: Path):
    """Validate that the .3mf contains sliced gcode."""
    try:
        with zipfile.ZipFile(filepath) as z:
            names = z.namelist()
            if "Metadata/plate_1.gcode" not in names:
                raise ValueError(
                    f"Arquivo nao contem gcode (nao foi fatiado).\n"
                    f"  Abra no Bambu Studio, fatie, e exporte como .3mf fatiado."
                )
    except zipfile.BadZipFile:
        raise ValueError("Arquivo .3mf invalido (nao e um ZIP valido)")


def upload_file(filepath: Path) -> str:
    """Upload .3mf to the printer via curl FTPS (FTP root, not /model/)."""
    filename = filepath.name
    log.info(f"Uploading {filename} para {PRINTER_IP}:{FTP_PORT} ...")

    result = subprocess.run(
        [
            "curl", "--ssl-reqd", "--insecure",
            "--user", f"bblp:{ACCESS_CODE}",
            "-T", str(filepath),
            f"ftps://{PRINTER_IP}:{FTP_PORT}/{filename}",
            "--connect-timeout", "15",
            "--max-time", "300",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"FTP upload falhou (exit {result.returncode}): {result.stderr}")

    log.info(f"Upload concluido: {filename}")
    return filename


def send_print_command(filename: str, opts: dict):
    """Send print command via local MQTT (port 8883)."""
    log.info(f"Enviando comando de impressao via MQTT local...")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"bambu-sched-{int(time.time())}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set("bblp", ACCESS_CODE)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ctx)

    result = {"done": False, "error": None}

    def on_connect(cli, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("Conectado ao MQTT local")
            ams_mapping = opts.get("ams_mapping")
            # Build 5-element ams_mapping array with -1 padding (per OpenBambuAPI spec)
            if ams_mapping:
                padded = [-1] * (5 - len(ams_mapping)) + list(ams_mapping)
            else:
                padded = ""
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
                    "timelapse": opts.get("timelapse", False),
                    "bed_levelling": opts.get("bed_leveling", True),
                    "flow_cali": opts.get("flow_cali", True),
                    "vibration_cali": opts.get("vibration_cali", True),
                    "layer_inspect": False,
                    "use_ams": bool(ams_mapping),
                    "ams_mapping": padded,
                }
            }
            topic = f"device/{SERIAL}/request"
            cli.publish(topic, json.dumps(cmd))
            log.info(f"Comando enviado em {topic}")
            result["done"] = True
        else:
            log.error(f"Falha MQTT: {reason_code}")
            result["error"] = str(reason_code)

    client.on_connect = on_connect
    client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
    client.loop_start()

    deadline = time.time() + 10
    while time.time() < deadline and not result["done"] and not result["error"]:
        time.sleep(0.3)

    client.loop_stop()
    client.disconnect()

    if result["error"]:
        raise RuntimeError(f"MQTT error: {result['error']}")
    if not result["done"]:
        raise TimeoutError("MQTT timeout - impressora offline?")

    log.info("Comando de impressao enviado com sucesso!")


def query_ams() -> list[dict]:
    """Query AMS tray info via local MQTT. Returns list of trays with slot, type, color."""
    log.info("Consultando AMS...")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"bambu-ams-{int(time.time())}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set("bblp", ACCESS_CODE)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ctx)

    trays = []
    done = {"received": False}

    def on_connect(cli, userdata, flags, reason_code, properties):
        if reason_code == 0:
            cli.subscribe(f"device/{SERIAL}/report")
            cmd = {"pushing": {"sequence_id": "0", "command": "pushall"}}
            cli.publish(f"device/{SERIAL}/request", json.dumps(cmd))

    def on_message(cli, userdata, msg):
        try:
            data = json.loads(msg.payload)
            ams_data = None
            if "print" in data and "ams" in data["print"]:
                ams_data = data["print"]["ams"]
            elif "ams" in data:
                ams_data = data["ams"]
            if not ams_data:
                return

            ams_units = ams_data.get("ams", [])
            for unit in ams_units:
                unit_id = int(unit.get("id", 0))
                for tray in unit.get("tray", []):
                    tray_id = int(tray.get("id", 0))
                    slot = unit_id * 4 + tray_id
                    filament_type = tray.get("tray_type", "")
                    color_hex = tray.get("tray_color", "")
                    trays.append({
                        "slot": slot,
                        "type": filament_type,
                        "color": color_hex[:6] if color_hex else "",
                        "empty": not filament_type,
                    })
            done["received"] = True
        except (json.JSONDecodeError, KeyError):
            pass

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
    client.loop_start()

    deadline = time.time() + 10
    while time.time() < deadline and not done["received"]:
        time.sleep(0.3)

    client.loop_stop()
    client.disconnect()

    return sorted(trays, key=lambda t: t["slot"])


def hex_to_color_name(hex_color: str) -> str:
    """Rough hex color to name for display."""
    if not hex_color:
        return "?"
    try:
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    except ValueError:
        return hex_color
    if r > 200 and g < 80 and b < 80:
        return "Vermelho"
    if r < 80 and g > 200 and b < 80:
        return "Verde"
    if r < 80 and g < 80 and b > 200:
        return "Azul"
    if r > 200 and g > 200 and b < 80:
        return "Amarelo"
    if r > 200 and g > 200 and b > 200:
        return "Branco"
    if r < 50 and g < 50 and b < 50:
        return "Preto"
    if r > 200 and g > 100 and b < 80:
        return "Laranja"
    return f"#{hex_color}"


def prompt_ams_mapping() -> list[int] | None:
    """Interactive prompt to select AMS slots for printing."""
    trays = query_ams()
    if not trays:
        log.warning("AMS nao detectada ou sem resposta")
        return None

    print("\n=== Slots da AMS ===")
    for t in trays:
        status = "VAZIO" if t["empty"] else f"{t['type']} ({hex_to_color_name(t['color'])})"
        print(f"  Slot {t['slot']}: {status}")
    print()

    while True:
        raw = input("Quais slots usar? (ex: 0 para 1 cor, 0,2 para 2 cores, Enter para slot 0): ").strip()
        if not raw:
            return [0]
        try:
            slots = [int(s.strip()) for s in raw.split(",")]
            valid_slots = [t["slot"] for t in trays if not t["empty"]]
            invalid = [s for s in slots if s not in valid_slots]
            if invalid:
                print(f"  Slots invalidos ou vazios: {invalid}. Tente novamente.")
                continue
            return slots
        except ValueError:
            print("  Formato invalido. Use numeros separados por virgula (ex: 0,1)")


def check_status():
    """Check printer status via local MQTT."""
    log.info(f"Verificando status de {PRINTER_IP}...")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"bambu-status-{int(time.time())}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set("bblp", ACCESS_CODE)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ctx)

    status = {"received": False}

    def on_connect(cli, userdata, flags, reason_code, properties):
        if reason_code == 0:
            cli.subscribe(f"device/{SERIAL}/report")
            cmd = {"pushing": {"sequence_id": "0", "command": "pushall"}}
            cli.publish(f"device/{SERIAL}/request", json.dumps(cmd))
            log.info("Aguardando status...")
        else:
            log.error(f"Falha ao conectar: {reason_code}")

    def on_message(cli, userdata, msg):
        try:
            data = json.loads(msg.payload)
            if "print" in data:
                p = data["print"]
                state = p.get("gcode_state", p.get("mc_print_stage", "unknown"))
                progress = p.get("mc_percent", "?")
                remaining = p.get("mc_remaining_time", "?")
                log.info(f"  Estado: {state}")
                log.info(f"  Progresso: {progress}%")
                if remaining != "?" and remaining:
                    log.info(f"  Tempo restante: {remaining} min")
                nozzle = p.get("nozzle_temper", "?")
                bed = p.get("bed_temper", "?")
                log.info(f"  Nozzle: {nozzle}C | Bed: {bed}C")
                status["received"] = True
        except (json.JSONDecodeError, KeyError):
            pass

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(PRINTER_IP, MQTT_PORT, keepalive=60)
    client.loop_start()

    deadline = time.time() + 10
    while time.time() < deadline and not status["received"]:
        time.sleep(0.3)

    client.loop_stop()
    client.disconnect()

    if not status["received"]:
        log.warning("Sem resposta (impressora offline ou serial incorreto)")


def run_print_job(filepath: str, opts: dict):
    """Execute print job: upload + print command."""
    log.info("=" * 50)
    log.info("INICIANDO JOB DE IMPRESSAO")
    log.info(f"Arquivo: {filepath}")
    log.info(f"Hora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 50)

    filename = upload_file(Path(filepath))
    send_print_command(filename, opts)

    log.info("Job concluido com sucesso!")


def main():
    parser = argparse.ArgumentParser(
        description="Bambu Lab A1 Print Scheduler (Local)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemplos:
  %(prog)s modelo.3mf "2026-07-13T02:00:00"    Agenda para 2h da manha
  %(prog)s modelo.3mf now                        Imprime agora
  %(prog)s --status                              Verifica status
        """,
    )
    parser.add_argument("file", nargs="?", help="Caminho do arquivo .3mf")
    parser.add_argument("timestamp", nargs="?", help="Quando imprimir (ISO format ou 'now')")
    parser.add_argument("--status", action="store_true", help="Verificar status da impressora")
    parser.add_argument("--timelapse", action="store_true", help="Ativar timelapse")
    parser.add_argument("--no-bed-leveling", action="store_true")
    parser.add_argument("--no-flow-cali", action="store_true")
    parser.add_argument("--no-vibration-cali", action="store_true")
    parser.add_argument("--no-ams", action="store_true", help="Pular selecao de AMS (usa filamento externo)")
    args = parser.parse_args()

    missing = [v for v in ["PRINTER_IP", "PRINTER_ACCESS_CODE", "PRINTER_SERIAL"] if not os.getenv(v)]
    if missing:
        log.error(f"Faltam no .env: {', '.join(missing)}")
        log.error("Copie .env.example para .env e preencha")
        return 1

    if args.status:
        check_status()
        return 0

    if not args.file or not args.timestamp:
        parser.error("file e timestamp sao obrigatorios (ou use --status)")

    filepath = Path(args.file).resolve()
    if not filepath.exists():
        log.error(f"Arquivo nao encontrado: {filepath}")
        return 1
    if filepath.suffix.lower() not in (".3mf",):
        log.error("Arquivo deve ser .3mf (fatiado no Bambu Studio)")
        return 1

    try:
        validate_3mf(filepath)
    except ValueError as e:
        log.error(str(e))
        return 1

    # Interactive AMS slot selection
    ams_mapping = None
    if not args.no_ams:
        ams_mapping = prompt_ams_mapping()
        if ams_mapping:
            log.info(f"AMS mapping: {ams_mapping}")

    opts = {
        "timelapse": args.timelapse,
        "bed_leveling": not args.no_bed_leveling,
        "flow_cali": not args.no_flow_cali,
        "vibration_cali": not args.no_vibration_cali,
        "ams_mapping": ams_mapping,
    }

    if args.timestamp.lower() == "now":
        run_print_job(str(filepath), opts)
    else:
        try:
            run_at = datetime.fromisoformat(args.timestamp)
        except ValueError:
            log.error(f"Formato invalido: {args.timestamp}")
            log.error("Use: 2026-07-13T02:00:00")
            return 1

        now = datetime.now()
        if run_at <= now:
            log.error(f"Timestamp {run_at} ja passou!")
            return 1

        delta = run_at - now
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes = remainder // 60
        log.info(f"Impressao agendada para: {run_at.strftime('%Y-%m-%d %H:%M:%S')}")
        log.info(f"Faltam: {hours}h {minutes}min")
        log.info("Pressione Ctrl+C para cancelar")

        scheduler = BlockingScheduler()
        scheduler.add_job(
            run_print_job,
            "date",
            run_date=run_at,
            args=[str(filepath), opts],
        )

        try:
            scheduler.start()
        except KeyboardInterrupt:
            log.info("Agendamento cancelado")
            scheduler.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
