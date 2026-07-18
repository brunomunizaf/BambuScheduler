import SwiftUI

// MARK: - Config

struct PrinterConfig: Codable {
    var printerIP: String
    var accessCode: String
    var serial: String
    var printerName: String

    static let configDir = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/BambuTiming")
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
struct BambuTimingApp: App {
    @StateObject private var vm = PrinterViewModel()

    init() {
        ServiceManager.loadBackend()
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

// MARK: - Service Manager

enum ServiceManager {
    private static let plistPath = NSString("~/Library/LaunchAgents/com.bambu.timing.plist").expandingTildeInPath

    static func loadBackend() {
        let status = run("/bin/launchctl", arguments: ["list", "com.bambu.timing"])
        if status != 0 {
            run("/bin/launchctl", arguments: ["load", plistPath])
        }
    }

    static func unloadBackend() {
        run("/bin/launchctl", arguments: ["unload", plistPath])
    }

    @discardableResult
    private static func run(_ path: String, arguments: [String]) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = arguments
        try? process.run()
        process.waitUntilExit()
        return process.terminationStatus
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
    @State private var errorMsg = ""
    @State private var saving = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Printer Setup")
                    .font(.headline)
                Text("Find this info on your printer's display")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            .padding(.horizontal, 16)
            .padding(.top, 12)
            .padding(.bottom, 10)

            Divider()

            VStack(alignment: .leading, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Printer IP")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("192.168.1.100", text: $ip)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text("Access Code")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    SecureField("12345678", text: $accessCode)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text("Serial Number")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("01A00A000000000", text: $serial)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }

                VStack(alignment: .leading, spacing: 4) {
                    Text("Name (optional)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    TextField("My Printer", text: $name)
                        .textFieldStyle(.roundedBorder)
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
                    Button("Cancel") {
                        vm.showingSettings = false
                    }
                }
                Spacer()
                Button(action: saveConfig) {
                    if saving {
                        ProgressView()
                            .controlSize(.small)
                    } else {
                        Text("Save")
                    }
                }
                .disabled(ip.isEmpty || accessCode.isEmpty || serial.isEmpty || saving)
                .buttonStyle(.borderedProminent)
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
            }
        }
    }

    private func saveConfig() {
        let config = PrinterConfig(
            printerIP: ip.trimmingCharacters(in: .whitespaces),
            accessCode: accessCode.trimmingCharacters(in: .whitespaces),
            serial: serial.trimmingCharacters(in: .whitespaces),
            printerName: name.trimmingCharacters(in: .whitespaces)
        )
        do {
            try config.save()
            errorMsg = ""
            saving = true
            Task {
                await vm.reloadBackendConfig()
                saving = false
                vm.showingSettings = false
                vm.needsSetup = false
                vm.printerName = config.printerName.isEmpty ? "Bambu Lab" : config.printerName
                vm.refresh()
            }
        } catch {
            errorMsg = "Failed to save: \(error.localizedDescription)"
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
                    StatusBadge(state: vm.status.gcode_state)
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
                    Text("Nozzle")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                    Text("\(vm.status.nozzle_temp, specifier: "%.0f")°C")
                        .font(.system(.caption, design: .monospaced).weight(.medium))
                }
                .frame(maxWidth: .infinity)

                Divider().frame(height: 30)

                VStack(spacing: 2) {
                    Text("Bed")
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
                    .foregroundColor(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(6)
                    .background(Color.red.opacity(0.1))
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
                        .tint(vm.status.gcode_state == "PAUSE" ? .orange : .blue)
                    HStack {
                        Text("\(vm.status.progress)%")
                        Spacer()
                        if vm.status.remaining_time > 0 {
                            let h = vm.status.remaining_time / 60
                            let m = vm.status.remaining_time % 60
                            Text(h > 0 ? "\(h)h \(m)m left" : "\(m)m left")
                        }
                    }
                    .font(.caption)
                    .foregroundColor(.secondary)

                    HStack(spacing: 8) {
                        if vm.status.gcode_state == "RUNNING" {
                            Button("Pause") { vm.pause() }
                        } else {
                            Button("Resume") { vm.resume() }
                        }
                        Spacer()
                        Button("Abort") { vm.stop() }
                            .foregroundColor(.red)
                    }
                    .font(.caption)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            }

            // Scheduled Jobs
            Divider()
            VStack(alignment: .leading, spacing: 6) {
                Text("SCHEDULE")
                    .font(.caption2)
                    .foregroundColor(.secondary)
                    .tracking(1)

                if vm.jobs.isEmpty {
                    Text("No scheduled prints")
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .padding(.vertical, 4)
                } else {
                    let todayJobs = vm.jobs.filter { $0.isToday }
                    let laterJobs = vm.jobs.filter { !$0.isToday }

                    if !todayJobs.isEmpty {
                        Text("Today")
                            .font(.caption2.weight(.semibold))
                            .foregroundColor(.blue)
                            .padding(.top, 2)
                        ForEach(todayJobs) { job in
                            JobRow(job: job, onCancel: { vm.cancelJob(id: job.id) })
                        }
                    }

                    if !laterJobs.isEmpty {
                        Text("Upcoming")
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
                Button("Open Web UI") {
                    NSWorkspace.shared.open(URL(string: "http://localhost:8080")!)
                }
                Spacer()
                Button("Quit") {
                    ServiceManager.unloadBackend()
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

    var color: Color {
        switch state {
        case "IDLE", "FINISH": return .green
        case "RUNNING": return .blue
        case "PREPARE": return .yellow
        case "PAUSE": return .orange
        case "FAILED": return .red
        default: return .gray
        }
    }

    var label: String {
        switch state {
        case "IDLE": return "Idle"
        case "FINISH": return "Finished"
        case "RUNNING": return "Printing"
        case "PREPARE": return "Preparing"
        case "PAUSE": return "Paused"
        case "FAILED": return "Error"
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
}

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
        if !needsSetup {
            refresh()
        }
        timer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, !self.needsSetup, !self.showingSettings else { return }
                self.refresh()
            }
        }
        animationTimer = Timer.scheduledTimer(withTimeInterval: 0.8, repeats: true) { [weak self] _ in
            Task { @MainActor in
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
