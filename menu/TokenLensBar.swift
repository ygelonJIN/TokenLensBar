import Cocoa
import SwiftUI
import Combine

// 菜单栏外壳 + 点开的图表面板。
// 计算全在 tokenlensbar.py 里；这里只负责取数和画图。
// 图表用 NSBezierPath 原生绘制，不引入 WebView 进程，所以点开是瞬时的。

let kRefreshInterval: TimeInterval = 60
let kDashboardPort = 8787
let kPanelWidth: CGFloat = 320

// MARK: - 取数

struct Snapshot: Decodable {
    struct Window: Decodable {
        let tokens: Double
        let cost: Double
        let events: Int
    }
    struct DayPoint: Decodable {
        let date: String
        let tokens: Double
        let cost: Double?
    }
    struct Slice: Decodable {
        let model: String?
        let agent: String?
        let tokens: Double
        let cost: Double
        let share: Double
        let subscription: Bool?
    }
    struct BalanceItem: Decodable {
        let currency: String?
        let total: Double?
        let granted: Double?
        let toppedUp: Double?
        let available: Bool?

        enum CodingKeys: String, CodingKey {
            case currency, total, granted, available
            case toppedUp = "topped_up"
        }
    }
    struct BalanceProvider: Decodable {
        let id: String?
        let name: String?
        let type: String?
        let items: [BalanceItem]?
        let fetchedAt: Double?
        let lastAttempt: Double?
        let error: String?
        let stale: Bool?

        enum CodingKeys: String, CodingKey {
            case id, name, type, items, error, stale
            case fetchedAt = "fetched_at"
            case lastAttempt = "last_attempt"
        }
    }
    struct Balances: Decodable {
        let configured: Bool?
        let refreshSeconds: Int?
        let providers: [BalanceProvider]?

        enum CodingKeys: String, CodingKey {
            case configured, providers
            case refreshSeconds = "refresh_seconds"
        }
    }

    let ok: Bool
    let symbol: String?
    let windows: [String: Window]?
    let daily: [DayPoint]?
    let models: [Slice]?
    let agents: [Slice]?
    let modelsToday: [Slice]?
    let agentsToday: [Slice]?
    let cacheRate: Double?
    let allTokens: Double?
    let allEvents: Int?
    let unpriced: [String]?
    let priced: Bool?
    let balances: Balances?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case ok, symbol, windows, daily, models, agents, unpriced, priced, balances, reason
        case cacheRate = "cache_rate"
        case allTokens = "all_tokens"
        case allEvents = "all_events"
        case modelsToday = "models_today"
        case agentsToday = "agents_today"
    }
}

final class PyBridge {
    private let scriptPath = ProcessInfo.processInfo.environment["TOKENLENSBAR_SCRIPT"] ?? kScriptPath

    private func run(arguments: [String], wait: Bool = true) -> Data? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [scriptPath] + arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do { try process.run() } catch { return nil }
        if !wait {
            DispatchQueue.global(qos: .background).async { process.waitUntilExit() }
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return data
    }

    func load() -> Snapshot? {
        guard let data = run(arguments: ["status", "--json"]) else { return nil }
        return try? JSONDecoder().decode(Snapshot.self, from: data)
    }

    func scan() { _ = run(arguments: ["scan", "--force-balance"]) }

    /// 打开看板：服务已经在跑就直接开浏览器，没跑就先起服务再开。
    /// 原来不管服务在不在都 blindly 起一个 serve，端口被占用时进程直接退出，
    /// 浏览器也不会打开——点上去就像"没反应"。
    func openDashboard() {
        let url = URL(string: "http://127.0.0.1:\(kDashboardPort)/")!
        DispatchQueue.global(qos: .utility).async { [weak self] in
            if !PyBridge.portOpen(kDashboardPort) {
                _ = self?.run(arguments: ["serve", "--port", "\(kDashboardPort)"], wait: false)
                Thread.sleep(forTimeInterval: 0.9)
            }
            DispatchQueue.main.async { NSWorkspace.shared.open(url) }
        }
    }

    private static func portOpen(_ port: Int) -> Bool {
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = in_port_t(UInt16(port).bigEndian)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let handle = socket(AF_INET, SOCK_STREAM, 0)
        if handle < 0 { return false }
        defer { close(handle) }
        let result = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(handle, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return result == 0
    }
}

// MARK: - 格式化

func cn(_ value: Double) -> String {
    if value >= 100_000_000 { return String(format: "%.2f亿", value / 100_000_000) }
    if value >= 10_000 { return String(format: "%.1f万", value / 10_000) }
    return String(format: "%.0f", value)
}

func cnTiny(_ value: Double) -> String {
    if value >= 100_000_000 { return String(format: "%.1f亿", value / 100_000_000) }
    if value >= 10_000 { return String(format: "%.0f万", value / 10_000) }
    return String(format: "%.0f", value)
}

func moneyCompact(_ value: Double, _ symbol: String) -> String {
    if value >= 100 { return symbol + String(format: "%.0f", value) }
    if value >= 10 { return symbol + String(format: "%.1f", value) }
    if value >= 0.1 { return symbol + String(format: "%.2f", value) }
    if value <= 0 { return symbol + "0" }
    return symbol + String(format: "%.3f", value)
}

/// 订阅制显示"订阅"；有 token 但没配单价显示横杠；其余按金额
func costText(_ cost: Double, tokens: Double, _ symbol: String,
              subscription: Bool = false) -> String {
    if subscription { return "订阅" }
    if tokens > 0 && cost <= 0 { return "—" }
    return money(cost, symbol)
}

func countText(_ value: Int) -> String {
    let formatter = NumberFormatter()
    formatter.numberStyle = .decimal
    formatter.groupingSeparator = ","
    return formatter.string(from: NSNumber(value: value)) ?? "\(value)"
}

func money(_ value: Double, _ symbol: String) -> String {
    if value <= 0 { return symbol + "0" }
    if value < 0.01 { return symbol + String(format: "%.3f", value) }
    if value >= 1000 { return symbol + String(format: "%.0f", value) }
    return symbol + String(format: "%.2f", value)
}

func logLine(_ message: String) {
    let path = (kDbPath as NSString).deletingLastPathComponent + "/bar.log"
    let stamp = ISO8601DateFormatter().string(from: Date())
    guard let data = "[\(stamp)] \(message)\n".data(using: .utf8) else { return }
    if let handle = FileHandle(forWritingAtPath: path) {
        handle.seekToEndOfFile()
        handle.write(data)
        handle.closeFile()
    } else {
        FileManager.default.createFile(atPath: path, contents: data)
    }
}

let kPanelHeight: CGFloat = 490

func shortDate(_ text: String) -> String {
    let parts = text.split(separator: "-")
    guard parts.count == 3 else { return text }
    return "\(parts[1])-\(parts[2])"
}

func balanceText(_ value: Double, currency: String) -> String {
    let code = currency.uppercased()
    let symbol = code == "CNY" ? "¥" : (code == "USD" ? "$" : code + " ")
    let formatter = NumberFormatter()
    formatter.numberStyle = .decimal
    formatter.minimumFractionDigits = 2
    formatter.maximumFractionDigits = 2
    formatter.groupingSeparator = ","
    let number = formatter.string(from: NSNumber(value: abs(value)))
        ?? String(format: "%.2f", abs(value))
    return (value < 0 ? "-" : "") + symbol + number
}

func providerBalanceText(_ provider: Snapshot.BalanceProvider) -> String {
    let values = (provider.items ?? []).map {
        balanceText($0.total ?? 0, currency: $0.currency ?? "CNY")
    }
    return values.isEmpty ? "—" : values.joined(separator: " / ")
}

func clockText(_ epoch: Double) -> String {
    let date = Date(timeIntervalSince1970: epoch)
    let formatter = DateFormatter()
    formatter.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm" : "MM-dd HH:mm"
    return formatter.string(from: date)
}

// MARK: - 数据模型

final class PanelModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var status: String = ""
    @Published var lastLoaded: Date?
    @Published var loadFailed = false
    @Published var colorScheme: ColorScheme = .dark

    private let bridge = PyBridge()
    private var clearItem: DispatchWorkItem?

    func reload() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let snap = self.bridge.load()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.snapshot = snap
                self.loadFailed = (snap == nil)
                self.lastLoaded = Date()
            }
        }
    }

    func scan() {
        status = "采集中…"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            self.bridge.scan()
            let snap = self.bridge.load()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.snapshot = snap
                self.loadFailed = (snap == nil)
                self.lastLoaded = Date()
                self.status = "已采集"
                self.clearItem?.cancel()
                let item = DispatchWorkItem { [weak self] in
                    if self?.status.hasPrefix("已采集") == true { self?.status = "" }
                }
                self.clearItem = item
                DispatchQueue.main.asyncAfter(deadline: .now() + 4, execute: item)
            }
        }
    }

    func openDashboard() {
        status = "正在打开看板…"
        bridge.openDashboard()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            guard let self else { return }
            self.status = "看板已打开"
            self.clearItem?.cancel()
            let item = DispatchWorkItem { [weak self] in
                if self?.status == "看板已打开" { self?.status = "" }
            }
            self.clearItem = item
            DispatchQueue.main.asyncAfter(deadline: .now() + 3, execute: item)
        }
    }

    func quit() {
        NSApp.terminate(nil)
    }
}

// MARK: - 原生 SwiftUI 面板

enum PanelTab: String, CaseIterable, Identifiable {
    case overview = "概览"
    case history = "历史"

    var id: String { rawValue }
}

struct PanelView: View {
    @ObservedObject var model: PanelModel
    @State private var tab: PanelTab = {
        if let raw = ProcessInfo.processInfo.environment["TOKENLENSBAR_TAB"],
           let value = PanelTab(rawValue: raw) {
            return value
        }
        return .overview
    }()

    var body: some View {
        GeometryReader { geo in
            VStack(spacing: 0) {
                topBar
                if model.snapshot?.ok == true {
                    if tab == .overview {
                        ScrollView {
                            OverviewSections(model: model)
                                .padding(.bottom, 62)
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        ScrollView {
                            HistorySections(model: model)
                                .padding(.bottom, 62)
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                } else {
                    Spacer(minLength: 0)
                    emptyState
                    Spacer(minLength: 0)
                }
            }
            .frame(width: geo.size.width, height: geo.size.height, alignment: .top)
            .overlay(alignment: .bottom) { bottomBar }
        }
        .frame(width: kPanelWidth, height: kPanelHeight)
        .background(Color.clear)
        .preferredColorScheme(model.colorScheme)
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text("TokenLensBar").font(.headline)
                Text("本地 token 用量")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            // 采集状态和更新时间放右上角，不再占底部按钮的位置。
            VStack(alignment: .trailing, spacing: 1) {
                if !model.status.isEmpty {
                    Text(model.status)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                if let loaded = model.lastLoaded {
                    HStack(spacing: 3) {
                        Text("更新")
                        Text(loaded, style: .time)
                    }
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
        .padding(.top, 10)
        .padding(.bottom, 8)
    }

    private var topBar: some View {
        VStack(spacing: 0) {
            header
            Picker("", selection: $tab) {
                ForEach(PanelTab.allCases) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 12)
            .padding(.bottom, 8)
            Divider()
        }
        .background(.bar)
    }

    private var bottomBar: some View {
        VStack(spacing: 0) {
            Divider()
            HStack(spacing: 8) {
                Button {
                    model.scan()
                } label: {
                    Label("立即采集", systemImage: "arrow.clockwise")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)

                Button {
                    model.openDashboard()
                } label: {
                    Label("打开看板", systemImage: "rectangle.on.rectangle")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button {
                    model.quit()
                } label: {
                    Label("退出", systemImage: "power")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Spacer(minLength: 0)
            }
            .padding(.horizontal, 12)
            .padding(.top, 6)
            .padding(.bottom, 8)
        }
        .background(.bar)
    }

    @ViewBuilder
    private var emptyState: some View {
        if model.snapshot == nil {
            if model.loadFailed {
                ContentUnavailableView {
                    Label("读取失败", systemImage: "exclamationmark.triangle")
                } description: {
                    Text("点下面的“立即采集”重试")
                } actions: {
                    Button("立即采集") { model.scan() }
                }
            } else {
                ProgressView("读取中…")
            }
        } else {
            ContentUnavailableView {
                Label("还没有数据", systemImage: "chart.bar")
            } description: {
                Text("点下面的“立即采集”")
            } actions: {
                Button("立即采集") { model.scan() }
            }
        }
    }
}

struct NativeSection<Content: View>: View {
    let title: String
    let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(title)
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 12)
                .padding(.top, 8)
                .padding(.bottom, 3)
            content
        }
    }
}

// MARK: - 概览

struct OverviewSections: View {
    @ObservedObject var model: PanelModel

    @ViewBuilder
    var body: some View {
        if let snap = model.snapshot, snap.ok {
            VStack(alignment: .leading, spacing: 0) {
                NativeSection("今日") {
                    SummaryRow(label: "金额",
                               value: money(snap.windows?["today"]?.cost ?? 0,
                                            snap.symbol ?? "¥"))
                    SummaryRow(label: "Token",
                               value: cn(snap.windows?["today"]?.tokens ?? 0))
                    SummaryRow(label: "缓存命中",
                               value: "\(Int(((snap.cacheRate ?? 0) * 100).rounded()))%")
                    SummaryRow(label: "请求",
                               value: countText(snap.windows?["today"]?.events ?? 0))
                }

                if let balances = snap.balances, balances.configured == true,
                   let providers = balances.providers, !providers.isEmpty {
                    NativeSection("余额") {
                        ForEach(Array(providers.enumerated()), id: \.offset) { _, provider in
                            SummaryRow(label: provider.name ?? provider.id ?? "余额",
                                       value: providerBalanceText(provider),
                                       help: provider.error)
                        }
                    }
                }

                NativeSection("按模型") {
                    SliceRows(slices: snap.modelsToday ?? [], symbol: snap.symbol ?? "¥",
                              empty: "今天还没有用量")
                }

                NativeSection("按 Agent") {
                    SliceRows(slices: snap.agentsToday ?? [], symbol: snap.symbol ?? "¥",
                              empty: "今天还没有用量", agent: true)
                }

            }
        }
    }
}

// MARK: - 历史

struct HistorySections: View {
    @ObservedObject var model: PanelModel

    @ViewBuilder
    var body: some View {
        if let snap = model.snapshot, snap.ok {
            NativeSection("全部历史") {
                SummaryRow(label: "总金额",
                           value: money((snap.models ?? []).reduce(0.0) { $0 + $1.cost },
                                        snap.symbol ?? "¥"))
                SummaryRow(label: "总 Token", value: cn(snap.allTokens ?? 0))
                SummaryRow(label: "请求", value: countText(snap.allEvents ?? 0))
            }

            NativeSection("按模型") {
                SliceRows(slices: snap.models ?? [], symbol: snap.symbol ?? "¥",
                          empty: "还没有历史数据")
            }

            NativeSection("按 Agent") {
                SliceRows(slices: snap.agents ?? [], symbol: snap.symbol ?? "¥",
                          empty: "还没有历史数据", agent: true)
            }
        }
    }
}

// MARK: - 复用行

struct SummaryRow: View {
    let label: String
    let value: String
    var help: String? = nil

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(label)
                Spacer()
                Text(value)
                    .monospacedDigit()
                    .help(help ?? "")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 2)
            Divider()
                .padding(.leading, 12)
        }
    }
}

struct SliceRows: View {
    let slices: [Snapshot.Slice]
    let symbol: String
    let empty: String
    var agent: Bool = false

    var body: some View {
        if slices.isEmpty {
            Text(empty)
                .foregroundStyle(.secondary)
        } else if agent {
            ForEach(slices, id: \.agent) { slice in
                SliceRow(slice: slice, symbol: symbol, agent: true)
            }
        } else {
            ForEach(slices, id: \.model) { slice in
                SliceRow(slice: slice, symbol: symbol, agent: false)
            }
        }
    }
}

struct SliceRow: View {
    let slice: Snapshot.Slice
    let symbol: String
    let agent: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(agent ? (slice.agent ?? "?") : (slice.model ?? "?"))
                    .lineLimit(1)
                Spacer(minLength: 8)
                Text(costText(slice.cost, tokens: slice.tokens, symbol,
                              subscription: slice.subscription ?? false))
                    .monospacedDigit()
                Text(cn(slice.tokens))
                    .monospacedDigit()
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 2)
            Divider()
                .padding(.leading, 12)
        }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let model = PanelModel()
    private var panel: NSHostingController<PanelView>?
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var timer: Timer?
    private var cancellables = Set<AnyCancellable>()
    private var loggedFirstTitle = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "…"
        statusItem.button?.toolTip = "TokenLensBar：本地 token 用量"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePopover)

        popover = NSPopover()
        popover.behavior = .transient
        popover.animates = true

        model.$snapshot
            .receive(on: RunLoop.main)
            .sink { [weak self] snapshot in
                self?.updateStatusItem(snapshot)
            }
            .store(in: &cancellables)

        logLine("启动：statusItem=\(statusItem.button == nil ? "nil" : "ok")")
        model.reload()

        let timer = Timer.scheduledTimer(withTimeInterval: kRefreshInterval, repeats: true) { [weak self] _ in
            self?.model.reload()
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func statusTitle(_ today: Snapshot.Window, symbol: String) -> NSAttributedString {
        let tokenFont = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        let moneyFont = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .semibold)
        let title = NSMutableAttributedString()
        title.append(NSAttributedString(string: cn(today.tokens),
                                        attributes: [.font: tokenFont]))
        title.append(NSAttributedString(string: "  ",
                                        attributes: [.font: tokenFont]))
        title.append(NSAttributedString(string: moneyCompact(today.cost, symbol),
                                        attributes: [.font: moneyFont]))
        return title
    }

    private func updateStatusItem(_ snapshot: Snapshot?) {
        guard let snapshot, snapshot.ok, let today = snapshot.windows?["today"] else {
            statusItem.button?.title = "—"
            return
        }
        let symbol = snapshot.symbol ?? "¥"
        statusItem.button?.attributedTitle = statusTitle(today, symbol: symbol)
        let plain = cn(today.tokens) + "  " + moneyCompact(today.cost, symbol)
        if !loggedFirstTitle {
            loggedFirstTitle = true
            logLine("首次刷新成功：菜单栏标题=\(plain)")
        }
        statusItem.button?.toolTip =
            "今日 \(cn(today.tokens)) tokens · \(money(today.cost, symbol)) · \(countText(today.events)) 次请求"
    }

    private func ensurePanel() -> NSHostingController<PanelView> {
        if let panel { return panel }
        let host = NSHostingController(rootView: PanelView(model: model))
        host.view.wantsLayer = true
        host.view.layer?.backgroundColor = NSColor.clear.cgColor
        host.preferredContentSize = NSSize(width: kPanelWidth, height: kPanelHeight)
        panel = host
        return host
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        model.reload()
        let menuDark = button.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
        model.colorScheme = menuDark ? .dark : .light
        let host = ensurePanel()
        popover.contentViewController = host
        popover.contentSize = NSSize(width: kPanelWidth, height: kPanelHeight)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let effective = self.popover.contentViewController?.view.window?.effectiveAppearance
                ?? NSApp.effectiveAppearance
            let isDark = effective.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            self.panel?.view.appearance = NSAppearance(named: isDark ? .darkAqua : .aqua)
            self.model.colorScheme = isDark ? .dark : .light
        }
    }
}

@main
struct TokenLensBarMain {
    static func main() {
        let args = CommandLine.arguments
        if let index = args.firstIndex(of: "--render-panel") {
            let path = index + 1 < args.count ? args[index + 1] : "/tmp/tokenlensbar-panel.png"
            renderPanel(path: path)
            return
        }
        if args.contains("--dump") {
            dump()
            return
        }
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }

    private static func renderPanel(path: String) {
        let model = PanelModel()
        model.snapshot = PyBridge().load()
        model.lastLoaded = Date()
        let scheme: ColorScheme = ProcessInfo.processInfo.environment["TOKENLENSBAR_APPEARANCE"] == "light"
            ? .light : .dark
        model.colorScheme = scheme
        let root = PanelView(model: model)
            .environment(\.colorScheme, scheme)
            .background(Color(nsColor: .windowBackgroundColor))
        let host = NSHostingController(rootView: root)
        let size = NSSize(width: kPanelWidth, height: kPanelHeight)
        host.view.frame = NSRect(origin: .zero, size: size)
        let appearanceName = ProcessInfo.processInfo.environment["TOKENLENSBAR_APPEARANCE"] == "light"
            ? NSAppearance.Name.aqua : NSAppearance.Name.darkAqua
        host.view.appearance = NSAppearance(named: appearanceName)
        host.view.wantsLayer = true
        host.view.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentViewController = host
        window.appearance = NSAppearance(named: appearanceName)
        window.orderFrontRegardless()
        host.view.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.2))

        let scale: CGFloat = 2
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: Int(size.width * scale),
            pixelsHigh: Int(size.height * scale),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ) else { return }
        rep.size = size
        host.view.cacheDisplay(in: host.view.bounds, to: rep)
        if let data = rep.representation(using: .png, properties: [:]) {
            try? data.write(to: URL(fileURLWithPath: path))
            print("已输出：\(path)")
        }
        window.orderOut(nil)
    }

    private static func dump() {
        guard let snapshot = PyBridge().load() else {
            print("取数失败")
            return
        }
        guard snapshot.ok else {
            print("还没有数据（\(snapshot.reason ?? "unknown")）")
            return
        }
        let symbol = snapshot.symbol ?? "¥"
        let windows = snapshot.windows ?? [:]
        print("菜单栏标题  \(cn(windows["today"]?.tokens ?? 0))")
        for (key, label) in [("today", "今日"), ("week", "近 7 天"), ("month", "近 30 天")] {
            guard let window = windows[key] else { continue }
            print("  \(label)  \(cn(window.tokens)) tokens  \(money(window.cost, symbol))  (\(window.events) 条)")
        }
        if let daily = snapshot.daily {
            print("\n近 14 天")
            print("  " + daily.map { cn($0.tokens) }.joined(separator: "  "))
        }
        if let unpriced = snapshot.unpriced, !unpriced.isEmpty {
            print("\n⚠ 未定价：\(unpriced.joined(separator: "、"))")
        }
        if let balances = snapshot.balances, balances.configured == true {
            for provider in balances.providers ?? [] {
                let values = (provider.items ?? []).map {
                    balanceText($0.total ?? 0, currency: $0.currency ?? "CNY")
                }
                print("\n余额 \(provider.name ?? provider.id ?? "?"): \(values.joined(separator: "  "))")
            }
        }
    }
}
