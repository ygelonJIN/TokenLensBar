import Cocoa

// 菜单栏外壳 + 点开的图表面板。
// 计算全在 tokenlens.py 里；这里只负责取数和画图。
// 图表用 NSBezierPath 原生绘制，不引入 WebView 进程，所以点开是瞬时的。

let kRefreshInterval: TimeInterval = 60
let kDashboardPort = 8787
let kPanelWidth: CGFloat = 428
let kFooterHeight: CGFloat = 44

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
    /// 近 7 天图表默认收起，点按钮才展开
    var chartExpanded = false {
        didSet {
            needsDisplay = true
            relayout()
            window?.invalidateCursorRects(for: self)
        }
    }

    /// 内容高度（不含底部按钮栏），由实际行数决定
    private(set) var contentHeight: CGFloat = 660
    var onHeightChange: ((CGFloat) -> Void)?

    private var toggleRect = NSRect.zero
    private let pad: CGFloat = 16
    private let inset: CGFloat = 14
    private let rowHeight: CGFloat = 21

    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }

    // MARK: - 交互

    /// 只有「近 7 天」这个按钮可点
    override func mouseDown(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        if toggleRect.contains(point) {
            chartExpanded.toggle()
            return
        }
        super.mouseDown(with: event)
    }

    override func resetCursorRects() {
        super.resetCursorRects()
        addCursorRect(toggleRect, cursor: .pointingHand)
    }

    // MARK: - 布局

    private func plan() -> (height: CGFloat, toggle: NSRect, today: NSRect, all: NSRect) {
        guard let snap = snapshot, snap.ok else {
            return (160, .zero, .zero, .zero)
        }
        let width = (bounds.width > 0 ? bounds.width : kPanelWidth) - pad * 2
        var y: CGFloat = 0
        y += 82

        let toggle = NSRect(x: pad, y: y, width: 156, height: 25)
        y += 25 + 10
        if chartExpanded { y += 152 }

        let modelsToday = snap.modelsToday?.count ?? 0
        let agentsToday = snap.agentsToday?.count ?? 0
        let todayHeight = 12 + 18 + CGFloat(max(modelsToday, 1)) * rowHeight
            + 8 + 18 + CGFloat(max(agentsToday, 1)) * rowHeight + 12
        let today = NSRect(x: pad, y: y, width: width, height: todayHeight)
        y += todayHeight + 10

        let modelsAll = snap.models?.count ?? 0
        let agentsAll = snap.agents?.count ?? 0
        let allHeight = 42 + 18 + CGFloat(max(modelsAll, 1)) * rowHeight
            + 8 + 18 + CGFloat(max(agentsAll, 1)) * rowHeight + 24
        let all = NSRect(x: pad, y: y, width: width, height: allHeight)
        y += allHeight + 12

        return (y, toggle, today, all)
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

    private func card(_ rect: NSRect) {
        Palette.card.setFill()
        NSBezierPath(roundedRect: rect, xRadius: 12, yRadius: 12).fill()
    }

    private func sectionLabel(_ string: String, _ point: NSPoint) {
        text(string, point, size: 9.5, color: Palette.muted)
    }

    private func row(_ box: NSRect, top: CGFloat, name: String, tokens: Double, cost: Double,
                     color: NSColor, symbol: String, subscription: Bool = false) {
        color.withAlphaComponent(0.9).setFill()
        NSBezierPath(roundedRect: NSRect(x: box.minX + inset, y: top + 4,
                                         width: 3, height: 11),
                     xRadius: 1.5, yRadius: 1.5).fill()
        text(name, NSPoint(x: box.minX + inset + 10, y: top), size: 11.5,
             width: box.width - 190)
        text(cn(tokens), NSPoint(x: box.maxX - inset - 152, y: top + 1), size: 10.5,
             color: Palette.muted, align: .right, width: 86)
        text(costText(cost, tokens: tokens, symbol, subscription: subscription),
             NSPoint(x: box.maxX - inset - 66, y: top), size: 11.5,
             color: Palette.text, align: .right, width: 66)
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
        let today = snap.windows?["today"]
        let layout = plan()
        toggleRect = layout.toggle

        drawHeadline(snap, today: today, symbol: symbol)
        drawToggle(layout.toggle, week: snap.windows?["week"], symbol: symbol)
        if chartExpanded {
            drawWeekChart(snap, top: layout.toggle.maxY + 10, symbol: symbol)
        }
        drawTodayCard(layout.today, snap: snap, symbol: symbol)
        drawAllCard(layout.all, snap: snap, symbol: symbol)
    }

    private func drawHeadline(_ snap: Snapshot, today: Snapshot.Window?, symbol: String) {
        text("今日用量", NSPoint(x: pad, y: 10), size: 11, color: Palette.muted)
        text(cn(today?.tokens ?? 0), NSPoint(x: pad - 1, y: 25), size: 25,
             weight: .semibold, color: Palette.mint)
        text(money(today?.cost ?? 0, symbol), NSPoint(x: 0, y: 28), size: 21,
             weight: .semibold, color: Palette.mint, align: .right,
             width: bounds.width - pad)
        let rate = Int(((snap.cacheRate ?? 0) * 100).rounded())
        text("缓存命中 \(rate)% · \(today?.events ?? 0) 次请求",
             NSPoint(x: pad, y: 57), size: 10, color: Palette.muted)
    }

    private func drawToggle(_ rect: NSRect, week: Snapshot.Window?, symbol: String) {
        let path = NSBezierPath(roundedRect: rect, xRadius: 8, yRadius: 8)
        Palette.card.setFill()
        path.fill()
        Palette.line.setStroke()
        path.lineWidth = 1
        path.stroke()
        text("近 7 天", NSPoint(x: rect.minX + 13, y: rect.minY + 5), size: 11,
             weight: .medium, color: Palette.text)
        text(money(week?.cost ?? 0, symbol), NSPoint(x: rect.maxX - 70, y: rect.minY + 6),
             size: 10.5, color: Palette.mint, align: .right, width: 46)
        text(chartExpanded ? "▴" : "▾", NSPoint(x: rect.maxX - 22, y: rect.minY + 4),
             size: 10, color: Palette.muted, align: .center, width: 12)
    }

    private func drawWeekChart(_ snap: Snapshot, top: CGFloat, symbol: String) {
        let box = NSRect(x: pad, y: top, width: bounds.width - pad * 2, height: 142)
        card(box)
        let days = snap.daily ?? []
        guard !days.isEmpty else { return }

        let labelX = box.minX + inset
        let chartX = box.minX + 52
        let chartW = box.maxX - inset - chartX
        let slot = chartW / CGFloat(days.count)
        let barsTop = box.minY + 14
        let barsBottom = box.minY + 82
        let maxValue = max(1, days.map { $0.tokens }.max() ?? 1)

        for (index, day) in days.enumerated() {
            let centerX = chartX + CGFloat(index) * slot + slot / 2
            let height = max(day.tokens > 0 ? 2 : 1,
                             CGFloat(day.tokens / maxValue) * (barsBottom - barsTop))
            let bar = NSRect(x: centerX - (slot - 14) / 2, y: barsBottom - height,
                             width: max(2, slot - 14), height: height)
            (index == days.count - 1 ? Palette.mint : Palette.mint.withAlphaComponent(0.45)).setFill()
            NSBezierPath(roundedRect: bar, xRadius: 3, yRadius: 3).fill()

            let cellX = chartX + CGFloat(index) * slot
            let hasData = day.tokens > 0
            text(hasData ? cnTiny(day.tokens) : "—",
                 NSPoint(x: cellX, y: box.minY + 84), size: 8.5,
                 color: hasData ? Palette.text : Palette.muted, align: .center, width: slot)
            text(hasData ? costText(day.cost ?? 0, tokens: day.tokens, symbol) : "—",
                 NSPoint(x: cellX, y: box.minY + 99), size: 8.5,
                 color: hasData ? Palette.mint : Palette.muted, align: .center, width: slot)
            text(shortDate(day.date), NSPoint(x: cellX, y: box.minY + 116), size: 8,
                 color: Palette.muted, align: .center, width: slot)
        }

        text("Token", NSPoint(x: labelX, y: box.minY + 84), size: 8, color: Palette.muted)
        text("金额", NSPoint(x: labelX, y: box.minY + 99), size: 8, color: Palette.muted)
        text("日期", NSPoint(x: labelX, y: box.minY + 116), size: 8, color: Palette.muted)
    }

    private func drawTodayCard(_ box: NSRect, snap: Snapshot, symbol: String) {
        card(box)
        var y = box.minY + 12

        sectionLabel("按模型", NSPoint(x: box.minX + inset, y: y))
        y += 18
        let models = snap.modelsToday ?? []
        if models.isEmpty {
            text("今天还没有用量", NSPoint(x: box.minX + inset, y: y + 2),
                 size: 11, color: Palette.muted)
        }
        for (index, slice) in models.enumerated() {
            row(box, top: y + CGFloat(index) * rowHeight, name: slice.model ?? "?",
                tokens: slice.tokens, cost: slice.cost,
                color: Palette.series[index % Palette.series.count], symbol: symbol,
                subscription: slice.subscription ?? false)
        }
        y += CGFloat(max(models.count, 1)) * rowHeight + 8

        sectionLabel("按 Agent", NSPoint(x: box.minX + inset, y: y))
        y += 18
        let agents = snap.agentsToday ?? []
        for (index, slice) in agents.enumerated() {
            row(box, top: y + CGFloat(index) * rowHeight, name: slice.agent ?? "?",
                tokens: slice.tokens, cost: slice.cost,
                color: Palette.series[(index + 2) % Palette.series.count], symbol: symbol,
                subscription: slice.subscription ?? false)
        }
    }

    private func drawAllCard(_ box: NSRect, snap: Snapshot, symbol: String) {
        card(box)
        let cost = (snap.models ?? []).reduce(0.0) { $0 + $1.cost }

        text("全部历史", NSPoint(x: box.minX + inset, y: box.minY + 14), size: 11.5,
             weight: .semibold)
        text(money(cost, symbol), NSPoint(x: box.maxX - inset - 70, y: box.minY + 13),
             size: 13, weight: .semibold, color: Palette.mint, align: .right, width: 70)
        text(cn(snap.allTokens ?? 0),
             NSPoint(x: box.maxX - inset - 70 - 96, y: box.minY + 13),
             size: 13, weight: .semibold, color: Palette.mint, align: .right, width: 92)

        var y = box.minY + 42
        sectionLabel("按模型", NSPoint(x: box.minX + inset, y: y))
        y += 18
        let models = snap.models ?? []
        for (index, slice) in models.enumerated() {
            row(box, top: y + CGFloat(index) * rowHeight, name: slice.model ?? "?",
                tokens: slice.tokens, cost: slice.cost,
                color: Palette.series[index % Palette.series.count], symbol: symbol,
                subscription: slice.subscription ?? false)
        }
        y += CGFloat(max(models.count, 1)) * rowHeight + 8

        sectionLabel("按 Agent", NSPoint(x: box.minX + inset, y: y))
        y += 18
        let agents = snap.agents ?? []
        for (index, slice) in agents.enumerated() {
            row(box, top: y + CGFloat(index) * rowHeight, name: slice.agent ?? "?",
                tokens: slice.tokens, cost: slice.cost,
                color: Palette.series[(index + 1) % Palette.series.count], symbol: symbol,
                subscription: slice.subscription ?? false)
        }

        text("共 \(snap.allEvents ?? 0) 次请求",
             NSPoint(x: box.minX + inset, y: box.maxY - 22), size: 9.5, color: Palette.muted)
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
            button.frame = NSRect(x: x, y: 10, width: button.frame.width + 18, height: 23)
            footer.addSubview(button)
            x += button.frame.width + 8
        }
        footerStatus.frame = NSRect(x: x + 4, y: 14,
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
                let title = cn(today.tokens)
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
        // 截图核对版式时用：TOKENLENS_PANEL_CHART=1 渲染展开的近 7 天图表
        if ProcessInfo.processInfo.environment["TOKENLENS_PANEL_CHART"] == "1" {
            panel.chart.chartExpanded = true
        }
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
