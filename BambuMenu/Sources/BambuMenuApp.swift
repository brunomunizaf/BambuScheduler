import SwiftUI
import IOKit.pwr_mgt

// MARK: - Config

struct PrinterConfig: Codable {
    var printerIP: String
    var accessCode: String
    var serial: String
    var printerName: String
    // Optional so configs written before localization still decode; nil == "en".
    var language: String?

    static let configDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/BambuScheduler")
    static let configFile = configDir.appendingPathComponent("config.json")

    static func load() -> PrinterConfig? {
        guard let data = try? Data(contentsOf: configFile),
              let config = try? JSONDecoder().decode(PrinterConfig.self, from: data) else {
            return nil
        }
        return config
    }

    func save() throws {
        try FileManager.default.createDirectory(at: Self.configDir, withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(self)
        try data.write(to: Self.configFile)
    }

    var isValid: Bool {
        !printerIP.isEmpty && !accessCode.isEmpty && !serial.isEmpty
    }
}

// MARK: - App

@main
struct BambuSchedulerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var vm = PrinterViewModel()

    init() {
        ServiceManager.startBackend()
    }

    var body: some Scene {
        MenuBarExtra {
            MenuContent(vm: vm)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: vm.menuBarIcon)
                if vm.status.gcode_state == "RUNNING" {
                    Text("\(vm.status.progress)%")
                }
            }
        }
        .menuBarExtraStyle(.window)
    }
}

// MARK: - App Delegate

/// Ensures the backend is stopped whenever the app terminates — not just when
/// the user clicks Quit (also Cmd-Q, logout, and Dock quit go through here).
/// Without this, a leftover backend keeps holding port 8080 and the next launch
/// can't bind, leaving the menu bar app silently non-functional.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationWillTerminate(_ notification: Notification) {
        ServiceManager.stopBackend()
    }
}

// MARK: - Service Manager

/// Runs the bundled Python backend (packaged with PyInstaller) as a child
/// process for the lifetime of the app, instead of relying on a launchd
/// service with hardcoded paths. This keeps the packaged .app self-contained
/// and installable by just dragging it to /Applications.
enum ServiceManager {
    private static var backendProcess: Process?

    /// PyInstaller --onedir layout: the executable lives in its own folder
    /// alongside an `_internal` directory inside Contents/Resources.
    private static var backendExecutableURL: URL? {
        Bundle.main.resourceURL?
            .appendingPathComponent("bambuscheduler-backend", isDirectory: true)
            .appendingPathComponent("bambuscheduler-backend")
    }

    static func startBackend() {
        guard backendProcess == nil else { return }

        // Clean up any backend orphaned by a previous force-quit/crash so it
        // doesn't keep port 8080 and block the fresh one from binding.
        terminateStaleBackends()

        guard let backendURL = backendExecutableURL,
              FileManager.default.isExecutableFile(atPath: backendURL.path) else {
            NSLog("BambuScheduler: bundled backend executable not found")
            return
        }

        let process = Process()
        process.executableURL = backendURL
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice

        do {
            try process.run()
            backendProcess = process
        } catch {
            NSLog("BambuScheduler: failed to start backend: \(error)")
        }
    }

    static func stopBackend() {
        backendProcess?.terminate()
        backendProcess = nil
    }

    /// Kill any stray backend process by name. Safe to call before spawning our
    /// own: the backend binary is "bambuscheduler-backend", distinct from this
    /// app's "BambuScheduler" executable, so this never targets the app itself.
    private static func terminateStaleBackends() {
        let pkill = Process()
        pkill.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        pkill.arguments = ["-f", "bambuscheduler-backend"]
        pkill.standardOutput = FileHandle.nullDevice
        pkill.standardError = FileHandle.nullDevice
        try? pkill.run()
        pkill.waitUntilExit()
    }
}

// MARK: - Power Manager

/// Prevents the Mac from idle-sleeping while a scheduled print is pending, so
/// the app is still running to fire it at the start time. Uses
/// PreventUserIdleSystemSleep, which keeps the *system* awake but lets the
/// display sleep normally — the screen still goes dark. The assertion is
/// released as soon as no jobs remain, and the OS releases it automatically if
/// the app quits.
enum PowerManager {
    private static var assertionID: IOPMAssertionID = 0
    private static var held = false

    static func setKeepAwake(_ on: Bool) {
        if on && !held {
            let reason = "A scheduled print is waiting to start" as CFString
            let result = IOPMAssertionCreateWithName(
                kIOPMAssertionTypePreventUserIdleSystemSleep as CFString,
                IOPMAssertionLevel(kIOPMAssertionLevelOn),
                reason,
                &assertionID)
            held = (result == kIOReturnSuccess)
        } else if !on && held {
            IOPMAssertionRelease(assertionID)
            assertionID = 0
            held = false
        }
    }
}

// MARK: - Menu Content

struct MenuContent: View {
    @ObservedObject var vm: PrinterViewModel

    var body: some View {
        if vm.needsSetup || vm.showingSettings {
            SetupView(vm: vm)
        } else {
            PrinterMenuView(vm: vm)
        }
    }
}

// MARK: - Setup View

struct SetupView: View {
    @ObservedObject var vm: PrinterViewModel
    @State private var ip = ""
    @State private var accessCode = ""
    @State private var serial = ""
    @State private var name = ""
    @State private var language = "en"
    @State private var errorMsg = ""
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text(L("Printer Setup", language))
                    .font(.headline)
                Text(L("Find this info on your printer's display", language))
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            .padding(.horizontal, 16)
            .padding(.top, 12)
            .padding(.bottom, 10)

            Divider()

            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(L("Printer IP", language))
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("192.168.1.100", text: $ip)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text(L("Access Code", language))
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("12345678", text: $accessCode)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text(L("Serial Number", language))
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("01A00A000000000", text: $serial)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text(L("Name (optional)", language))
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("My Printer", text: $name)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text(L("Language", language))
                        .font(.caption)
                        .foregroundColor(.secondary)
                    Picker("", selection: $language) {
                        ForEach(supportedLanguages, id: \.code) { lang in
                            Text(lang.name).tag(lang.code)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                    .font(.caption)
                }

                if !errorMsg.isEmpty {
                    Text(errorMsg)
                        .font(.caption)
                        .foregroundColor(.red)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)

            Divider()

            HStack {
                if vm.showingSettings {
                    Button(L("Cancel", language)) {
                        vm.showingSettings = false
                    }
                }
                Spacer()
                Button(action: saveConfig) {
                    if saving {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Text(L("Save", language))
                    }
                }
                .disabled(ip.isEmpty || accessCode.isEmpty || serial.isEmpty || saving)
                .buttonStyle(.borderedProminent)
                .tint(.bambu)
                .controlSize(.small)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
        }
        .frame(width: 300)
        .onAppear {
            if let config = PrinterConfig.load() {
                ip = config.printerIP
                accessCode = config.accessCode
                serial = config.serial
                name = config.printerName
                language = config.language ?? "en"
            }
        }
    }

    private func saveConfig() {
        let config = PrinterConfig(
            printerIP: ip.trimmingCharacters(in: .whitespaces),
            accessCode: accessCode.trimmingCharacters(in: .whitespaces),
            serial: serial.trimmingCharacters(in: .whitespaces),
            printerName: name.trimmingCharacters(in: .whitespaces),
            language: language
        )
        do {
            try config.save()
            errorMsg = ""
            saving = true
            vm.language = language
            Task {
                await vm.reloadBackendConfig()
                saving = false
                vm.showingSettings = false
                vm.needsSetup = false
                vm.printerName = config.printerName.isEmpty ? "Bambu Lab" : config.printerName
                vm.refresh()
            }
        } catch {
            errorMsg = L("Failed to save: ", language) + error.localizedDescription
        }
    }
}

// MARK: - Printer Menu View

struct PrinterMenuView: View {
    @ObservedObject var vm: PrinterViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(vm.printerName)
                        .font(.headline)
                    StatusBadge(state: vm.status.gcode_state, stgCur: vm.status.stg_cur, lang: vm.language)
                }
                Spacer()
                HStack(spacing: 12) {
                    Button(action: { vm.showingSettings = true }) {
                        Image(systemName: "gearshape")
                    }
                    Button(action: vm.openLogs) {
                        Image(systemName: "doc.text")
                    }
                    Button(action: vm.refresh) {
                        Image(systemName: "arrow.clockwise")
                    }
                    .disabled(vm.loading)
                }
                .buttonStyle(.plain)
                .foregroundColor(.secondary)
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 10)

            Divider()

            // Temperatures
            HStack(spacing: 0) {
                VStack(spacing: 2) {
                    Text(L("Nozzle", vm.language))
                        .font(.caption2)
                        .foregroundColor(.secondary)
                    Text("\(vm.status.nozzle_temp, specifier: "%.0f")°C")
                        .font(.system(.caption, design: .monospaced).weight(.medium))
                }
                .frame(maxWidth: .infinity)

                Divider().frame(height: 30)

                VStack(spacing: 2) {
                    Text(L("Bed", vm.language))
                        .font(.caption2)
                        .foregroundColor(.secondary)
                    Text("\(vm.status.bed_temp, specifier: "%.0f")°C")
                        .font(.system(.caption, design: .monospaced).weight(.medium))
                }
                .frame(maxWidth: .infinity)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)

            // Error
            if !vm.status.error_msg.isEmpty {
                Text(vm.status.error_msg)
                    .font(.caption)
                    .foregroundColor(.stateFail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(6)
                    .background(Color.stateFail.opacity(0.1))
                    .cornerRadius(6)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 6)
            }

            // Print progress
            if vm.status.gcode_state == "RUNNING" || vm.status.gcode_state == "PAUSE" {
                Divider()
                VStack(spacing: 6) {
                    if !vm.status.subtask_name.isEmpty {
                        Text(vm.status.subtask_name)
                            .font(.caption)
                            .lineLimit(1)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    ProgressView(value: Double(vm.status.progress), total: 100)
                        .tint(vm.status.gcode_state == "PAUSE" ? .statePause : .bambu)
                    HStack {
                        Text("\(vm.status.progress)%")
                        Spacer()
                        if vm.status.remaining_time > 0 {
                            let h = vm.status.remaining_time / 60
                            let m = vm.status.remaining_time % 60
                            let left = L("left", vm.language)
                            Text(h > 0 ? "\(h)h \(m)m \(left)" : "\(m)m \(left)")
                        }
                    }
                    .font(.caption)
                    .foregroundColor(.secondary)

                    HStack(spacing: 8) {
                        if vm.status.gcode_state == "RUNNING" {
                            Button(L("Pause", vm.language)) { vm.pause() }
                        } else {
                            Button(L("Resume", vm.language)) { vm.resume() }
                        }
                        Spacer()
                        Button(L("Abort", vm.language)) { vm.stop() }
                            .foregroundColor(.stateFail)
                    }
                    .font(.caption)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            }

            // Scheduled Jobs
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                Text(L("SCHEDULE", vm.language))
                    .font(.caption2)
                    .foregroundColor(.secondary)
                    .tracking(1)

                if vm.jobs.isEmpty {
                    Text(L("No scheduled prints", vm.language))
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .padding(.vertical, 4)
                } else {
                    let todayJobs = vm.jobs.filter { $0.isToday }
                    let laterJobs = vm.jobs.filter { !$0.isToday }

                    if !todayJobs.isEmpty {
                        Text(L("Today", vm.language))
                            .font(.caption2.weight(.semibold))
                            .foregroundColor(.bambu)
                            .padding(.top, 2)
                        ForEach(todayJobs) { job in
                            JobRow(job: job, onCancel: { vm.cancelJob(id: job.id) })
                        }
                    }

                    if !laterJobs.isEmpty {
                        Text(L("Upcoming", vm.language))
                            .font(.caption2.weight(.semibold))
                            .foregroundColor(.secondary)
                            .padding(.top, todayJobs.isEmpty ? 2 : 6)
                        ForEach(laterJobs) { job in
                            JobRow(job: job, onCancel: { vm.cancelJob(id: job.id) })
                        }
                    }
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)

            Divider()

            // Footer
            HStack {
                Button(L("Open Web UI", vm.language)) {
                    NSWorkspace.shared.open(URL(string: "http://localhost:8080")!)
                }
                Spacer()
                Button(L("Quit", vm.language)) {
                    ServiceManager.stopBackend()
                    NSApplication.shared.terminate(nil)
                }
            }
            .font(.caption)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
        }
        .frame(width: 300)
    }
}

struct StatusBadge: View {
    let state: String
    var stgCur = -1
    var lang = "en"

    var color: Color {
        switch state {
        case "FINISH": return .bambu
        case "IDLE": return .stateIdle
        case "RUNNING": return .stateRun
        case "PREPARE": return .statePrepare
        case "PAUSE": return .statePause
        case "FAILED": return .stateFail
        default: return .stateIdle
        }
    }

    var label: String {
        // While printing/paused, prefer the fine-grained stage (e.g. "Changing
        // filament") over the coarse "Printing"; fall back when it's just printing.
        if state == "RUNNING" || state == "PAUSE",
           let stage = (stageLabelsByLang[lang] ?? stageLabelsByLang["en"])?[stgCur] {
            return stage
        }
        switch state {
        case "IDLE": return L("Idle", lang)
        case "FINISH": return L("Finished", lang)
        case "RUNNING": return L("Printing", lang)
        case "PREPARE": return L("Preparing", lang)
        case "PAUSE": return L("Paused", lang)
        case "FAILED": return L("Error", lang)
        default: return state
        }
    }

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
            Text(label)
                .font(.subheadline.weight(.medium))
        }
    }
}

// MARK: - Models

struct PrinterStatus {
    var gcode_state = "unknown"
    var progress = 0
    var remaining_time = 0
    var nozzle_temp = 0.0
    var bed_temp = 0.0
    var subtask_name = ""
    var error_msg = ""
    /// Fine-grained current stage (heating, changing filament, calibrating, …).
    /// -1 means no special stage — just normal printing.
    var stg_cur = -1
}

// MARK: - Localization

/// Languages offered in the app settings. Codes match the shared config.json.
let supportedLanguages: [(code: String, name: String)] = [
    ("en", "English"),
    ("pt-BR", "Português (Brasil)"),
    ("es", "Español"),
]

/// UI string table keyed by language then by English key. Missing keys fall
/// back to the English string (and finally the key itself).
let translations: [String: [String: String]] = [
    "en": [:],  // English keys are their own values; handled by L(...) fallback.
    "pt-BR": [
        "Printer Setup": "Configuração da impressora",
        "Find this info on your printer's display": "Encontre estas informações na tela da impressora",
        "Printer IP": "IP da impressora",
        "Access Code": "Código de acesso",
        "Serial Number": "Número de série",
        "Name (optional)": "Nome (opcional)",
        "Language": "Idioma",
        "Cancel": "Cancelar",
        "Save": "Salvar",
        "Failed to save: ": "Falha ao salvar: ",
        "Nozzle": "Bico",
        "Bed": "Mesa",
        "Pause": "Pausar",
        "Resume": "Retomar",
        "Abort": "Cancelar impressão",
        "SCHEDULE": "AGENDA",
        "No scheduled prints": "Nenhuma impressão agendada",
        "Today": "Hoje",
        "Upcoming": "Próximas",
        "Open Web UI": "Abrir Web UI",
        "Quit": "Sair",
        "left": "restantes",
        "Idle": "Ocioso",
        "Finished": "Concluído",
        "Printing": "Imprimindo",
        "Preparing": "Preparando",
        "Paused": "Pausado",
        "Error": "Erro",
    ],
    "es": [
        "Printer Setup": "Configuración de la impresora",
        "Find this info on your printer's display": "Encuentra estos datos en la pantalla de tu impresora",
        "Printer IP": "IP de la impresora",
        "Access Code": "Código de acceso",
        "Serial Number": "Número de serie",
        "Name (optional)": "Nombre (opcional)",
        "Language": "Idioma",
        "Cancel": "Cancelar",
        "Save": "Guardar",
        "Failed to save: ": "Error al guardar: ",
        "Nozzle": "Boquilla",
        "Bed": "Cama",
        "Pause": "Pausar",
        "Resume": "Reanudar",
        "Abort": "Cancelar impresión",
        "SCHEDULE": "AGENDA",
        "No scheduled prints": "No hay impresiones programadas",
        "Today": "Hoy",
        "Upcoming": "Próximas",
        "Open Web UI": "Abrir Web UI",
        "Quit": "Salir",
        "left": "restantes",
        "Idle": "Inactivo",
        "Finished": "Terminado",
        "Printing": "Imprimiendo",
        "Preparing": "Preparando",
        "Paused": "Pausado",
        "Error": "Error",
    ],
]

/// Look up a UI string for the given language, falling back to English.
func L(_ key: String, _ lang: String) -> String {
    translations[lang]?[key] ?? key
}

/// Human labels for the printer's current-stage codes (stg_cur), per language.
/// Codes not listed (-1, 0) mean normal printing and fall back to the state label.
let stageLabelsByLang: [String: [Int: String]] = [
    "en": [
        1: "Auto bed leveling", 2: "Preheating bed", 3: "Sweeping XY",
        4: "Changing filament", 5: "Paused (M400)", 6: "Paused — filament runout",
        7: "Heating nozzle", 8: "Calibrating extrusion", 9: "Scanning bed surface",
        10: "Inspecting first layer", 11: "Identifying build plate", 12: "Calibrating LiDAR",
        13: "Homing toolhead", 14: "Cleaning nozzle", 15: "Checking nozzle temp",
        16: "Paused by user", 17: "Paused — front cover open", 18: "Calibrating LiDAR",
        19: "Calibrating flow", 20: "Paused — nozzle temp error", 21: "Paused — bed temp error",
        22: "Unloading filament", 23: "Paused — skip step", 24: "Loading filament",
        25: "Calibrating motor noise", 26: "Paused — AMS disconnected",
        27: "Paused — heat-break fan slow", 28: "Paused — chamber temp error",
        29: "Cooling chamber", 30: "Paused — user G-code", 31: "Motor noise calibration",
        32: "Paused — nozzle covered", 33: "Paused — cutter error",
        34: "Paused — first-layer error", 35: "Paused — nozzle clog",
    ],
    "pt-BR": [
        1: "Nivelando a mesa", 2: "Preaquecendo a mesa", 3: "Varredura XY",
        4: "Trocando filamento", 5: "Pausado (M400)", 6: "Pausado — fim do filamento",
        7: "Aquecendo o bico", 8: "Calibrando extrusão", 9: "Escaneando a mesa",
        10: "Inspecionando 1ª camada", 11: "Identificando a mesa", 12: "Calibrando LiDAR",
        13: "Referenciando o cabeçote", 14: "Limpando o bico", 15: "Verificando temp. do bico",
        16: "Pausado pelo usuário", 17: "Pausado — tampa aberta", 18: "Calibrando LiDAR",
        19: "Calibrando o fluxo", 20: "Pausado — erro temp. do bico", 21: "Pausado — erro temp. da mesa",
        22: "Descarregando filamento", 23: "Pausado — pular passo", 24: "Carregando filamento",
        25: "Calibrando ruído do motor", 26: "Pausado — AMS desconectado",
        27: "Pausado — ventoinha lenta", 28: "Pausado — erro temp. da câmara",
        29: "Resfriando a câmara", 30: "Pausado — G-code do usuário", 31: "Calibração de ruído",
        32: "Pausado — bico coberto", 33: "Pausado — erro do cortador",
        34: "Pausado — erro na 1ª camada", 35: "Pausado — bico entupido",
    ],
    "es": [
        1: "Nivelando la cama", 2: "Precalentando la cama", 3: "Barrido XY",
        4: "Cambiando filamento", 5: "Pausado (M400)", 6: "Pausado — sin filamento",
        7: "Calentando la boquilla", 8: "Calibrando extrusión", 9: "Escaneando la cama",
        10: "Inspeccionando 1ª capa", 11: "Identificando la cama", 12: "Calibrando LiDAR",
        13: "Referenciando el cabezal", 14: "Limpiando la boquilla", 15: "Verificando temp. boquilla",
        16: "Pausado por el usuario", 17: "Pausado — tapa abierta", 18: "Calibrando LiDAR",
        19: "Calibrando el flujo", 20: "Pausado — error temp. boquilla", 21: "Pausado — error temp. cama",
        22: "Descargando filamento", 23: "Pausado — omitir paso", 24: "Cargando filamento",
        25: "Calibrando ruido del motor", 26: "Pausado — AMS desconectado",
        27: "Pausado — ventilador lento", 28: "Pausado — error temp. cámara",
        29: "Enfriando la cámara", 30: "Pausado — G-code del usuario", 31: "Calibración de ruido",
        32: "Pausado — boquilla cubierta", 33: "Pausado — error del cortador",
        34: "Pausado — error 1ª capa", 35: "Pausado — boquilla obstruida",
    ],
]

struct ScheduledJob: Identifiable {
    let id: String
    let name: String
    let next_run: String
    let amsColor: String?

    var isToday: Bool {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        guard let date = formatter.date(from: next_run) else { return false }
        return Calendar.current.isDateInToday(date)
    }

    var timeOnly: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        guard let date = formatter.date(from: next_run) else { return next_run }
        let out = DateFormatter()
        out.dateFormat = "HH:mm"
        return out.string(from: date)
    }
}

struct JobRow: View {
    let job: ScheduledJob
    let onCancel: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            if let hex = job.amsColor, let color = Color(hex: hex) {
                Circle()
                    .fill(color)
                    .frame(width: 10, height: 10)
                    .overlay(Circle().stroke(Color.primary.opacity(0.2), lineWidth: 0.5))
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(job.name)
                    .font(.caption)
                    .lineLimit(1)
                Text(job.isToday ? job.timeOnly : job.next_run)
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
            Spacer()
            Button(action: onCancel) {
                Image(systemName: "xmark.circle.fill")
                    .foregroundColor(.secondary)
            }
            .buttonStyle(.plain)
        }
        .padding(.vertical, 2)
    }
}

extension Color {
    init?(hex: String) {
        let cleaned = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        guard cleaned.count >= 6,
              let val = UInt64(cleaned.prefix(6), radix: 16) else { return nil }
        let r = Double((val >> 16) & 0xFF) / 255.0
        let g = Double((val >> 8) & 0xFF) / 255.0
        let b = Double(val & 0xFF) / 255.0
        self.init(red: r, green: g, blue: b)
    }

    // Bambu-branded palette, matched to the web UI so both surfaces read as
    // one product.
    static let bambu = Color(hex: "00C489")!        // primary teal-green
    static let bambuBright = Color(hex: "3AE3A3")!  // filament highlight
    static let bambuAmber = Color(hex: "F6B44A")!   // time / scheduling accent

    // Printer state colors
    static let stateRun = Color(hex: "34E0A1")!
    static let statePrepare = Color(hex: "F6B44A")!
    static let statePause = Color(hex: "FF9D42")!
    static let stateFail = Color(hex: "FF5D5D")!
    static let stateIdle = Color(hex: "6FA592")!
}

// MARK: - ViewModel

@MainActor
class PrinterViewModel: ObservableObject {
    @Published var status = PrinterStatus()
    @Published var printerName = "Bambu Lab"
    @Published var jobs: [ScheduledJob] = []
    @Published var loading = false
    @Published var needsSetup: Bool
    @Published var showingSettings = false
    @Published var language = "en"

    private let baseURL = "http://localhost:8080"
    private var timer: Timer?

    @Published private var cubeFilled = false
    private var animationTimer: Timer?

    var menuBarIcon: String {
        if showingSettings || needsSetup {
            return "gearshape"
        }
        switch status.gcode_state {
        case "RUNNING": return "cube.fill"
        case "PAUSE": return "pause.circle"
        case "FAILED": return "exclamationmark.triangle"
        case "IDLE", "FINISH": return "cube.fill"
        default: return cubeFilled ? "cube.fill" : "cube"
        }
    }

    init() {
        let config = PrinterConfig.load()
        needsSetup = config == nil || !(config!.isValid)
        if let config, !config.printerName.isEmpty {
            printerName = config.printerName
        }
        if let lang = config?.language, !lang.isEmpty {
            language = lang
        }
        if !needsSetup {
            refresh()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in
            Task { @MainActor [weak self] in
                guard let self, !self.needsSetup, !self.showingSettings else { return }
                self.refresh()
            }
        }
        animationTimer = Timer.scheduledTimer(withTimeInterval: 0.8, repeats: true) { _ in
            Task { @MainActor [weak self] in
                guard let self, self.status.gcode_state == "unknown" else { return }
                self.cubeFilled.toggle()
            }
        }
    }

    func reloadBackendConfig() async {
        guard let url = URL(string: "\(baseURL)/api/reload-config") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        _ = try? await URLSession.shared.data(for: req)
    }

    func refresh() {
        loading = true
        Task {
            await fetchStatus()
            await fetchJobs()
            loading = false
        }
    }

    func stop() { Task { await postAction("/api/stop") } }
    func pause() { Task { await postAction("/api/pause") } }
    func resume() { Task { await postAction("/api/resume") } }

    func openLogs() {
        Task {
            guard let url = URL(string: "\(baseURL)/api/log-path") else { return }
            guard let (data, _) = try? await URLSession.shared.data(from: url),
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let path = json["path"] as? String else { return }
            let fileURL = URL(fileURLWithPath: path)
            NSWorkspace.shared.open(fileURL)
        }
    }

    func cancelJob(id: String) {
        Task {
            guard let url = URL(string: "\(baseURL)/api/cancel-job") else { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: ["job_id": id])
            _ = try? await URLSession.shared.data(for: req)
            refresh()
        }
    }

    private func fetchStatus() async {
        guard let url = URL(string: "\(baseURL)/api/status") else { return }
        guard let (data, _) = try? await URLSession.shared.data(from: url) else { return }
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        guard let s = json["status"] as? [String: Any] else { return }

        if let name = json["printer_name"] as? String, !name.isEmpty {
            printerName = name
        }

        status.gcode_state = s["gcode_state"] as? String ?? "unknown"
        status.progress = s["progress"] as? Int ?? 0
        status.remaining_time = s["remaining_time"] as? Int ?? 0
        status.nozzle_temp = s["nozzle_temp"] as? Double ?? 0
        status.bed_temp = s["bed_temp"] as? Double ?? 0
        status.subtask_name = s["subtask_name"] as? String ?? ""
        status.error_msg = s["error_msg"] as? String ?? ""
        status.stg_cur = s["stg_cur"] as? Int ?? -1
    }

    private func fetchJobs() async {
        guard let url = URL(string: "\(baseURL)/api/jobs") else { return }
        guard let (data, _) = try? await URLSession.shared.data(from: url) else { return }
        guard let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }

        jobs = arr.compactMap { j in
            guard let id = j["id"] as? String,
                  let name = j["name"] as? String,
                  let next = j["next_run"] as? String else { return nil }
            let color = j["ams_color"] as? String
            return ScheduledJob(id: id, name: name, next_run: next, amsColor: color)
        }

        // Keep the Mac awake while a scheduled print is waiting to fire, so it
        // doesn't sleep past the start time. The display is still allowed to
        // sleep — only the system stays awake.
        PowerManager.setKeepAwake(!jobs.isEmpty)
    }

    private func postAction(_ path: String) async {
        guard let url = URL(string: "\(baseURL)\(path)") else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = "{}".data(using: .utf8)
        _ = try? await URLSession.shared.data(for: req)
        try? await Task.sleep(nanoseconds: 2_000_000_000)
        refresh()
    }
}
