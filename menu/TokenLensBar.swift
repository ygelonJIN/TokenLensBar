import Cocoa

// 菜单栏外壳 + 点开的图表面板。
// 计算全在 tokenlens.py 里；这里只负责取数和画图。
// 图表用 NSBezierPath 原生绘制，不引入 WebView 进程，所以点开是瞬时的。

let kRefreshInterval: TimeInterval = 60
let kDashboardPort = 8787
let kPanelWidth: CGFloat = 404
let kPanelHeight: CGFloat = 542

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

    func openDashboard() {
        _ = run(arguments: ["serve", "--port", "\(kDashboardPort)"], wait: false)
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
    var snapshot: Snapshot?

    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }

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
                          width: width > 0 ? width : bounds.width - point.x - 16,
                          height: size + 8)
        NSAttributedString(string: string, attributes: attrs).draw(in: rect)
    }

    private func card(_ rect: NSRect, _ title: String) {
        Palette.card.setFill()
        NSBezierPath(roundedRect: rect, xRadius: 10, yRadius: 10).fill()
        text(title, NSPoint(x: rect.minX + 14, y: rect.minY + 11), size: 11,
             weight: .medium, color: Palette.muted)
    }

    override func draw(_ dirtyRect: NSRect) {
        Palette.bg.setFill()
        bounds.fill()
        guard let snap = snapshot, snap.ok else {
            text("还没有数据", NSPoint(x: 16, y: 40), size: 14, color: Palette.muted)
            return
        }
        let symbol = snap.symbol ?? "¥"
        let today = snap.windows?["today"]

        // ── 今日 ──────────────────────────────────────────
        text("今日用量", NSPoint(x: 16, y: 12), size: 12, color: Palette.text)
        text(cn(today?.tokens ?? 0), NSPoint(x: 14, y: 28), size: 20,
             weight: .semibold, color: Palette.mint)
        text(costText(today?.cost ?? 0, tokens: today?.tokens ?? 0, symbol),
             NSPoint(x: 0, y: 28), size: 20,
             weight: .medium, color: Palette.mint, align: .right, width: bounds.width - 16)
        // 缓存命中：小一号、灰色，放在金额左侧同一行
        text(String(format: "缓存命中 %.0f%%", (snap.cacheRate ?? 0) * 100),
             NSPoint(x: 0, y: 35), size: 9.5, color: Palette.muted,
             align: .right, width: bounds.width - 128)

        // ── 近 7 天 ───────────────────────────────────────
        let weekCard = NSRect(x: 12, y: 88, width: bounds.width - 24, height: 144)
        card(weekCard, "近 7 天")
        let days = snap.daily ?? []
        if !days.isEmpty {
            let labelX = weekCard.minX + 14
            let chartX = weekCard.minX + 60
            let chartW = weekCard.maxX - 14 - chartX
            let slot = chartW / CGFloat(days.count)
            let barsTop = weekCard.minY + 30
            let barsBottom = weekCard.minY + 92
            let maxValue = max(1, days.map { $0.tokens }.max() ?? 1)

            for (index, day) in days.enumerated() {
                let centerX = chartX + CGFloat(index) * slot + slot / 2
                let height = max(day.tokens > 0 ? 2 : 1,
                                 CGFloat(day.tokens / maxValue) * (barsBottom - barsTop))
                let bar = NSRect(x: centerX - (slot - 14) / 2, y: barsBottom - height,
                                 width: slot - 14, height: height)
                (index == days.count - 1 ? Palette.mint : Palette.mint.withAlphaComponent(0.5)).setFill()
                NSBezierPath(roundedRect: bar, xRadius: 4, yRadius: 4).fill()

                let cellX = chartX + CGFloat(index) * slot
                let hasData = day.tokens > 0
                text(hasData ? cnTiny(day.tokens) : "—",
                     NSPoint(x: cellX, y: weekCard.minY + 94),
                     size: 9, color: hasData ? Palette.text : Palette.muted,
                     align: .center, width: slot)
                text(hasData ? costText(day.cost ?? 0, tokens: day.tokens, symbol) : "—",
                     NSPoint(x: cellX, y: weekCard.minY + 108),
                     size: 9, color: hasData ? Palette.mint : Palette.muted,
                     align: .center, width: slot)
                text(shortDate(day.date), NSPoint(x: cellX, y: weekCard.minY + 122),
                     size: 8.5, color: Palette.mint, align: .center, width: slot)
            }

            text("Token", NSPoint(x: labelX, y: weekCard.minY + 94), size: 8,
                 color: Palette.muted)
            text("金额", NSPoint(x: labelX, y: weekCard.minY + 108), size: 8,
                 color: Palette.muted)
            text("日期", NSPoint(x: labelX, y: weekCard.minY + 122), size: 8,
                 color: Palette.muted)
        }

        // ── 全部历史 ──────────────────────────────────────
        let historyCard = NSRect(x: 12, y: 248, width: bounds.width - 24, height: 266)
        Palette.card.setFill()
        NSBezierPath(roundedRect: historyCard, xRadius: 10, yRadius: 10).fill()
        text("全部历史", NSPoint(x: historyCard.minX + 14, y: historyCard.minY + 16),
             size: 12, weight: .semibold)

        let totalTokens = snap.allTokens ?? 0
        let totalCost = (snap.models ?? []).reduce(0.0) { $0 + $1.cost }
        text(cn(totalTokens), NSPoint(x: historyCard.maxX - 206,
                                      y: historyCard.minY + 13),
             size: 18, weight: .semibold, color: Palette.mint,
             align: .right, width: 106)
        text(money(totalCost, symbol), NSPoint(x: historyCard.maxX - 94,
                                               y: historyCard.minY + 13),
             size: 18, weight: .semibold, color: Palette.mint,
             align: .right, width: 80)

        let nameX = historyCard.minX + 14
        let tokenX = historyCard.minX + 186
        let tokenW: CGFloat = 96
        let costX = historyCard.maxX - 74
        let costW: CGFloat = 60

        func section(_ title: String, top: CGFloat) {
            text(title, NSPoint(x: nameX, y: historyCard.minY + top), size: 9.5,
                 color: Palette.muted)
        }

        func row(_ name: String, _ tokens: Double, _ cost: Double,
                 top: CGFloat, isSub: Bool = false) {
            text(name, NSPoint(x: nameX, y: historyCard.minY + top), size: 10.5, width: 158)
            text(cn(tokens), NSPoint(x: tokenX, y: historyCard.minY + top), size: 10.5,
                 align: .right, width: tokenW)
            text(costText(cost, tokens: tokens, symbol, subscription: isSub),
                 NSPoint(x: costX, y: historyCard.minY + top),
                 size: 10.5, color: Palette.text, align: .right, width: costW)
        }

        section("按模型", top: 44)
        for (index, slice) in (snap.models ?? []).prefix(4).enumerated() {
            row(slice.model ?? "?", slice.tokens, slice.cost,
                top: 60 + CGFloat(index) * 22, isSub: slice.subscription ?? false)
        }

        section("按 Agent", top: 156)
        for (index, slice) in (snap.agents ?? []).prefix(4).enumerated() {
            row(slice.agent ?? "?", slice.tokens, slice.cost,
                top: 174 + CGFloat(index) * 22, isSub: slice.subscription ?? false)
        }

        if let events = snap.allEvents {
            text("共 \(events) 次请求", NSPoint(x: 16, y: 522), size: 9.5,
                 color: Palette.muted)
        }
    }

    private func shortDate(_ text: String) -> String {
        let parts = text.split(separator: "-")
        guard parts.count == 3 else { return text }
        return "\(parts[1])-\(parts[2])"
    }
}

// MARK: - 面板

final class PanelController: NSViewController {
    private let bridge = PyBridge()
    let chart = ChartView(frame: NSRect(x: 0, y: 0, width: kPanelWidth, height: kPanelHeight))

    var snapshot: Snapshot? {
        didSet { chart.snapshot = snapshot; chart.needsDisplay = true }
    }

    override func loadView() {
        let container = NSView(frame: NSRect(x: 0, y: 0, width: kPanelWidth,
                                             height: kPanelHeight + 42))
        container.wantsLayer = true
        chart.frame = NSRect(x: 0, y: 42, width: kPanelWidth, height: kPanelHeight)
        container.addSubview(chart)

        let footer = NSView(frame: NSRect(x: 0, y: 0, width: kPanelWidth, height: 42))
        footer.wantsLayer = true
        footer.layer?.backgroundColor = Palette.bg.cgColor

        let buttons: [(String, Selector)] = [
            ("立即采集", #selector(scanNow)),
            ("打开看板", #selector(openDashboard)),
            ("退出", #selector(quit)),
        ]
        var x: CGFloat = 12
        for (title, action) in buttons {
            // 无边框按钮默认黑字，在深色底上看不见 —— 手动给浅色标题
            let button = NSButton(title: title, target: self, action: action)
            button.isBordered = false
            button.attributedTitle = NSAttributedString(string: title, attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                .foregroundColor: NSColor(srgbRed: 0.78, green: 0.83, blue: 0.88, alpha: 1),
            ])
            button.sizeToFit()
            button.frame = NSRect(x: x, y: 9, width: button.frame.width + 14, height: 22)
            footer.addSubview(button)
            x += button.frame.width + 8
        }
        container.addSubview(footer)
        self.view = container
    }

    @objc private func scanNow() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            self?.bridge.scan()
            DispatchQueue.main.async { self?.reload() }
        }
    }

    @objc private func openDashboard() { bridge.openDashboard() }
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
        let view = ChartView(frame: NSRect(x: 0, y: 0, width: kPanelWidth, height: kPanelHeight))
        view.snapshot = PyBridge().load()
        let scale: CGFloat = 2
        guard let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: Int(kPanelWidth * scale),
            pixelsHigh: Int(kPanelHeight * scale),
            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
        ) else { return }
        rep.size = NSSize(width: kPanelWidth, height: kPanelHeight)
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
