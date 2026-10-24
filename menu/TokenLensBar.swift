import Cocoa
import SwiftUI
import Charts

// 菜单栏外壳 + 点开的图表面板。
// 计算全在 tokenlens.py 里；这里只负责取数和画图。
// 图表用 NSBezierPath 原生绘制，不引入 WebView 进程，所以点开是瞬时的。

let kRefreshInterval: TimeInterval = 60
let kDashboardPort = 8787
let kPanelWidth: CGFloat = 344

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
    private let scriptPath = ProcessInfo.processInfo.environment["TOKENLENS_SCRIPT"] ?? kScriptPath

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
// MARK: - 原生小组件

/// 原生 Swift Charts 柱状图：柱高是 Token，柱顶两行是 Token / 金额。
struct DailyBarChart: View {
    let days: [Snapshot.DayPoint]
    let symbol: String

    private var maxValue: Double {
        max(1, days.map { $0.tokens }.max() ?? 1)
    }

    var body: some View {
        Chart(days, id: \.date) { day in
            BarMark(
                x: .value("日期", shortDate(day.date)),
                y: .value("Token", day.tokens)
            )
            .foregroundStyle(Color.accentColor)
            .cornerRadius(2)
            .annotation(position: .top, spacing: 2) {
                if day.tokens > 0 {
                    VStack(spacing: 0) {
                        Text(cnTiny(day.tokens))
                            .font(.system(size: 9, weight: .medium))
                            .foregroundStyle(.primary)
                        Text(costText(day.cost ?? 0, tokens: day.tokens, symbol))
                            .font(.system(size: 8))
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text("—")
                        .font(.system(size: 9))
                        .foregroundStyle(.secondary)
                }
            }
        }
        .chartYAxis(.hidden)
        .chartYScale(domain: 0...(maxValue * 1.55))
        .chartXAxis {
            AxisMarks { _ in
                AxisGridLine().foregroundStyle(.clear)
                AxisTick().foregroundStyle(.clear)
                AxisValueLabel()
            }
        }
        .chartPlotStyle { plot in
            plot.padding(.top, 2)
        }
        .background(Color.clear)
    }
}

/// 原生分组卡片：系统 controlBackgroundColor + separatorColor，跟随浅色/深色。
final class CardView: NSView {
    override var isOpaque: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        let rect = bounds.insetBy(dx: 0.5, dy: 0.5)
        let path = NSBezierPath(roundedRect: rect, xRadius: 10, yRadius: 10)
        NSColor.controlBackgroundColor.setFill()
        path.fill()
        NSColor.separatorColor.setStroke()
        path.lineWidth = 1
        path.stroke()
    }
}

/// 面板底色。用 windowBackgroundColor，浅色/深色都由系统决定。
final class PanelBackgroundView: NSView {
    override var isOpaque: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.windowBackgroundColor.setFill()
        bounds.fill()
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

func clockText(_ epoch: Double) -> String {
    let date = Date(timeIntervalSince1970: epoch)
    let formatter = DateFormatter()
    formatter.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm" : "MM-dd HH:mm"
    return formatter.string(from: date)
}

private func balanceErrorColor(_ message: String) -> NSColor {
    if message.contains("未配置") { return .secondaryLabelColor }
    if message.contains("Key") || message.contains("权限") { return .systemRed }
    return .systemOrange
}

// MARK: - 面板

final class PanelController: NSViewController {
    private let bridge = PyBridge()
    private let contentStack = NSStackView()
    private let footerStatus = NSTextField(labelWithString: "")
    private var lastHeight: CGFloat = 0
    private var snapshotValue: Snapshot?

    weak var popover: NSPopover?

    var snapshot: Snapshot? {
        get { snapshotValue }
        set {
            snapshotValue = newValue
            if isViewLoaded { rebuild() }
        }
    }

    override func loadView() {
        let root = PanelBackgroundView(frame: NSRect(x: 0, y: 0, width: kPanelWidth, height: 620))
        self.view = root

        contentStack.orientation = .vertical
        contentStack.alignment = .leading
        contentStack.spacing = 8
        contentStack.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(contentStack)
        NSLayoutConstraint.activate([
            contentStack.topAnchor.constraint(equalTo: root.topAnchor, constant: 12),
            contentStack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 12),
            contentStack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -12),
            contentStack.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -10),
        ])

        footerStatus.font = .systemFont(ofSize: 10)
        footerStatus.textColor = .secondaryLabelColor
        footerStatus.alignment = .right
        footerStatus.lineBreakMode = .byTruncatingTail
        footerStatus.translatesAutoresizingMaskIntoConstraints = false
        footerStatus.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        preferredContentSize = NSSize(width: kPanelWidth, height: 620)
        rebuild()
    }

    // MARK: - 重建

    private func rebuild() {
        guard isViewLoaded else { return }
        for view in contentStack.arrangedSubviews {
            contentStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }

        guard let snap = snapshot, snap.ok else {
            addFullWidth(makeLabel("还没有数据", size: 13, color: .secondaryLabelColor),
                         to: contentStack)
            addFooter()
            applySize()
            return
        }

        addHeader(snap)
        addBalanceCard(snap)
        addTodayCard(snap)
        addChartCard(snap)
        addHistoryCard(snap)
        addFooter()
        applySize()
    }

    private func applySize() {
        guard isViewLoaded else { return }
        view.layoutSubtreeIfNeeded()
        let fitting = contentStack.fittingSize
        let height = max(160, ceil(fitting.height + 22))
        guard abs(height - lastHeight) > 0.5 else { return }
        lastHeight = height
        let size = NSSize(width: kPanelWidth, height: height)
        preferredContentSize = size
        view.frame = NSRect(origin: .zero, size: size)
        popover?.contentSize = size
        view.layoutSubtreeIfNeeded()
    }

    // MARK: - 顶部摘要

    private func addHeader(_ snap: Snapshot) {
        let header = NSStackView()
        header.orientation = .vertical
        header.alignment = .leading
        header.spacing = 6
        header.translatesAutoresizingMaskIntoConstraints = false

        header.addArrangedSubview(makeLabel("今日用量", size: 13, weight: .semibold))

        let metrics = NSStackView()
        metrics.orientation = .horizontal
        metrics.alignment = .top
        metrics.distribution = .fillEqually
        metrics.spacing = 12
        metrics.translatesAutoresizingMaskIntoConstraints = false
        metrics.addArrangedSubview(metricColumn("金额",
                                                money(snap.windows?["today"]?.cost ?? 0,
                                                      snap.symbol ?? "¥"),
                                                size: 20))
        metrics.addArrangedSubview(metricColumn("Token",
                                                cn(snap.windows?["today"]?.tokens ?? 0),
                                                size: 20))
        addFullWidth(metrics, to: header)

        let rate = Int(((snap.cacheRate ?? 0) * 100).rounded())
        let caption = NSMutableAttributedString()
        caption.append(NSAttributedString(string: "缓存命中 ", attributes: [
            .font: NSFont.systemFont(ofSize: 11),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]))
        caption.append(NSAttributedString(string: "\(rate)%", attributes: [
            .font: NSFont.systemFont(ofSize: 11, weight: .medium),
            .foregroundColor: NSColor.controlAccentColor,
        ]))
        caption.append(NSAttributedString(
            string: " · \(countText(snap.windows?["today"]?.events ?? 0)) 次请求",
            attributes: [
                .font: NSFont.systemFont(ofSize: 11),
                .foregroundColor: NSColor.secondaryLabelColor,
            ]))
        let captionField = NSTextField(labelWithAttributedString: caption)
        captionField.translatesAutoresizingMaskIntoConstraints = false
        captionField.lineBreakMode = .byTruncatingTail
        addFullWidth(captionField, to: header)

        addFullWidth(header, to: contentStack)
    }

    private func metricColumn(_ title: String, _ value: String, size: CGFloat) -> NSStackView {
        let column = NSStackView()
        column.orientation = .vertical
        column.alignment = .leading
        column.spacing = 1
        column.translatesAutoresizingMaskIntoConstraints = false
        column.addArrangedSubview(makeLabel(title, size: 11, color: .secondaryLabelColor))
        column.addArrangedSubview(makeLabel(value, size: size, weight: .semibold,
                                           monospaced: true))
        return column
    }

    // MARK: - 余额

    private func addBalanceCard(_ snap: Snapshot) {
        guard let balances = snap.balances, balances.configured == true,
              let providers = balances.providers, !providers.isEmpty else { return }

        let (card, stack) = makeCard()
        let (update, updateColor) = balanceUpdate(providers)
        let right = update.isEmpty ? nil : makeLabel(update, size: 10, color: updateColor)
        addFullWidth(cardTitle("账户余额", right: right), to: stack)

        for provider in providers {
            let name = provider.name ?? provider.id ?? "?"
            let items = provider.items ?? []
            if items.isEmpty {
                let row = horizontalRow()
                row.addArrangedSubview(makeLabel(name, size: 12))
                row.addArrangedSubview(makeSpacer())
                let message = provider.error ?? "读取中…"
                row.addArrangedSubview(makeLabel(message, size: 11,
                                                 color: balanceErrorColor(message)))
                addFullWidth(row, to: stack)
                continue
            }
            for (index, item) in items.enumerated() {
                let row = horizontalRow()
                row.addArrangedSubview(makeLabel(index == 0 ? name : (item.currency ?? ""),
                                                 size: index == 0 ? 12 : 10,
                                                 color: index == 0 ? .labelColor : .secondaryLabelColor))
                row.addArrangedSubview(makeSpacer())
                row.addArrangedSubview(makeLabel(
                    balanceText(item.total ?? 0, currency: item.currency ?? "CNY"),
                    size: 12, weight: .semibold,
                    color: item.available == false ? .systemRed : .labelColor,
                    monospaced: true))
                addFullWidth(row, to: stack)
            }
        }

        addFullWidth(card, to: contentStack)
    }

    private func balanceUpdate(_ providers: [Snapshot.BalanceProvider]) -> (String, NSColor) {
        let fetched = providers.compactMap { $0.fetchedAt }.max()
        guard let fetched else { return ("", .secondaryLabelColor) }
        if providers.contains(where: { !($0.error ?? "").isEmpty }) {
            return ("更新失败", .systemOrange)
        }
        if providers.contains(where: { $0.stale == true }) {
            return ("已过期", .systemOrange)
        }
        return ("\(clockText(fetched)) 更新", .secondaryLabelColor)
    }

    // MARK: - 今日明细

    private func addTodayCard(_ snap: Snapshot) {
        let (card, stack) = makeCard()
        let symbol = snap.symbol ?? "¥"

        stack.addArrangedSubview(makeLabel("按模型", size: 11, weight: .semibold,
                                           color: .secondaryLabelColor))
        let models = snap.modelsToday ?? []
        if models.isEmpty {
            stack.addArrangedSubview(makeLabel("今天还没有用量", size: 11,
                                               color: .secondaryLabelColor))
        } else if let grid = makeGrid(models.map {
            ($0.model ?? "?", cn($0.tokens),
             costText($0.cost, tokens: $0.tokens, symbol,
                      subscription: $0.subscription ?? false))
        }) {
            addGrid(grid, to: stack)
        }
        stack.setCustomSpacing(8, after: stack.arrangedSubviews.last!)

        stack.addArrangedSubview(makeLabel("按 Agent", size: 11, weight: .semibold,
                                           color: .secondaryLabelColor))
        let agents = snap.agentsToday ?? []
        if agents.isEmpty {
            stack.addArrangedSubview(makeLabel("今天还没有用量", size: 11,
                                               color: .secondaryLabelColor))
        } else if let grid = makeGrid(agents.map {
            ($0.agent ?? "?", cn($0.tokens),
             costText($0.cost, tokens: $0.tokens, symbol,
                      subscription: $0.subscription ?? false))
        }) {
            addGrid(grid, to: stack)
        }

        addFullWidth(card, to: contentStack)
    }

    // MARK: - 近 7 天

    private func addChartCard(_ snap: Snapshot) {
        let (card, stack) = makeCard()
        addFullWidth(cardTitle("近 7 天",
                               right: makeLabel("Token / 金额", size: 9,
                                                color: .tertiaryLabelColor)),
                     to: stack)

        let days = snap.daily ?? []
        if days.isEmpty {
            stack.addArrangedSubview(makeLabel("暂无数据", size: 11,
                                               color: .secondaryLabelColor))
        } else {
            let host = NSHostingView(rootView: DailyBarChart(
                days: days, symbol: snap.symbol ?? "¥"))
            host.translatesAutoresizingMaskIntoConstraints = false
            host.wantsLayer = true
            host.layer?.backgroundColor = NSColor.clear.cgColor
            stack.addArrangedSubview(host)
            host.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
            host.heightAnchor.constraint(equalToConstant: 88).isActive = true
        }

        addFullWidth(card, to: contentStack)
    }

    // MARK: - 全部历史

    private func addHistoryCard(_ snap: Snapshot) {
        let (card, stack) = makeCard()
        let symbol = snap.symbol ?? "¥"
        let totalCost = (snap.models ?? []).reduce(0.0) { $0 + $1.cost }

        let titleRow = horizontalRow()
        titleRow.addArrangedSubview(makeLabel("全部历史", size: 12, weight: .semibold))
        titleRow.addArrangedSubview(makeLabel(money(totalCost, symbol), size: 12,
                                              weight: .semibold, monospaced: true))
        titleRow.addArrangedSubview(makeLabel(cn(snap.allTokens ?? 0), size: 12,
                                              weight: .semibold, monospaced: true))
        titleRow.addArrangedSubview(makeSpacer())
        addFullWidth(titleRow, to: stack)

        stack.addArrangedSubview(makeLabel("按模型", size: 11, weight: .semibold,
                                           color: .secondaryLabelColor))
        let models = snap.models ?? []
        if models.isEmpty {
            stack.addArrangedSubview(makeLabel("还没有历史数据", size: 11,
                                               color: .secondaryLabelColor))
        } else if let grid = makeGrid(models.map {
            ($0.model ?? "?", cn($0.tokens),
             costText($0.cost, tokens: $0.tokens, symbol,
                      subscription: $0.subscription ?? false))
        }) {
            addGrid(grid, to: stack)
        }
        stack.setCustomSpacing(8, after: stack.arrangedSubviews.last!)

        stack.addArrangedSubview(makeLabel("按 Agent", size: 11, weight: .semibold,
                                           color: .secondaryLabelColor))
        let agents = snap.agents ?? []
        if agents.isEmpty {
            stack.addArrangedSubview(makeLabel("还没有历史数据", size: 11,
                                               color: .secondaryLabelColor))
        } else if let grid = makeGrid(agents.map {
            ($0.agent ?? "?", cn($0.tokens),
             costText($0.cost, tokens: $0.tokens, symbol,
                      subscription: $0.subscription ?? false))
        }) {
            addGrid(grid, to: stack)
        }

        let footer = makeLabel("共 \(countText(snap.allEvents ?? 0)) 次请求",
                               size: 10, color: .secondaryLabelColor)
        stack.setCustomSpacing(6, after: stack.arrangedSubviews.last!)
        stack.addArrangedSubview(footer)
        addFullWidth(card, to: contentStack)
    }

    // MARK: - 底部

    private func addFooter() {
        let separator = NSBox()
        separator.boxType = .separator
        separator.translatesAutoresizingMaskIntoConstraints = false
        contentStack.addArrangedSubview(separator)
        separator.widthAnchor.constraint(equalTo: contentStack.widthAnchor).isActive = true

        let row = horizontalRow()
        row.addArrangedSubview(makeButton("立即采集", symbol: "arrow.clockwise",
                                          action: #selector(scanNow)))
        row.addArrangedSubview(makeButton("打开看板", symbol: "rectangle.on.rectangle",
                                          action: #selector(openDashboard)))
        row.addArrangedSubview(makeButton("退出", symbol: "power",
                                          action: #selector(quit)))
        row.addArrangedSubview(makeSpacer())
        row.addArrangedSubview(footerStatus)
        footerStatus.widthAnchor.constraint(lessThanOrEqualToConstant: 92).isActive = true
        addFullWidth(row, to: contentStack)
    }

    private func makeButton(_ title: String, symbol: String, action: Selector) -> NSButton {
        let button = NSButton(title: title, target: self, action: action)
        button.bezelStyle = .rounded
        button.controlSize = .small
        button.font = .systemFont(ofSize: 11)
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        button.imagePosition = .imageLeading
        button.imageHugsTitle = true
        button.translatesAutoresizingMaskIntoConstraints = false
        button.setContentHuggingPriority(.required, for: .horizontal)
        return button
    }

    // MARK: - 基础小组件

    private func makeCard() -> (CardView, NSStackView) {
        let card = CardView()
        card.translatesAutoresizingMaskIntoConstraints = false
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        stack.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: card.topAnchor, constant: 10),
            stack.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: 10),
            stack.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -10),
            stack.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -10),
        ])
        return (card, stack)
    }

    private func cardTitle(_ title: String, right: NSView? = nil) -> NSStackView {
        let row = horizontalRow()
        row.addArrangedSubview(makeLabel(title, size: 12, weight: .semibold))
        row.addArrangedSubview(makeSpacer())
        if let right { row.addArrangedSubview(right) }
        return row
    }

    private func horizontalRow() -> NSStackView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        row.translatesAutoresizingMaskIntoConstraints = false
        return row
    }

    private func makeSpacer() -> NSView {
        let view = NSView()
        view.translatesAutoresizingMaskIntoConstraints = false
        view.setContentHuggingPriority(.defaultLow, for: .horizontal)
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        return view
    }

    private func makeLabel(_ text: String, size: CGFloat = 12,
                           weight: NSFont.Weight = .regular,
                           color: NSColor = .labelColor,
                           monospaced: Bool = false) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.font = monospaced
            ? NSFont.monospacedDigitSystemFont(ofSize: size, weight: weight)
            : NSFont.systemFont(ofSize: size, weight: weight)
        field.textColor = color
        field.lineBreakMode = .byTruncatingTail
        field.translatesAutoresizingMaskIntoConstraints = false
        return field
    }

    private func makeGrid(_ rows: [(String, String, String)]) -> NSGridView? {
        guard !rows.isEmpty else { return nil }
        var views: [[NSView]] = []
        for row in rows {
            views.append([
                makeLabel(row.0, size: 12),
                makeLabel(row.1, size: 11, color: .secondaryLabelColor, monospaced: true),
                makeLabel(row.2, size: 12, weight: .medium, monospaced: true),
            ])
        }
        let grid = NSGridView(views: views)
        grid.translatesAutoresizingMaskIntoConstraints = false
        grid.rowSpacing = 2
        grid.columnSpacing = 10
        grid.rowAlignment = .firstBaseline
        grid.column(at: 0).xPlacement = .leading
        grid.column(at: 1).xPlacement = .trailing
        grid.column(at: 2).xPlacement = .trailing
        grid.column(at: 1).width = 72
        grid.column(at: 2).width = 66
        return grid
    }

    private func addGrid(_ grid: NSGridView, to stack: NSStackView) {
        stack.addArrangedSubview(grid)
        grid.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    private func addFullWidth(_ child: NSView, to stack: NSStackView) {
        child.translatesAutoresizingMaskIntoConstraints = false
        stack.addArrangedSubview(child)
        child.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
    }

    // MARK: - 动作

    private func note(_ text: String, color: NSColor = .secondaryLabelColor) {
        footerStatus.textColor = color
        footerStatus.stringValue = text
    }

    @objc private func scanNow() {
        note("采集中…", color: .controlAccentColor)
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.bridge.scan()
            DispatchQueue.main.async {
                self?.reload()
                let stamp = DateFormatter()
                stamp.dateFormat = "HH:mm"
                self?.note("已采集 " + stamp.string(from: Date()),
                           color: .controlAccentColor)
                DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                    if self?.footerStatus.stringValue.hasPrefix("已采集") == true {
                        self?.note("")
                    }
                }
            }
        }
    }

    @objc private func openDashboard() {
        note("正在打开看板…", color: .controlAccentColor)
        bridge.openDashboard()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.note("看板 127.0.0.1:8787")
        }
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    func reload() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let snap = self.bridge.load()
            DispatchQueue.main.async { self.snapshot = snap }
        }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let bridge = PyBridge()
    private let panel = PanelController()
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private var timer: Timer?
    private var lastSnapshot: Snapshot?
    private var loggedFirstTitle = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "…"
        statusItem.button?.toolTip = "tokenlens：本地 token 用量"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePopover)

        popover = NSPopover()
        popover.appearance = NSAppearance(named: .aqua)
        popover.behavior = .transient
        popover.animates = true
        popover.contentViewController = panel
        panel.popover = popover

        logLine("启动：statusItem=\(statusItem.button == nil ? "nil" : "ok")")
        refresh()

        let timer = Timer.scheduledTimer(withTimeInterval: kRefreshInterval, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func refresh() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let snapshot = self.bridge.load()
            DispatchQueue.main.async {
                self.lastSnapshot = snapshot
                self.panel.snapshot = snapshot
                guard let snapshot, snapshot.ok,
                      let today = snapshot.windows?["today"] else {
                    self.statusItem.button?.title = "—"
                    logLine("取数失败")
                    return
                }
                let title = cn(today.tokens) + " · " + moneyCompact(today.cost, snapshot.symbol ?? "¥")
                self.statusItem.button?.title = title
                if !self.loggedFirstTitle {
                    self.loggedFirstTitle = true
                    logLine("首次刷新成功：菜单栏标题=\(title)")
                }
                self.statusItem.button?.toolTip =
                    "今日 \(title) tokens · \(money(today.cost, snapshot.symbol ?? "¥"))"
            }
        }
    }

    private func syncPopoverAppearance() {
        let match = NSApp.effectiveAppearance.bestMatch(from: [.darkAqua, .aqua])
        popover.appearance = NSAppearance(named: match == .darkAqua ? .darkAqua : .aqua)
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
        syncPopoverAppearance()
        panel.reload()
        popover.contentSize = panel.preferredContentSize
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
    }
}

@main
struct TokenLensBarMain {
    static func main() {
        let args = CommandLine.arguments
        if let index = args.firstIndex(of: "--render-panel") {
            let path = index + 1 < args.count ? args[index + 1] : "/tmp/tokenlens-panel.png"
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

    /// 离屏把面板画成 PNG，方便验证版式。
    private static func renderPanel(path: String) {
        let panel = PanelController()
        _ = panel.view
        let appearanceName = ProcessInfo.processInfo.environment["TOKENLENS_APPEARANCE"] == "light"
            ? NSAppearance.Name.aqua : NSAppearance.Name.darkAqua
        panel.view.appearance = NSAppearance(named: appearanceName)
        panel.snapshot = PyBridge().load()
        panel.view.layoutSubtreeIfNeeded()
        let size = panel.preferredContentSize.height > 100
            ? panel.preferredContentSize
            : NSSize(width: kPanelWidth, height: 700)
        panel.view.frame = NSRect(origin: .zero, size: size)
        panel.view.layoutSubtreeIfNeeded()
        let scale: CGFloat = 2
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: Int(size.width * scale),
            pixelsHigh: Int(size.height * scale),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ) else { return }
        rep.size = size
        panel.view.cacheDisplay(in: panel.view.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else { return }
        try? data.write(to: URL(fileURLWithPath: path))
        print("已输出：\(path)")
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
