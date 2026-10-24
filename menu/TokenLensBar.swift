import Cocoa

// 菜单栏外壳 + 点开的图表面板。
// 计算全在 tokenlens.py 里；这里只负责取数和画图。
// 图表用 NSBezierPath 原生绘制，不引入 WebView 进程，所以点开是瞬时的。

let kRefreshInterval: TimeInterval = 60
let kDashboardPort = 8787
let kPanelWidth: CGFloat = 372
let kFooterHeight: CGFloat = 42

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
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case ok, symbol, windows, daily, models, agents, unpriced, priced, reason
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

    func scan() { _ = run(arguments: ["scan"]) }

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

// MARK: - 配色

enum Palette {
    static let bg = NSColor(srgbRed: 0.055, green: 0.075, blue: 0.105, alpha: 1)
    static let card = NSColor(srgbRed: 0.098, green: 0.118, blue: 0.153, alpha: 1)
    static let line = NSColor(srgbRed: 0.145, green: 0.169, blue: 0.212, alpha: 1)
    static let text = NSColor(srgbRed: 0.902, green: 0.929, blue: 0.953, alpha: 1)
    static let muted = NSColor(srgbRed: 0.545, green: 0.580, blue: 0.620, alpha: 1)
    static let mint = NSColor(srgbRed: 0.220, green: 0.827, blue: 0.624, alpha: 1)
    static let periwinkle = NSColor(srgbRed: 0.486, green: 0.549, blue: 1.0, alpha: 1)
    static let amber = NSColor(srgbRed: 0.941, green: 0.706, blue: 0.161, alpha: 1)
    static let rose = NSColor(srgbRed: 0.941, green: 0.451, blue: 0.545, alpha: 1)
    static let sky = NSColor(srgbRed: 0.369, green: 0.749, blue: 0.937, alpha: 1)

    static let series: [NSColor] = [mint, periwinkle, amber, rose, sky]
}

// MARK: - 图表

final class ChartView: NSView {
    var snapshot: Snapshot? {
        didSet { needsDisplay = true; relayout() }
    }

    /// 内容高度（不含底部按钮栏），由实际行数决定
    private(set) var contentHeight: CGFloat = 640
    var onHeightChange: ((CGFloat) -> Void)?

    private let pad: CGFloat = 14
    private let inset: CGFloat = 13
    private let rowHeight: CGFloat = 21
    private let chartHeight: CGFloat = 104

    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }

    // MARK: - 布局

    private func plan() -> (height: CGFloat, today: NSRect, chart: NSRect, all: NSRect) {
        guard let snap = snapshot, snap.ok else {
            return (160, .zero, .zero, .zero)
        }
        let width = (bounds.width > 0 ? bounds.width : kPanelWidth) - pad * 2
        var y: CGFloat = 0
        y += 68

        let modelsToday = snap.modelsToday?.count ?? 0
        let agentsToday = snap.agentsToday?.count ?? 0
        let todayHeight = 12 + 17 + CGFloat(max(modelsToday, 1)) * rowHeight
            + 6 + 17 + CGFloat(max(agentsToday, 1)) * rowHeight + 11
        let today = NSRect(x: pad, y: y, width: width, height: todayHeight)
        y += todayHeight + 10

        let chart = NSRect(x: pad, y: y, width: width, height: chartHeight)
        y += chartHeight + 10

        let modelsAll = snap.models?.count ?? 0
        let agentsAll = snap.agents?.count ?? 0
        let allHeight = 38 + 17 + CGFloat(max(modelsAll, 1)) * rowHeight
            + 6 + 17 + CGFloat(max(agentsAll, 1)) * rowHeight + 21
        let all = NSRect(x: pad, y: y, width: width, height: allHeight)
        y += allHeight + 6

        return (y, today, chart, all)
    }

    private func relayout() {
        let height = plan().height
        guard abs(height - contentHeight) > 0.5 else { return }
        contentHeight = height
        onHeightChange?(height)
    }

    // MARK: - 绘制基础

    private func text(_ string: String, _ point: NSPoint, size: CGFloat,
                      weight: NSFont.Weight = .regular, color: NSColor = Palette.text,
                      align: NSTextAlignment = .left, width: CGFloat = 0) {
        let style = NSMutableParagraphStyle()
        style.alignment = align
        style.lineBreakMode = .byTruncatingTail
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
            .foregroundColor: color,
            .paragraphStyle: style,
        ]
        let rect = NSRect(x: point.x, y: point.y,
                          width: width > 0 ? width : bounds.width - point.x - pad,
                          height: size + 8)
        NSAttributedString(string: string, attributes: attrs).draw(in: rect)
    }

    private func width(_ string: String, size: CGFloat, weight: NSFont.Weight) -> CGFloat {
        NSAttributedString(string: string, attributes: [
            .font: NSFont.systemFont(ofSize: size, weight: weight),
        ]).size().width
    }

    private func card(_ rect: NSRect) {
        Palette.card.setFill()
        NSBezierPath(roundedRect: rect, xRadius: 11, yRadius: 11).fill()
    }

    private func sectionLabel(_ string: String, _ point: NSPoint) {
        text(string, point, size: 9.5, color: Palette.muted)
    }

    private func row(_ box: NSRect, top: CGFloat, name: String, tokens: Double, cost: Double,
                     symbol: String, subscription: Bool = false) {
        text(name, NSPoint(x: box.minX + inset, y: top), size: 11,
             width: box.width - 168)
        text(cn(tokens), NSPoint(x: box.maxX - inset - 148, y: top + 1), size: 10,
             color: Palette.muted, align: .right, width: 84)
        text(costText(cost, tokens: tokens, symbol, subscription: subscription),
             NSPoint(x: box.maxX - inset - 62, y: top), size: 11,
             color: Palette.text, align: .right, width: 62)
    }

    // MARK: - 绘制

    override func draw(_ dirtyRect: NSRect) {
        Palette.bg.setFill()
        bounds.fill()
        guard let snap = snapshot, snap.ok else {
            text("还没有数据", NSPoint(x: pad, y: 40), size: 14, color: Palette.muted)
            return
        }
        let symbol = snap.symbol ?? "¥"
        let layout = plan()

        drawHeadline(snap, today: snap.windows?["today"], symbol: symbol)
        drawTodayCard(layout.today, snap: snap, symbol: symbol)
        drawWeekChart(snap, box: layout.chart, symbol: symbol)
        drawAllCard(layout.all, snap: snap, symbol: symbol)
    }

    private func drawHeadline(_ snap: Snapshot, today: Snapshot.Window?, symbol: String) {
        text("今日用量", NSPoint(x: pad, y: 7), size: 10, color: Palette.muted)
        let tokenText = cn(today?.tokens ?? 0)
        text(tokenText, NSPoint(x: pad - 1, y: 20), size: 23,
             weight: .semibold, color: Palette.mint)
        // 金额紧贴 token 右侧
        text(money(today?.cost ?? 0, symbol),
             NSPoint(x: pad - 1 + width(tokenText, size: 23, weight: .semibold) + 9, y: 24),
             size: 19, weight: .semibold, color: Palette.mint)
        let rate = Int(((snap.cacheRate ?? 0) * 100).rounded())
        text("缓存命中 \(rate)% · \(today?.events ?? 0) 次请求",
             NSPoint(x: pad, y: 48), size: 9.5, color: Palette.muted)
    }

    private func drawTodayCard(_ box: NSRect, snap: Snapshot, symbol: String) {
        card(box)
        var y = box.minY + 11

        sectionLabel("按模型", NSPoint(x: box.minX + inset, y: y))
        y += 17
        let models = snap.modelsToday ?? []
        if models.isEmpty {
            text("今天还没有用量", NSPoint(x: box.minX + inset, y: y + 1),
                 size: 10.5, color: Palette.muted)
        }
        for (index, slice) in models.enumerated() {
            row(box, top: y + CGFloat(index) * rowHeight, name: slice.model ?? "?",
                tokens: slice.tokens, cost: slice.cost, symbol: symbol,
                subscription: slice.subscription ?? false)
        }
        y += CGFloat(max(models.count, 1)) * rowHeight + 6

        sectionLabel("按 Agent", NSPoint(x: box.minX + inset, y: y))
        y += 17
        let agents = snap.agentsToday ?? []
        for (index, slice) in agents.enumerated() {
            row(box, top: y + CGFloat(index) * rowHeight, name: slice.agent ?? "?",
                tokens: slice.tokens, cost: slice.cost, symbol: symbol,
                subscription: slice.subscription ?? false)
        }
    }

    /// 近 7 天：常驻显示，做得紧凑一点
    private func drawWeekChart(_ snap: Snapshot, box: NSRect, symbol: String) {
        card(box)
        let days = snap.daily ?? []
        guard !days.isEmpty else { return }

        let weekTitle = "近 7 天"
        text(weekTitle, NSPoint(x: box.minX + inset, y: box.minY + 7), size: 9.5,
             color: Palette.muted)
        let week = snap.windows?["week"]
        text(money(week?.cost ?? 0, symbol),
             NSPoint(x: box.minX + inset + width(weekTitle, size: 9.5, weight: .regular) + 8,
                     y: box.minY + 7),
             size: 9.5, color: Palette.mint)

        let labelX = box.minX + inset
        let chartX = box.minX + 40
        let chartW = box.maxX - inset - chartX
        let slot = chartW / CGFloat(days.count)
        let barsTop = box.minY + 22
        let barsBottom = box.minY + 60
        let maxValue = max(1, days.map { $0.tokens }.max() ?? 1)

        for (index, day) in days.enumerated() {
            let centerX = chartX + CGFloat(index) * slot + slot / 2
            let height = max(day.tokens > 0 ? 2 : 1,
                             CGFloat(day.tokens / maxValue) * (barsBottom - barsTop))
            let bar = NSRect(x: centerX - (slot - 9) / 2, y: barsBottom - height,
                             width: max(2, slot - 9), height: height)
            (index == days.count - 1 ? Palette.mint : Palette.mint.withAlphaComponent(0.45)).setFill()
            NSBezierPath(roundedRect: bar, xRadius: 2, yRadius: 2).fill()

            let cellX = chartX + CGFloat(index) * slot
            let hasData = day.tokens > 0
            text(hasData ? cnTiny(day.tokens) : "—",
                 NSPoint(x: cellX, y: box.minY + 62), size: 8,
                 color: hasData ? Palette.text : Palette.muted, align: .center, width: slot)
            text(hasData ? costText(day.cost ?? 0, tokens: day.tokens, symbol) : "—",
                 NSPoint(x: cellX, y: box.minY + 74), size: 8,
                 color: hasData ? Palette.mint : Palette.muted, align: .center, width: slot)
            text(shortDate(day.date), NSPoint(x: cellX, y: box.minY + 86), size: 7.5,
                 color: Palette.muted, align: .center, width: slot)
        }

        text("Token", NSPoint(x: labelX, y: box.minY + 62), size: 7.5, color: Palette.muted)
        text("金额", NSPoint(x: labelX, y: box.minY + 74), size: 7.5, color: Palette.muted)
        text("日期", NSPoint(x: labelX, y: box.minY + 86), size: 7.5, color: Palette.muted)
    }

    private func drawAllCard(_ box: NSRect, snap: Snapshot, symbol: String) {
        card(box)
        let cost = (snap.models ?? []).reduce(0.0) { $0 + $1.cost }

        let allTitle = "全部历史"
        text(allTitle, NSPoint(x: box.minX + inset, y: box.minY + 12), size: 11,
             weight: .semibold)
        let titleWidth = width(allTitle, size: 11, weight: .semibold)
        let totalText = cn(snap.allTokens ?? 0)
        let totalX = box.minX + inset + titleWidth + 12
        text(totalText, NSPoint(x: totalX, y: box.minY + 12), size: 11.5,
             weight: .semibold, color: Palette.mint)
        text(money(cost, symbol),
             NSPoint(x: totalX + width(totalText, size: 11.5, weight: .semibold) + 8,
                     y: box.minY + 12),
             size: 11.5, weight: .semibold, color: Palette.mint)

        var y = box.minY + 38
        sectionLabel("按模型", NSPoint(x: box.minX + inset, y: y))
        y += 17
        let models = snap.models ?? []
        for (index, slice) in models.enumerated() {
            row(box, top: y + CGFloat(index) * rowHeight, name: slice.model ?? "?",
                tokens: slice.tokens, cost: slice.cost, symbol: symbol,
                subscription: slice.subscription ?? false)
        }
        y += CGFloat(max(models.count, 1)) * rowHeight + 6

        sectionLabel("按 Agent", NSPoint(x: box.minX + inset, y: y))
        y += 17
        let agents = snap.agents ?? []
        for (index, slice) in agents.enumerated() {
            row(box, top: y + CGFloat(index) * rowHeight, name: slice.agent ?? "?",
                tokens: slice.tokens, cost: slice.cost, symbol: symbol,
                subscription: slice.subscription ?? false)
        }

        text("共 \(snap.allEvents ?? 0) 次请求",
             NSPoint(x: box.minX + inset, y: box.maxY - 20), size: 9, color: Palette.muted)
    }

    private func shortDate(_ text: String) -> String {
        let parts = text.split(separator: "-")
        guard parts.count == 3 else { return text }
        return "\(parts[1])-\(parts[2])"
    }
}

// MARK: - 面板

/// 深色底上的胶囊按钮：有底色、有描边，hover 和按下都有反馈。
final class PanelButton: NSButton {
    private var tracking: NSTrackingArea?

    static let hoverColor = NSColor(srgbRed: 0.129, green: 0.157, blue: 0.204, alpha: 1)
    static let pressColor = NSColor(srgbRed: 0.180, green: 0.216, blue: 0.274, alpha: 1)

    static func make(_ title: String, target: AnyObject, action: Selector) -> PanelButton {
        let button = PanelButton(title: title, target: target, action: action)
        button.isBordered = false
        button.setButtonType(.momentaryChange)
        button.wantsLayer = true
        button.layer?.cornerRadius = 7
        button.layer?.borderWidth = 1
        button.layer?.borderColor = Palette.line.cgColor
        button.layer?.backgroundColor = Palette.card.cgColor
        button.attributedTitle = NSAttributedString(string: title, attributes: [
            .font: NSFont.systemFont(ofSize: 11, weight: .medium),
            .foregroundColor: Palette.text,
        ])
        return button
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: bounds,
                                  options: [.mouseEnteredAndExited, .activeInActiveApp],
                                  owner: self, userInfo: nil)
        addTrackingArea(area)
        tracking = area
    }

    override func mouseEntered(with event: NSEvent) {
        layer?.backgroundColor = PanelButton.hoverColor.cgColor
    }

    override func mouseExited(with event: NSEvent) {
        layer?.backgroundColor = Palette.card.cgColor
    }

    override func highlight(_ flag: Bool) {
        super.highlight(flag)
        layer?.backgroundColor = (flag ? PanelButton.pressColor : Palette.card).cgColor
    }
}

final class PanelController: NSViewController {
    private let bridge = PyBridge()
    let chart = ChartView(frame: NSRect(x: 0, y: 0, width: kPanelWidth, height: 660))
    private let footer = NSView()
    weak var popover: NSPopover?

    private let footerStatus: NSTextField = {
        let field = NSTextField(labelWithString: "")
        field.font = NSFont.systemFont(ofSize: 10.5)
        field.textColor = Palette.muted
        field.alignment = .right
        field.isBezeled = false
        field.drawsBackground = false
        field.isEditable = false
        field.isSelectable = false
        return field
    }()

    var snapshot: Snapshot? {
        didSet { chart.snapshot = snapshot }
    }

    override func loadView() {
        let container = NSView(frame: NSRect(x: 0, y: 0, width: kPanelWidth,
                                             height: 660 + kFooterHeight))
        container.wantsLayer = true

        footer.frame = NSRect(x: 0, y: 0, width: kPanelWidth, height: kFooterHeight)
        footer.wantsLayer = true
        footer.layer?.backgroundColor = Palette.bg.cgColor

        let buttons: [(String, Selector)] = [
            ("立即采集", #selector(scanNow)),
            ("打开看板", #selector(openDashboard)),
            ("退出", #selector(quit)),
        ]
        var x: CGFloat = 12
        for (title, action) in buttons {
            let button = PanelButton.make(title, target: self, action: action)
            button.sizeToFit()
            button.frame = NSRect(x: x, y: 9, width: button.frame.width + 16, height: 22)
            footer.addSubview(button)
            x += button.frame.width + 8
        }
        footerStatus.frame = NSRect(x: x + 4, y: 13,
                                    width: kPanelWidth - x - 20, height: 16)
        footer.addSubview(footerStatus)

        container.addSubview(footer)
        container.addSubview(chart)
        self.view = container

        chart.onHeightChange = { [weak self] height in self?.applyHeight(height) }
        applyHeight(chart.contentHeight)
    }

    /// 行数变了就重排：面板高度跟着内容走
    private func applyHeight(_ height: CGFloat) {
        let total = height + kFooterHeight
        view.frame = NSRect(x: 0, y: 0, width: kPanelWidth, height: total)
        chart.frame = NSRect(x: 0, y: kFooterHeight, width: kPanelWidth, height: height)
        footer.frame = NSRect(x: 0, y: 0, width: kPanelWidth, height: kFooterHeight)
        preferredContentSize = NSSize(width: kPanelWidth, height: total)
        if let popover, popover.isShown {
            popover.contentSize = NSSize(width: kPanelWidth, height: total)
        }
    }

    private func note(_ text: String, color: NSColor = Palette.muted) {
        footerStatus.textColor = color
        footerStatus.stringValue = text
    }

    @objc private func scanNow() {
        note("采集中…", color: Palette.mint)
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.bridge.scan()
            DispatchQueue.main.async {
                self?.reload()
                let stamp = DateFormatter()
                stamp.dateFormat = "HH:mm"
                self?.note("已采集 " + stamp.string(from: Date()), color: Palette.mint)
                DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
                    if self?.footerStatus.stringValue.hasPrefix("已采集") == true {
                        self?.note("")
                    }
                }
            }
        }
    }

    @objc private func openDashboard() {
        note("正在打开看板…", color: Palette.mint)
        bridge.openDashboard()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            self?.note("看板 127.0.0.1:8787")
        }
    }

    @objc private func quit() { NSApp.terminate(nil) }

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
        popover.behavior = .transient
        popover.animates = false
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

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
            return
        }
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
        if let index = args.firstIndex(of: "--render") {
            let path = index + 1 < args.count ? args[index + 1] : "/tmp/tokenlens-panel.png"
            render(path: path)
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

    /// 离屏把面板画成 PNG，方便验证版式（不需要屏幕录制权限）
    private static func render(path: String) {
        let view = ChartView(frame: NSRect(x: 0, y: 0, width: kPanelWidth, height: 660))
        view.snapshot = PyBridge().load()
        let height = view.contentHeight
        view.frame = NSRect(x: 0, y: 0, width: kPanelWidth, height: height)
        let scale: CGFloat = 2
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: Int(kPanelWidth * scale),
            pixelsHigh: Int(height * scale),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ) else { return }
        rep.size = NSSize(width: kPanelWidth, height: height)
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else { return }
        try? data.write(to: URL(fileURLWithPath: path))
        print("已输出：\(path)")
    }

    /// 连底部按钮一起渲染，验证深色底上的可读性
    private static func renderPanel(path: String) {
        let panel = PanelController()
        _ = panel.view
        panel.snapshot = PyBridge().load()
        let size = panel.view.bounds.size
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
    }
}
