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
let kHoverWidth: CGFloat = 238
let kHoverKey = "TokenLensBarHoverPreview"

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

/// 菜单栏 / 悬停 HUD / 右键摘要共用的一套「今日」口径。
func todayFigures(_ snapshot: Snapshot) -> (tokens: Double, cost: Double, events: Int, symbol: String) {
    let today = snapshot.windows?["today"]
    let spend = snapshot.todaySpend
    let symbol = snapshot.symbol ?? "¥"
    let manualTokens = Double(spend?.manual?.tokens ?? 0)
    let manualAmount = spend?.manual?.amount ?? 0
    let primary = spend?.currencies?.first
    let hasSample = primary?.providers?.contains { $0.spent != nil } == true
    let showSpend = primary != nil && (hasSample || manualAmount != 0 || manualTokens > 0)
    let cost = showSpend ? (primary?.total ?? 0) : (today?.cost ?? 0)
    return (tokens: (today?.tokens ?? 0) + manualTokens,
            cost: cost,
            events: today?.events ?? 0,
            symbol: symbol)
}

func topSlice(_ slices: [Snapshot.Slice]?) -> Snapshot.Slice? {
    slices?.max { $0.tokens < $1.tokens }
}

func firstBalance(_ snapshot: Snapshot) -> (name: String, value: String)? {
    guard let balances = snapshot.balances, balances.configured == true else { return nil }
    for provider in balances.providers ?? [] {
        let values = (provider.items ?? []).map {
            balanceText($0.total ?? 0, currency: $0.currency ?? "CNY")
        }
        if !values.isEmpty {
            return (provider.name ?? provider.id ?? "余额",
                    values.joined(separator: " / "))
        }
    }
    return nil
}

func cachePercent(_ snapshot: Snapshot) -> String {
    guard let rate = snapshot.cacheRate, rate.isFinite else { return "—" }
    return "\(Int((rate * 100).rounded()))%"
}

extension Notification.Name {
    static let tokenLensBarCopied = Notification.Name("TokenLensBarCopied")
}

func copyToPasteboard(_ text: String) {
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !trimmed.isEmpty else { return }
    let pasteboard = NSPasteboard.general
    pasteboard.clearContents()
    pasteboard.setString(trimmed, forType: .string)
    NotificationCenter.default.post(name: .tokenLensBarCopied, object: nil)
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

    func flash(_ message: String, seconds: Double) {
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

/// 面板里的行：悬停高亮，点击复制。
private struct CopyableRow: ViewModifier {
    let text: String
    var helpText: String = "点击复制"
    @State private var hovering = false

    func body(content: Content) -> some View {
        content
            .contentShape(Rectangle())
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(Color.primary.opacity(hovering ? 0.07 : 0))
                    .padding(.horizontal, 6)
            )
            .onHover { hovering = $0 }
            .onTapGesture { copyToPasteboard(text) }
            .help(helpText)
    }
}

extension View {
    func copyable(_ text: String, help: String = "点击复制") -> some View {
        modifier(CopyableRow(text: text, helpText: help))
    }
}

// MARK: - 悬停预览

struct MiniBars: View {
    let values: [Double]

    var body: some View {
        GeometryReader { geo in
            let maxValue = max(values.max() ?? 0, 1)
            let count = max(values.count, 1)
            let gap: CGFloat = 2
            let width = max(2, (geo.size.width - gap * CGFloat(count - 1)) / CGFloat(count))
            HStack(alignment: .bottom, spacing: gap) {
                ForEach(Array(values.enumerated()), id: \.offset) { index, value in
                    RoundedRectangle(cornerRadius: 1.5, style: .continuous)
                        .fill(index == values.count - 1
                              ? Color.accentColor
                              : Color.accentColor.opacity(0.35))
                        .frame(width: width,
                               height: max(2, geo.size.height * value / maxValue))
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        }
    }
}

/// 鼠标悬停菜单栏时的小卡片：只读缓存，不额外起 Python。
struct HoverPreview: View {
    let snapshot: Snapshot?
    let lastLoaded: Date?

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                Text("今日")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer(minLength: 8)
                if let lastLoaded {
                    Text(lastLoaded, style: .time)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .monospacedDigit()
                }
            }

            if let snapshot, snapshot.ok {
                let figures = todayFigures(snapshot)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(cn(figures.tokens))
                        .font(.system(size: 20, weight: .semibold, design: .rounded))
                        .monospacedDigit()
                    Text(moneyCompactSigned(figures.cost, figures.symbol))
                        .font(.system(size: 20, weight: .semibold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(Color.accentColor)
                }
                Text("\(countText(figures.events)) 次请求 · 缓存 \(cachePercent(snapshot))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)

                if let daily = snapshot.daily, !daily.isEmpty {
                    MiniBars(values: daily.suffix(7).map(\.tokens))
                        .frame(height: 20)
                }

                if let top = topSlice(snapshot.modelsToday) {
                    Divider()
                    HStack(spacing: 6) {
                        Text("最多")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Text(top.model ?? "?")
                            .font(.caption)
                            .lineLimit(1)
                        Spacer(minLength: 6)
                        Text(cn(top.tokens))
                            .font(.caption)
                            .monospacedDigit()
                    }
                }

                if let balance = firstBalance(snapshot) {
                    HStack(spacing: 6) {
                        Text("余额")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        Text(balance.name)
                            .font(.caption)
                            .lineLimit(1)
                        Spacer(minLength: 6)
                        Text(balance.value)
                            .font(.caption)
                            .monospacedDigit()
                    }
                }
            } else {
                Text("还没有数据")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                Text("点开面板看采集状态")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            Text("点菜单栏图标打开 · 右键更多")
                .font(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(12)
        .frame(width: kHoverWidth, alignment: .leading)
        .fixedSize(horizontal: false, vertical: true)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor))
                .overlay(
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .strokeBorder(Color.primary.opacity(0.1))
                )
        )
    }
}

/// 右键菜单操作后的轻量反馈，不抢焦点。
struct ToastView: View {
    let text: String

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
            Text(text)
                .font(.callout)
                .lineLimit(1)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .frame(width: 190, height: 34)
        .background(
            Capsule(style: .continuous)
                .fill(Color(nsColor: .windowBackgroundColor))
                .overlay(
                    Capsule(style: .continuous)
                        .strokeBorder(Color.primary.opacity(0.1))
                )
        )
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
        .onReceive(NotificationCenter.default.publisher(for: .tokenLensBarCopied)) { _ in
            model.flash("已复制", seconds: 2)
        }
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
            .padding(.horizontal, 12)
            .padding(.vertical, 2)
            .copyable("今日消费: \(value)（\(note)）", help: help + "\n\n点击复制")
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
            .copyable("\(title): \(value)")
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
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 2)
            .copyable("\(label): \(value)",
                      help: help.map { $0 + "\n\n点击复制" } ?? "点击复制")
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
            .copyable(copyText)
            Divider()
                .padding(.leading, 12)
        }
    }

    private var copyText: String {
        let name = agent ? (slice.agent ?? "?") : (slice.model ?? "?")
        let price = costText(slice.cost, tokens: slice.tokens, symbol,
                             subscription: slice.subscription ?? false)
        return "\(name) · \(cn(slice.tokens)) tokens · \(price)"
    }
}

// MARK: - 菜单栏两行标题

/// 菜单栏厚度只有 22pt，一行放两段信息太宽：金额在上、token 徽标在下。
/// 不用 NSButtonCell 的两行标题（它按单行高度摆，整块偏上），
/// 直接当按钮的子视图自己画；hitTest 放行，点击/右键/悬停仍走按钮。
final class TwoLineStatusView: NSView {
    static let fallbackWidth: CGFloat = 26

    var tokensText = "…" { didSet { needsDisplay = true } }
    var costText = "" { didSet { needsDisplay = true } }
    var fallback = false { didSet { needsDisplay = true } }

    private static let tokenFont = NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .semibold)
    private static let costFont = NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .semibold)
    /// 两行的墨迹中心（view 坐标，从下往上），和旁边图标中线对齐。
    private static let topInkCenter: CGFloat = 17.0
    private static let badgeInkCenter: CGFloat = 6.25
    private static let badgeHeight: CGFloat = 10.5
    private static let badgePadding: CGFloat = 3

    /// 状态项宽度：取两行较宽者，徽标那行加上内边距，再留左右白边。
    static func measure(_ view: TwoLineStatusView) -> CGFloat {
        guard !view.fallback else { return fallbackWidth }
        let tokens = NSAttributedString(string: view.tokensText, attributes: [.font: tokenFont])
        let cost = NSAttributedString(string: view.costText, attributes: [.font: costFont])
        let widest = max(cost.size().width, tokens.size().width + badgePadding * 2)
        return ceil(widest) + 6
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func draw(_ dirtyRect: NSRect) {
        let width = bounds.width
        if fallback {
            let dash = NSAttributedString(string: "—", attributes: [.font: Self.costFont])
            let size = dash.size()
            dash.draw(at: NSPoint(x: (width - size.width) / 2,
                                  y: Self.topInkCenter - size.height / 2))
            return
        }
        // 上行：金额
        let cost = NSAttributedString(
            string: costText,
            attributes: [.font: Self.costFont, .foregroundColor: NSColor.labelColor])
        let costSize = cost.size()
        cost.draw(at: NSPoint(x: (width - costSize.width) / 2,
                              y: Self.topInkCenter - costSize.height / 2))
        // 下行：token 量徽标，底色和文字反色；语义色随菜单栏浅/深色自动翻转
        let tokens = NSAttributedString(
            string: tokensText,
            attributes: [.font: Self.tokenFont, .foregroundColor: NSColor.textBackgroundColor])
        let tokenSize = tokens.size()
        let pillWidth = tokenSize.width + Self.badgePadding * 2
        let pill = NSBezierPath(
            roundedRect: NSRect(x: (width - pillWidth) / 2,
                                y: Self.badgeInkCenter - Self.badgeHeight / 2,
                                width: pillWidth, height: Self.badgeHeight),
            xRadius: 2.5, yRadius: 2.5)
        NSColor.labelColor.setFill()
        pill.fill()
        tokens.draw(at: NSPoint(x: (width - tokenSize.width) / 2,
                                y: Self.badgeInkCenter - tokenSize.height / 2))
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let model = PanelModel()
    private var panel: NSHostingController<PanelView>?
    private var panelSizeObservation: NSKeyValueObservation?
    private var statusItem: NSStatusItem!
    private let statusView = TwoLineStatusView(frame: .zero)
    private var popover: NSPopover!
    private var timer: Timer?
    private var cancellables = Set<AnyCancellable>()
    private var loggedFirstTitle = false
    private var themeObserver: NSObjectProtocol?
    private var wakeObservers: [NSObjectProtocol] = []
    private var lastWakeRefresh = Date.distantPast
    private var hoverGlobalMonitor: Any?
    private var hoverLocalMonitor: Any?
    private var hoverWork: DispatchWorkItem?
    private var hoverShown = false
    private var hoverPanel: NSPanel?
    private var toastPanel: NSPanel?
    private var toastWork: DispatchWorkItem?
    private var isShowingStatusMenu = false
    private var hoverSuppressedUntil = Date.distantPast

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: TwoLineStatusView.fallbackWidth)
        statusItem.button?.attributedTitle = NSAttributedString(string: "")
        statusItem.button?.toolTip = "TokenLensBar：本地 token 用量"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(statusItemClicked(_:))
        _ = statusItem.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        statusView.frame = statusItem.button?.bounds ?? .zero
        statusView.autoresizingMask = [.width, .height]
        statusItem.button?.addSubview(statusView)

        popover = NSPopover()
        popover.behavior = .transient
        popover.animates = true
        installHoverMonitoring()

        // 系统浅色/深色切换时，面板若开着就立刻跟着变。
        themeObserver = DistributedNotificationCenter.default().addObserver(
            forName: NSNotification.Name("AppleInterfaceThemeChangedNotification"),
            object: nil, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            self.model.colorScheme = self.systemIsDark ? .dark : .light
            if self.popover.isShown {
                self.applyPanelAppearance()
            }
            if self.hoverShown {
                self.hideHover()
            }
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

        model.colorScheme = systemIsDark ? .dark : .light

        logLine("启动：statusItem=\(statusItem.button == nil ? "nil" : "ok")")
        if ProcessInfo.processInfo.environment["TOKENLENSBAR_DEBUG_HOVER"] == "1" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                guard let self, let button = self.statusItem.button,
                      let window = button.window else { return }
                let frame = window.convertToScreen(button.convert(button.bounds, to: nil))
                logLine("调试：状态栏按钮 \(frame)")
            }
        }
        if ProcessInfo.processInfo.environment["TOKENLENSBAR_DEMO_PANEL"] == "1" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                self?.togglePopover()
            }
        }
        if ProcessInfo.processInfo.environment["TOKENLENSBAR_DEMO_MENU"] == "1" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
                self?.showStatusMenu()
            }
        }
        model.reload()

        let timer = Timer.scheduledTimer(withTimeInterval: kRefreshInterval, repeats: true) { [weak self] _ in
            self?.model.reload()
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func updateStatusItem(_ snapshot: Snapshot?) {
        guard let snapshot, snapshot.ok, let today = snapshot.windows?["today"] else {
            statusView.fallback = true
            statusItem.length = TwoLineStatusView.fallbackWidth
            return
        }
        let figures = todayFigures(snapshot)
        let symbol = figures.symbol
        let spend = snapshot.todaySpend
        let primary = spend?.currencies?.first

        statusView.fallback = false
        statusView.tokensText = cn(figures.tokens)
        statusView.costText = moneyCompactSigned(figures.cost, symbol)
        statusItem.length = TwoLineStatusView.measure(statusView)
        let plain = cn(figures.tokens) + "  " + moneyCompactSigned(figures.cost, symbol)
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
        let richToolTip =
            "今日 \(cn(figures.tokens)) tokens · \(spendHelp) · \(countText(figures.events)) 次请求"
        statusItem.button?.toolTip = hoverPreviewEnabled ? nil : richToolTip
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

    // MARK: 悬停 / 右键 / 复制

    private var hoverPreviewEnabled: Bool {
        UserDefaults.standard.object(forKey: kHoverKey) as? Bool ?? true
    }

    private func installHoverMonitoring() {
        guard let button = statusItem.button else { return }
        let area = NSTrackingArea(
            rect: .zero,
            options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        button.addTrackingArea(area)

        // 事件驱动：全局监听覆盖菜单栏，本地监听覆盖面板内部。
        // 不用轮询，空闲时不产生唤醒，也不需要用 beginActivity 顶着 App Nap。
        hoverGlobalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved]) {
            [weak self] _ in
            self?.evaluateHover()
        }
        hoverLocalMonitor = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved]) {
            [weak self] event in
            self?.evaluateHover()
            return event
        }
    }

    @objc private func mouseEntered(with event: NSEvent) {
        guard hoverPreviewEnabled else { return }
        scheduleHover()
    }

    @objc private func mouseExited(with event: NSEvent) {
        cancelHover()
        hideHover()
    }

    private func evaluateHover() {
        guard hoverPreviewEnabled,
              let button = statusItem.button,
              let window = button.window else { return }
        let frame = window.convertToScreen(button.convert(button.bounds, to: nil))
        if frame.contains(NSEvent.mouseLocation) {
            scheduleHover()
        } else {
            cancelHover()
            hideHover()
        }
    }

    private func scheduleHover() {
        guard hoverPreviewEnabled, !hoverShown, hoverWork == nil,
              !popover.isShown, statusItem.menu == nil,
              Date() >= hoverSuppressedUntil,
              toastPanel?.isVisible != true else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.hoverWork = nil
            self.showHover()
        }
        hoverWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.03, execute: work)
    }

    private func cancelHover() {
        hoverWork?.cancel()
        hoverWork = nil
    }

    private func debugHover(_ message: String) {
        guard ProcessInfo.processInfo.environment["TOKENLENSBAR_DEBUG_HOVER"] == "1" else { return }
        logLine("悬停：\(message)")
    }

    private func showHover() {
        guard hoverPreviewEnabled,
              let button = statusItem.button,
              let window = button.window else { return }
        let buttonFrame = window.convertToScreen(button.convert(button.bounds, to: nil))
        guard buttonFrame.contains(NSEvent.mouseLocation),
              !popover.isShown, statusItem.menu == nil else { return }

        let dark = systemIsDark
        let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let root = HoverPreview(snapshot: model.snapshot, lastLoaded: model.lastLoaded)
            .environment(\.colorScheme, dark ? .dark : .light)
        let host = NSHostingView(rootView: root)
        host.appearance = appearance
        host.frame = NSRect(x: 0, y: 0, width: kHoverWidth, height: 10)
        host.layoutSubtreeIfNeeded()
        let size = NSSize(width: kHoverWidth, height: max(host.fittingSize.height, 60))
        host.frame = NSRect(origin: .zero, size: size)
        host.layoutSubtreeIfNeeded()

        if hoverPanel == nil {
            hoverPanel = makeFloatingPanel()
        }
        guard let panel = hoverPanel else { return }
        panel.appearance = appearance
        panel.contentView = host
        panel.setContentSize(size)
        place(panel: panel, size: size, under: buttonFrame, screen: window.screen)
        panel.orderFrontRegardless()
        hoverShown = true
        debugHover("显示 \(size.width)x\(size.height) at \(panel.frame)")
    }

    private func hideHover() {
        guard hoverShown else { return }
        hoverShown = false
        hoverPanel?.orderOut(nil)
        debugHover("隐藏")
    }

    private func makeFloatingPanel() -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: kHoverWidth, height: 120),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.level = .statusBar
        panel.collectionBehavior = [
            .canJoinAllSpaces, .transient, .ignoresCycle, .fullScreenAuxiliary
        ]
        panel.ignoresMouseEvents = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.animationBehavior = .utilityWindow
        return panel
    }

    private func place(panel: NSPanel, size: NSSize, under buttonFrame: NSRect, screen: NSScreen?) {
        let visible = (screen ?? NSScreen.main)?.visibleFrame
            ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        var x = buttonFrame.midX - size.width / 2
        x = min(max(x, visible.minX + 8), visible.maxX - size.width - 8)
        var y = buttonFrame.minY - size.height - 6
        y = max(y, visible.minY + 8)
        panel.setFrame(NSRect(x: x, y: y, width: size.width, height: size.height),
                       display: true)
    }

    @objc private func statusItemClicked(_ sender: NSStatusBarButton) {
        guard !isShowingStatusMenu else { return }
        if NSApp.currentEvent?.type == .rightMouseUp {
            showStatusMenu()
        } else {
            togglePopover()
        }
    }

    private func showStatusMenu() {
        hoverSuppressedUntil = Date().addingTimeInterval(1.0)
        cancelHover()
        hideHover()
        isShowingStatusMenu = true
        let menu = buildStatusMenu()
        statusItem.menu = menu
        statusItem.button?.performClick(nil)
        statusItem.menu = nil
        isShowingStatusMenu = false
    }

    private func buildStatusMenu() -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false
        addStatusItem(menu, "打开面板", #selector(menuOpenPanel))
        addStatusItem(menu, "打开看板", #selector(menuOpenDashboard))
        addStatusItem(menu, "立即采集", #selector(menuScan))
        menu.addItem(.separator())
        addStatusItem(menu, "复制今日摘要", #selector(menuCopySummary))
        menu.addItem(.separator())
        let hover = NSMenuItem(title: "悬停预览",
                               action: #selector(menuToggleHover),
                               keyEquivalent: "")
        hover.target = self
        hover.state = hoverPreviewEnabled ? .on : .off
        menu.addItem(hover)
        menu.addItem(.separator())
        addStatusItem(menu, "退出", #selector(menuQuit))
        return menu
    }

    private func addStatusItem(_ menu: NSMenu, _ title: String, _ action: Selector) {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
    }

    @objc private func menuOpenPanel() {
        if !popover.isShown {
            togglePopover()
        }
    }

    @objc private func menuOpenDashboard() {
        model.openDashboard()
    }

    @objc private func menuScan() {
        model.scan()
        showToast("开始采集…")
    }

    @objc private func menuCopySummary() {
        guard let snapshot = model.snapshot, snapshot.ok else {
            showToast("还没有数据")
            return
        }
        copyToPasteboard(summaryText(snapshot))
        showToast("已复制今日摘要")
    }

    @objc private func menuToggleHover() {
        let enabled = !hoverPreviewEnabled
        UserDefaults.standard.set(enabled, forKey: kHoverKey)
        if !enabled {
            cancelHover()
            hideHover()
        }
        if let snapshot = model.snapshot {
            updateStatusItem(snapshot)
        }
        showToast(enabled ? "悬停预览已开启" : "悬停预览已关闭")
    }

    @objc private func menuQuit() {
        NSApp.terminate(nil)
    }

    private func summaryText(_ snapshot: Snapshot) -> String {
        let figures = todayFigures(snapshot)
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm"
        var lines = [
            "TokenLensBar · \(formatter.string(from: Date()))",
            "今日  \(cn(figures.tokens)) tokens · "
                + "\(moneyCompactSigned(figures.cost, figures.symbol)) · "
                + "\(countText(figures.events)) 次请求",
            "缓存  \(cachePercent(snapshot))",
        ]
        if let top = topSlice(snapshot.modelsToday) {
            lines.append("最多  \(top.model ?? "?")  \(cn(top.tokens)) tokens")
        }
        if let balance = firstBalance(snapshot) {
            lines.append("余额  \(balance.name)  \(balance.value)")
        }
        return lines.joined(separator: "\n")
    }

    private func showToast(_ text: String) {
        guard let button = statusItem.button, let window = button.window else { return }
        cancelHover()
        hideHover()
        if toastPanel == nil {
            toastPanel = makeFloatingPanel()
        }
        guard let panel = toastPanel else { return }
        let dark = systemIsDark
        let appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        let root = ToastView(text: text)
            .environment(\.colorScheme, dark ? .dark : .light)
        let host = NSHostingView(rootView: root)
        host.appearance = appearance
        host.frame = NSRect(x: 0, y: 0, width: 190, height: 34)
        panel.appearance = appearance
        panel.contentView = host
        panel.setContentSize(NSSize(width: 190, height: 34))
        let frame = window.convertToScreen(button.convert(button.bounds, to: nil))
        place(panel: panel, size: NSSize(width: 190, height: 34),
              under: frame, screen: window.screen)
        panel.orderFrontRegardless()
        toastWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            self?.toastPanel?.orderOut(nil)
        }
        toastWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.4, execute: work)
    }

    @objc private func togglePopover() {
        hoverSuppressedUntil = Date().addingTimeInterval(0.8)
        cancelHover()
        hideHover()
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
        NSApp.activate(ignoringOtherApps: true)
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
        if let index = args.firstIndex(of: "--render-hover") {
            let path = index + 1 < args.count ? args[index + 1] : "/tmp/tokenlensbar-hover.png"
            renderHover(path: path)
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

    private static func renderHover(path: String) {
        let model = PanelModel()
        model.snapshot = PyBridge().load()
        model.lastLoaded = Date()
        let scheme: ColorScheme = ProcessInfo.processInfo.environment["TOKENLENSBAR_APPEARANCE"] == "light"
            ? .light : .dark
        let root = HoverPreview(snapshot: model.snapshot, lastLoaded: model.lastLoaded)
            .environment(\.colorScheme, scheme)
        let host = NSHostingView(rootView: root)
        host.frame = NSRect(x: 0, y: 0, width: kHoverWidth, height: 10)
        host.layoutSubtreeIfNeeded()
        let size = NSSize(width: kHoverWidth, height: max(host.fittingSize.height, 60))
        host.frame = NSRect(origin: .zero, size: size)
        host.layoutSubtreeIfNeeded()

        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isOpaque = false
        window.backgroundColor = .clear
        window.appearance = NSAppearance(named: scheme == .dark ? .darkAqua : .aqua)
        window.contentView = host
        window.orderFrontRegardless()
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.3))

        let captureSize = host.bounds.size
        let scale: CGFloat = 2
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: Int(captureSize.width * scale),
            pixelsHigh: Int(captureSize.height * scale),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ) else { return }
        rep.size = captureSize
        host.cacheDisplay(in: host.bounds, to: rep)
        if let data = rep.representation(using: .png, properties: [:]) {
            try? data.write(to: URL(fileURLWithPath: path))
            print("已输出：\(path)（\(Int(captureSize.width))x\(Int(captureSize.height))）")
        }
        window.orderOut(nil)
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
