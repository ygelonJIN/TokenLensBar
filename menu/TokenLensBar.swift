import Cocoa
import SwiftUI
import Combine

// 菜单栏外壳 + 点开的图表面板。
// 计算全在 tokenlensbar.py 里；这里只负责取数和画图。
// 图表用 NSBezierPath 原生绘制，不引入 WebView 进程，所以点开是瞬时的。

let kRefreshInterval: TimeInterval = 60
let kDashboardPort = 8787
let kPanelWidth: CGFloat = 280
let kPanelDefaultHeight: CGFloat = 300

/// 面板最高不超过屏幕可见区；超出的内容在面板里滚动，不写死总高度。
func panelMaxHeight() -> CGFloat {
    let visible = NSScreen.main?.visibleFrame.height ?? 900
    return max(320, visible - 120)
}

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
    struct TodaySpend: Decodable {
        struct Provider: Decodable {
            let id: String?
            let name: String?
            let currency: String?
            let spent: Double?
            let startTs: Double?
            let samples: Int?
            let gap: Double?

            enum CodingKeys: String, CodingKey {
                case id, name, currency, spent, samples, gap
                case startTs = "start_ts"
            }
        }
        struct Currency: Decodable {
            let currency: String?
            let symbol: String?
            let automatic: Double?
            let manual: Double?
            let total: Double
            let providers: [Provider]?
        }
        struct ManualEntry: Decodable {
            let id: Int?
            let amount: Double?
            let tokens: Int?
            let agent: String?
            let model: String?
            let createdAt: Double?

            enum CodingKeys: String, CodingKey {
                case id, amount, tokens, agent, model
                case createdAt = "created_at"
            }
        }
        struct Manual: Decodable {
            let amount: Double?
            let tokens: Int?
            let entries: [ManualEntry]?
        }

        let configured: Bool?
        let day: String?
        let currencies: [Currency]?
        let manual: Manual?
        let tokens: Int?
        let error: String?
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
    let allCost: Double?
    let todaySpend: TodaySpend?
    let unpriced: [String]?
    let priced: Bool?
    let balances: Balances?
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case ok, symbol, windows, daily, models, agents, unpriced, priced, balances, reason
        case cacheRate = "cache_rate"
        case allTokens = "all_tokens"
        case allEvents = "all_events"
        case allCost = "all_cost"
        case todaySpend = "today_spend"
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

    /// 电脑唤醒/打开面板时补一次余额，让今天的起点尽量贴近开盖时刻。
    func refreshBalance() { _ = run(arguments: ["balance", "--refresh"]) }

    /// 手动补记。金额和 Token 至少传一个。
    func addManual(amount: Double?, tokens: Int?, agent: String?, model: String?) {
        var args = ["manual", "add"]
        if let amount {
            let text = String(format: "%.4f", amount)
                .replacingOccurrences(of: ",", with: ".")
            args += ["--amount", text]
        }
        if let tokens {
            args += ["--tokens", String(tokens)]
        }
        if let agent, !agent.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args += ["--agent", agent]
        }
        if let model, !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            args += ["--model", model]
        }
        guard args.count > 2 else { return }
        _ = run(arguments: args)
    }

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

func moneyCompactSigned(_ value: Double, _ symbol: String) -> String {
    if value < 0 { return "-" + moneyCompact(-value, symbol) }
    return moneyCompact(value, symbol)
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

/// 允许负数的金额显示（手工补记可以填负数来做冲减）。
func signedMoney(_ value: Double, _ symbol: String) -> String {
    if value < 0 { return "-" + money(-value, symbol) }
    return money(value, symbol)
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
    private var lastBalanceRefresh: Date?

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

    /// 补一次余额。唤醒时强制，打开面板时隔一段时间才补，避免每次点开都打接口。
    func refreshBalance(force: Bool = false) {
        let now = Date()
        if !force, let last = lastBalanceRefresh,
           now.timeIntervalSince(last) < 120 {
            return
        }
        lastBalanceRefresh = now
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            self.bridge.refreshBalance()
            let snap = self.bridge.load()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.snapshot = snap
                self.loadFailed = (snap == nil)
                self.lastLoaded = Date()
                self.flash("余额已更新", seconds: 3)
            }
        }
    }

    /// 打开面板时如果今天还没样本，或上次余额刷新较久，后台补一次。
    func refreshBalanceIfNeeded() {
        refreshBalance(force: false)
    }

    func addManual(amount: Double?, tokens: Int?, agent: String?, model: String?) {
        guard amount != nil || tokens != nil else {
            status = "金额和 Token 至少填一个"
            return
        }
        status = "正在补记…"
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            self.bridge.addManual(amount: amount, tokens: tokens,
                                  agent: agent, model: model)
            let snap = self.bridge.load()
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.snapshot = snap
                self.loadFailed = (snap == nil)
                self.lastLoaded = Date()
                self.flash("已补记", seconds: 4)
            }
        }
    }

    /// 手动补记入口：故意做得不显眼，字段都可选，金额/Token 至少一个。
    func presentManualEntry() {
        let alert = NSAlert()
        alert.messageText = "手动补记"
        alert.informativeText = "金额和 Token 至少填一个；Agent / Model 可空。\n只补余额没覆盖的消费，余额已经算过的不要再补。"
        alert.alertStyle = .informational
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")

        let amountField = NSTextField(string: "")
        let tokensField = NSTextField(string: "")
        let agentField = NSTextField(string: "")
        let modelField = NSTextField(string: "")
        amountField.placeholderString = "例如 2.00，可为负数"
        tokensField.placeholderString = "例如 12000"
        agentField.placeholderString = "可空"
        modelField.placeholderString = "可空"
        for field in [amountField, tokensField, agentField, modelField] {
            field.widthAnchor.constraint(equalToConstant: 180).isActive = true
        }

        let grid = NSGridView(views: [
            [NSTextField(labelWithString: "金额"), amountField],
            [NSTextField(labelWithString: "Token"), tokensField],
            [NSTextField(labelWithString: "Agent"), agentField],
            [NSTextField(labelWithString: "Model"), modelField],
        ])
        grid.rowSpacing = 8
        grid.columnSpacing = 10
        grid.column(at: 0).xPlacement = .trailing
        grid.frame = NSRect(x: 0, y: 0, width: 270, height: 120)
        alert.accessoryView = grid
        alert.window.initialFirstResponder = amountField
        NSApp.activate(ignoringOtherApps: true)

        guard alert.runModal() == .alertFirstButtonReturn else { return }

        func amountValue(_ text: String) -> Double? {
            let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines)
                .replacingOccurrences(of: "，", with: ".")
                .replacingOccurrences(of: ",", with: ".")
            guard !normalized.isEmpty else { return nil }
            return Double(normalized)
        }
        func tokenValue(_ text: String) -> Int? {
            let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines)
                .replacingOccurrences(of: ",", with: "")
            guard !normalized.isEmpty else { return nil }
            return Int(normalized)
        }

        let amountText = amountField.stringValue
        let tokenText = tokensField.stringValue
        let amount = amountValue(amountText)
        let tokens = tokenValue(tokenText)
        if !amountText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, amount == nil {
            status = "金额格式不对"
            return
        }
        if let amount, !amount.isFinite {
            status = "金额格式不对"
            return
        }
        if !tokenText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, tokens == nil {
            status = "Token 必须是整数"
            return
        }
        if let tokens, tokens < 0 {
            status = "Token 不能是负数"
            return
        }
        guard amount != nil || tokens != nil else {
            status = "金额和 Token 至少填一个"
            return
        }
        addManual(amount: amount, tokens: tokens,
                  agent: agentField.stringValue, model: modelField.stringValue)
    }

    private func flash(_ message: String, seconds: Double) {
        status = message
        clearItem?.cancel()
        let item = DispatchWorkItem { [weak self] in
            if self?.status == message { self?.status = "" }
        }
        clearItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: item)
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

private struct PanelContentHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
    }
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
    @State private var contentHeight: CGFloat = kPanelDefaultHeight

    private var maxScrollHeight: CGFloat {
        max(160, panelMaxHeight() - 118)
    }

    var body: some View {
        VStack(spacing: 0) {
            topBar
            if model.snapshot?.ok == true {
                ScrollView(.vertical, showsIndicators: contentHeight > maxScrollHeight + 1) {
                    Group {
                        if tab == .overview {
                            OverviewSections(model: model)
                        } else {
                            HistorySections(model: model)
                        }
                    }
                    .padding(.bottom, 6)
                    .background(
                        GeometryReader { proxy in
                            Color.clear.preference(key: PanelContentHeightKey.self,
                                                   value: proxy.size.height)
                        }
                    )
                }
                .frame(height: min(max(contentHeight, 1), maxScrollHeight))
            } else {
                emptyState
                    .frame(height: 250)
            }
            bottomBar
        }
        .frame(width: kPanelWidth)
        .onPreferenceChange(PanelContentHeightKey.self) { height in
            if abs(height - contentHeight) > 0.5 {
                contentHeight = height
            }
        }
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
            HStack(spacing: 6) {
                Button {
                    model.scan()
                } label: {
                    Label("采集", systemImage: "arrow.clockwise")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .help("立即采集一次")

                Button {
                    model.openDashboard()
                } label: {
                    Label("看板", systemImage: "rectangle.on.rectangle")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("打开浏览器看板")

                Button {
                    model.quit()
                } label: {
                    Label("退出", systemImage: "power")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
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
                    Text("点下面的「采集」重试")
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

struct EmptyHint: View {
    let title: String
    var detail: String? = nil

    var body: some View {
        VStack(spacing: 4) {
            Text(title)
                .font(.callout)
                .foregroundStyle(.secondary)
            if let detail {
                Text(detail)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 16)
        .padding(.vertical, 18)
    }
}

struct OverviewSections: View {
    @ObservedObject var model: PanelModel

    @ViewBuilder
    var body: some View {
        if let snap = model.snapshot, snap.ok {
            VStack(alignment: .leading, spacing: 0) {
                NativeSection("今日") {
                    TodaySpendRow(snap: snap)
                    SummaryRow(label: "Token", value: todayTokenText(snap))
                    SummaryRow(label: "缓存命中",
                               value: "\(Int(((snap.cacheRate ?? 0) * 100).rounded()))%")
                    SummaryRow(label: "请求",
                               value: countText(snap.windows?["today"]?.events ?? 0))

                    HStack {
                        Spacer()
                        Button {
                            model.presentManualEntry()
                        } label: {
                            Label("补记", systemImage: "square.and.pencil")
                                .font(.caption)
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .help("手动补记金额 / Token / Agent / Model")
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 4)
                }

                if let entries = snap.todaySpend?.manual?.entries, !entries.isEmpty {
                    NativeSection("手动补记") {
                        ForEach(entries, id: \.id) { entry in
                            ManualEntryRow(entry: entry, symbol: snap.symbol ?? "¥")
                        }
                    }
                }

                if (snap.modelsToday ?? []).isEmpty && (snap.agentsToday ?? []).isEmpty {
                    // 今天没有自动 token 记录。有手动补记时只显示上面的补记列表，
                    // 不再重复贴“今天还没有用量”。
                    if (snap.todaySpend?.manual?.entries ?? []).isEmpty {
                        EmptyHint(title: "今天还没有用量",
                                  detail: "有请求后，这里会按模型和 Agent 拆分")
                    }
                } else {
                    NativeSection("按模型（估算）") {
                        SliceRows(slices: snap.modelsToday ?? [], symbol: snap.symbol ?? "¥",
                                  empty: "今天没有按模型的记录")
                    }

                    NativeSection("按 Agent（估算）") {
                        SliceRows(slices: snap.agentsToday ?? [], symbol: snap.symbol ?? "¥",
                                  empty: "今天没有按 Agent 的记录", agent: true)
                    }
                }

                // 余额放在按模型 / 按 Agent 下面，先看用量再看账户。
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
            }
        }
    }

    private func todayTokenText(_ snap: Snapshot) -> String {
        let manual = Double(snap.todaySpend?.tokens ?? 0)
        return cn((snap.windows?["today"]?.tokens ?? 0) + manual)
    }
}

/// 今日消费：余额下降为金额；没有余额平台时退回 Token 估算。
struct TodaySpendRow: View {
    let snap: Snapshot

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("消费")
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Text(value)
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            .help(help)
            .padding(.horizontal, 12)
            .padding(.vertical, 2)
            Divider()
                .padding(.leading, 12)
        }
    }

    private var spend: Snapshot.TodaySpend? { snap.todaySpend }

    private var primary: Snapshot.TodaySpend.Currency? {
        spend?.currencies?.first
    }

    private var value: String {
        guard let primary else {
            return money(snap.windows?["today"]?.cost ?? 0, snap.symbol ?? "¥")
        }
        let hasSample = primary.providers?.contains { $0.spent != nil } == true
        let hasManual = (spend?.manual?.amount ?? 0) != 0
            || (spend?.manual?.tokens ?? 0) > 0
        if !hasSample && !hasManual {
            return "—"
        }
        let symbol = primary.symbol ?? snap.symbol ?? "¥"
        var text = signedMoney(primary.total, symbol)
        for other in (spend?.currencies ?? []).dropFirst() {
            text += " + " + signedMoney(other.total, other.symbol ?? other.currency ?? "")
        }
        return text
    }

    private var note: String {
        guard let primary else { return "Token 估算" }
        let starts = (primary.providers ?? []).compactMap { $0.startTs }
        if let earliest = starts.min() {
            return "\(clockText(earliest)) 起"
        }
        if (spend?.manual?.tokens ?? 0) > 0 || (spend?.manual?.amount ?? 0) != 0 {
            return "手动补记"
        }
        return "今天还没采到余额"
    }

    private var help: String {
        guard let spend else {
            return "还没有余额数据；这是按本地单价表算出的 Token 估算。"
        }
        if let primary = spend.currencies?.first {
            var lines: [String] = []
            for provider in primary.providers ?? [] {
                let symbol = primary.symbol ?? snap.symbol ?? "¥"
                let spent = provider.spent.map { signedMoney($0, symbol) } ?? "—"
                let start = provider.startTs.map { clockText($0) + " 起" } ?? ""
                lines.append("\(provider.name ?? provider.id ?? "?"): \(spent)"
                             + (start.isEmpty ? "" : "（\(start)）"))
            }
            if let manual = spend.manual, (manual.amount ?? 0) != 0 || (manual.tokens ?? 0) > 0 {
                let symbol = primary.symbol ?? snap.symbol ?? "¥"
                lines.append("手动补记: \(signedMoney(manual.amount ?? 0, symbol))"
                             + ((manual.tokens ?? 0) > 0 ? " · \(countText(manual.tokens ?? 0)) tokens" : ""))
            }
            if lines.isEmpty {
                lines.append("今天还没采到余额。")
            }
            return lines.joined(separator: "\n")
        }
        return "还没有余额数据；这是按本地单价表算出的 Token 估算。"
    }
}

struct ManualEntryRow: View {
    let entry: Snapshot.TodaySpend.ManualEntry
    let symbol: String

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text(title)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Text(value)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 2)
            Divider()
                .padding(.leading, 12)
        }
    }

    private var title: String {
        let parts = [entry.agent, entry.model].compactMap { $0 }.filter { !$0.isEmpty }
        return parts.isEmpty ? "手动补记" : parts.joined(separator: " · ")
    }

    private var value: String {
        var parts: [String] = []
        if let amount = entry.amount, amount != 0 {
            parts.append(signedMoney(amount, symbol))
        }
        if let tokens = entry.tokens, tokens > 0 {
            parts.append("\(countText(tokens)) tokens")
        }
        return parts.isEmpty ? "—" : parts.joined(separator: " · ")
    }
}

// MARK: - 历史

struct HistorySections: View {
    @ObservedObject var model: PanelModel

    @ViewBuilder
    var body: some View {
        if let snap = model.snapshot, snap.ok {
            NativeSection("全部历史") {
                SummaryRow(label: "总金额（Token 估算）",
                           value: money(modelCost(snap), snap.symbol ?? "¥"))
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

    private func modelCost(_ snap: Snapshot) -> Double {
        (snap.models ?? []).reduce(0.0) { $0 + $1.cost }
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
            EmptyHint(title: empty)
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
    private var panelSizeObservation: NSKeyValueObservation?
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var timer: Timer?
    private var cancellables = Set<AnyCancellable>()
    private var loggedFirstTitle = false
    private var themeObserver: NSObjectProtocol?
    private var wakeObservers: [NSObjectProtocol] = []
    private var lastWakeRefresh = Date.distantPast

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "…"
        statusItem.button?.toolTip = "TokenLensBar：本地 token 用量"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePopover)

        popover = NSPopover()
        popover.behavior = .transient
        popover.animates = true

        // 系统浅色/深色切换时，面板若开着就立刻跟着变。
        themeObserver = DistributedNotificationCenter.default().addObserver(
            forName: NSNotification.Name("AppleInterfaceThemeChangedNotification"),
            object: nil, queue: .main
        ) { [weak self] _ in
            guard let self, self.popover.isShown else { return }
            self.applyPanelAppearance()
        }

        // 开盖/唤醒时立刻补一次余额，让今天的起点尽量贴近开盖时刻。
        let workspaceCenter = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.didWakeNotification,
                     NSWorkspace.screensDidWakeNotification] {
            wakeObservers.append(workspaceCenter.addObserver(
                forName: name, object: nil, queue: .main
            ) { [weak self] _ in
                guard let self else { return }
                let now = Date()
                guard now.timeIntervalSince(self.lastWakeRefresh) > 5 else { return }
                self.lastWakeRefresh = now
                self.model.refreshBalance(force: true)
            })
        }

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

    private func statusTitle(_ today: Snapshot.Window, tokens: Double, cost: Double,
                             symbol: String) -> NSAttributedString {
        let tokenFont = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        let moneyFont = NSFont.monospacedDigitSystemFont(ofSize: 13, weight: .semibold)
        let title = NSMutableAttributedString()
        title.append(NSAttributedString(string: cn(tokens),
                                        attributes: [.font: tokenFont]))
        title.append(NSAttributedString(string: "  ",
                                        attributes: [.font: tokenFont]))
        title.append(NSAttributedString(string: moneyCompactSigned(cost, symbol),
                                        attributes: [.font: moneyFont]))
        return title
    }

    private func updateStatusItem(_ snapshot: Snapshot?) {
        guard let snapshot, snapshot.ok, let today = snapshot.windows?["today"] else {
            statusItem.button?.title = "—"
            return
        }
        let symbol = snapshot.symbol ?? "¥"
        let spend = snapshot.todaySpend
        let primary = spend?.currencies?.first
        let hasSample = primary?.providers?.contains { $0.spent != nil } == true
        let hasManual = (spend?.manual?.amount ?? 0) != 0 || (spend?.manual?.tokens ?? 0) > 0
        let showSpend = primary != nil && (hasSample || hasManual)
        let cost = showSpend ? (primary?.total ?? 0) : today.cost
        let totalTokens = today.tokens + Double(spend?.tokens ?? 0)

        statusItem.button?.attributedTitle = statusTitle(
            today, tokens: totalTokens, cost: cost, symbol: symbol)
        let plain = cn(totalTokens) + "  " + moneyCompactSigned(cost, symbol)
        if !loggedFirstTitle {
            loggedFirstTitle = true
            logLine("首次刷新成功：菜单栏标题=\(plain)")
        }

        var spendHelp = money(today.cost, symbol)
        if let primary {
            var parts: [String] = []
            for provider in primary.providers ?? [] {
                let value = provider.spent.map {
                    money($0, primary.symbol ?? symbol)
                } ?? "—"
                parts.append("\(provider.name ?? provider.id ?? "?"): \(value)")
            }
            if let manual = spend?.manual, (manual.amount ?? 0) != 0 {
                parts.append("手动: \(money(manual.amount ?? 0, primary.symbol ?? symbol))")
            }
            if !parts.isEmpty {
                spendHelp = "消费 \(money(primary.total, primary.symbol ?? symbol))（"
                    + parts.joined(separator: " / ") + "）"
            }
        }
        statusItem.button?.toolTip =
            "今日 \(cn(totalTokens)) tokens · \(spendHelp) · \(countText(today.events)) 次请求"
    }

    private func ensurePanel() -> NSHostingController<PanelView> {
        if let panel { return panel }
        let host = NSHostingController(rootView: PanelView(model: model))
        host.view.wantsLayer = true
        host.view.layer?.backgroundColor = NSColor.clear.cgColor
        // 高度跟着内容走：SwiftUI 算完再回写 popover，不再写死 530。
        host.sizingOptions = [.preferredContentSize]
        panelSizeObservation = host.observe(\.preferredContentSize, options: [.new]) {
            [weak self] _, change in
            guard let self, let size = change.newValue, size.height > 100 else { return }
            self.applyPanelSize(size)
        }
        panel = host
        return host
    }

    /// 内容高度自适应；超过屏幕可见区就停在最大值，由面板内部滚动。
    private func applyPanelSize(_ size: NSSize) {
        let height = min(max(size.height, 200), panelMaxHeight())
        let target = NSSize(width: kPanelWidth, height: height)
        guard abs(popover.contentSize.height - target.height) > 0.5
            || abs(popover.contentSize.width - target.width) > 0.5 else { return }
        popover.contentSize = target
    }

    /// 系统浅色/深色（不跟菜单栏的壁纸自适应走）。
    private var systemIsDark: Bool {
        NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
    }

    /// 把面板的 AppKit 外观和 SwiftUI colorScheme 一起切到系统外观。
    /// popover 单独设 appearance，否则会继承菜单栏的 VibrantDark。
    private func applyPanelAppearance() {
        let isDark = systemIsDark
        let appearance = NSAppearance(named: isDark ? .darkAqua : .aqua)
        popover.appearance = appearance
        popover.contentViewController?.view.window?.appearance = appearance
        panel?.view.appearance = appearance
        model.colorScheme = isDark ? .dark : .light
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        model.reload()
        model.refreshBalanceIfNeeded()
        let host = ensurePanel()
        applyPanelAppearance()
        popover.contentViewController = host
        host.view.layoutSubtreeIfNeeded()
        let preferred = host.preferredContentSize
        let height = preferred.height > 100
            ? min(preferred.height, panelMaxHeight()) : kPanelDefaultHeight
        popover.contentSize = NSSize(width: kPanelWidth, height: height)
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        DispatchQueue.main.async { [weak self] in
            self?.applyPanelAppearance()
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
        host.sizingOptions = [.preferredContentSize]
        let appearanceName = ProcessInfo.processInfo.environment["TOKENLENSBAR_APPEARANCE"] == "light"
            ? NSAppearance.Name.aqua : NSAppearance.Name.darkAqua
        host.view.appearance = NSAppearance(named: appearanceName)
        host.view.wantsLayer = true
        host.view.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor

        let window = NSWindow(
            contentRect: NSRect(origin: .zero,
                                size: NSSize(width: kPanelWidth, height: kPanelDefaultHeight)),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentViewController = host
        window.appearance = NSAppearance(named: appearanceName)
        window.orderFrontRegardless()
        host.view.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.35))

        // 内容量出来后再把窗口定到实际高度。
        let preferred = host.preferredContentSize
        let size = NSSize(width: kPanelWidth,
                          height: min(max(preferred.height, 120), panelMaxHeight()))
        window.setContentSize(size)
        host.view.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.35))

        let captureSize = host.view.bounds.size.width > 1 ? host.view.bounds.size : size
        let scale: CGFloat = 2
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: Int(captureSize.width * scale),
            pixelsHigh: Int(captureSize.height * scale),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ) else { return }
        rep.size = captureSize
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
        if let spend = snapshot.todaySpend {
            for bucket in spend.currencies ?? [] {
                print("\n消费 \(bucket.symbol ?? symbol)\(String(format: "%.2f", bucket.total))")
                for provider in bucket.providers ?? [] {
                    if let value = provider.spent {
                        print("  \(provider.name ?? provider.id ?? "?")  \(String(format: "%.2f", value))")
                    } else {
                        print("  \(provider.name ?? provider.id ?? "?")  —")
                    }
                }
                if let manual = spend.manual, (manual.amount ?? 0) != 0 {
                    print("  手动补记  \(String(format: "%.2f", manual.amount ?? 0))")
                }
            }
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
