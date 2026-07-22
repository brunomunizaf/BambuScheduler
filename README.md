# BambuScheduler

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
- A Bambu Lab printer on the same network (tested with A1, should work with X1C, P1S, etc.)

## Install

1. Download the latest `BambuScheduler.zip` from the [Releases page](https://github.com/brunomunizaf/BambuScheduler/releases)
2. Unzip it and drag `BambuScheduler.app` to `/Applications`
3. Open it.

### First launch: getting past Gatekeeper

BambuScheduler isn't signed with a paid Apple Developer ID, so on first launch macOS blocks it with a message like **"Apple could not verify BambuScheduler is free of malware."** This is expected for any app distributed outside the App Store — here's how to allow it:

1. When the **"BambuScheduler Not Opened"** dialog appears, click **Done** (⚠️ *not* "Move to Trash").
2. Open **System Settings › Privacy & Security** and scroll down to the **Security** section.
3. You'll see *"BambuScheduler was blocked to protect your Mac."* — click **Open Anyway**.
4. Confirm with **Open Anyway** again and authenticate with Touch ID or your password.

You only need to do this once. After that it opens normally.

> On older macOS you could instead right-click the app and choose **Open**, but on macOS Sequoia (15) and later that option is gone — use the Privacy & Security steps above.
>
> Prefer the terminal? Run `xattr -d com.apple.quarantine /Applications/BambuScheduler.app` and then open the app normally.

No Python install or extra setup needed — the app bundles its own backend.

On first launch, you'll see a setup screen where you enter your printer's:

- **IP address** — Found in Settings > Network on the printer
- **Access Code** — Found in Settings > LAN on the printer
- **Serial Number** — Found in Settings > Device Info or on the printer's sticker
- **Name** (optional) — A custom name for your printer

Settings are saved to `~/Library/Application Support/BambuScheduler/config.json`.

## Build from source

Only needed if you want to develop or build the app yourself instead of downloading a release.

```bash
git clone https://github.com/brunomunizaf/BambuScheduler.git
cd BambuScheduler
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./scripts/build_release.sh
open release/BambuScheduler.app
```

The script bundles the Python backend with PyInstaller, builds the Swift menu bar app, assembles `release/BambuScheduler.app`, ad-hoc code-signs it, and zips it to `release/BambuScheduler.zip`.

For quick iteration on the backend alone without rebuilding the whole app, you can also run it directly:

```bash
python3 web.py
```

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
- **Flask** (port 8080) — Serves the web UI and API, bundled into the app with PyInstaller
- **SwiftUI MenuBarExtra** — Native macOS menu bar widget, launches the bundled backend as a subprocess on startup and stops it on Quit
- **APScheduler** — Handles timed print scheduling with persistence

### Running the backend as a login service (optional)

If you'd rather run the Flask backend as a persistent background service instead of through the menu bar app (e.g. for headless use), `com.bambu.scheduler.plist` is provided as a launchd template. Edit the paths inside it to match your setup, then:

```bash
cp com.bambu.scheduler.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.bambu.scheduler.plist
```

## License

MIT
