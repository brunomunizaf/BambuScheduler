# BambuTiming

A macOS menu bar app for scheduling and monitoring prints on Bambu Lab printers over LAN. No cloud required.

## Features

- **Menu bar status** — Live printer state, nozzle/bed temperatures, and print progress
- **Print scheduling** — Schedule .3mf prints for specific dates and times
- **Print control** — Pause, resume, and abort prints directly from the menu bar
- **AMS support** — Select filament slots and see colors in scheduled jobs
- **Web UI** — Full browser interface for uploading files and managing schedules
- **100% local** — Communicates directly with the printer via MQTT and FTP over LAN

## Requirements

- macOS 14.0+
- Python 3.9+
- A Bambu Lab printer on the same network (tested with A1, should work with X1C, P1S, etc.)

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/yourusername/bambu-timing.git
cd bambu-timing
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Build the menu bar app

```bash
cd BambuMenu
swift build -c release
```

### 3. Create the app bundle

```bash
mkdir -p BambuTiming.app/Contents/MacOS
mkdir -p BambuTiming.app/Contents/Resources
cp BambuMenu/.build/release/BambuTiming BambuTiming.app/Contents/MacOS/
```

Copy the provided `Info.plist` into `BambuTiming.app/Contents/` and optionally generate the icon:

```bash
python3 generate_icon.py
cp /tmp/AppIcon.icns BambuTiming.app/Contents/Resources/
```

### 4. Install the background service

Edit `com.bambu.timing.plist` and update the paths to match your install location, then:

```bash
cp com.bambu.timing.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.bambu.timing.plist
```

### 5. Launch

Open `BambuTiming.app`. On first launch, it will show a setup screen where you enter your printer's:

- **IP address** — Found in Settings > Network on the printer
- **Access Code** — Found in Settings > LAN on the printer
- **Serial Number** — Found in Settings > Device Info or on the printer's sticker
- **Name** (optional) — A custom name for your printer

Settings are saved to `~/Library/Application Support/BambuTiming/config.json`.

## Usage

### Menu bar

Click the cube icon in the menu bar to see:
- Printer status (Idle, Printing, Paused, Error)
- Nozzle and bed temperatures
- Print progress with time remaining
- Scheduled prints grouped by Today / Upcoming

### Web UI

Open `http://localhost:8080` in your browser (or click "Open Web UI" in the menu) to:
- Upload .3mf files
- Start prints immediately or schedule them
- Select AMS filament slots
- View and cancel scheduled jobs

### API

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | Printer status, temperatures, progress |
| `/api/jobs` | GET | List scheduled print jobs |
| `/api/upload` | POST | Upload a .3mf file |
| `/api/print` | POST | Start or schedule a print |
| `/api/stop` | POST | Abort current print |
| `/api/pause` | POST | Pause current print |
| `/api/resume` | POST | Resume paused print |
| `/api/cancel-job` | POST | Cancel a scheduled job |

## How it works

- **MQTT** (port 8883) — Queries printer status and sends print commands
- **FTPS** (port 990) — Uploads .3mf files to the printer's internal storage
- **Flask** (port 8080) — Serves the web UI and API
- **SwiftUI MenuBarExtra** — Native macOS menu bar widget
- **APScheduler** — Handles timed print scheduling with persistence

## License

MIT
