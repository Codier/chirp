import AppKit
import ChirpCore

/// Everything the first-run window shows. The controller reads it from the system; the snapshots pass fixed values.
public struct OnboardingState: Sendable {
    public var flow = OnboardingFlow()
    public var checks = OnboardingChecks()
    public var hotkey: KeyCombo = .controlOptionSpace
    public var activationMode: ActivationMode = .autoStop
    public var localModelsAvailable = false

    public init() {}
    public init(step: OnboardingStep, checks: OnboardingChecks = OnboardingChecks(), heardHotkey: Bool = false,
                hotkey: KeyCombo = .controlOptionSpace) {
        var flow = OnboardingFlow()
        while flow.step != step { flow.advance(markDone: {}) }
        flow.heardHotkey = heardHotkey
        self.flow = flow
        self.checks = checks
        self.hotkey = hotkey
    }
}

/// What a step's status row says: the words, the symbol tone and the label of its Fix button (nil when there is
/// nothing to fix). Pure, so the wording is tested without a window.
public struct OnboardingStatus: Equatable, Sendable {
    public var text: String
    public var tone: MenuEntry.Tone
    public var fixTitle: String?

    public static func of(_ step: OnboardingStep, _ checks: OnboardingChecks) -> OnboardingStatus? {
        switch step {
        case .welcome, .tryIt: return nil
        case .microphone:
            switch checks.microphone {
            case .granted: return .init(text: "Allowed", tone: .ok)
            case .notAsked: return .init(text: "Not asked yet", tone: .warning, fixTitle: "Allow…")
            case .denied: return .init(text: "Blocked in System Settings", tone: .error, fixTitle: "Open Settings…")
            }
        case .accessibility:
            return checks.accessibility ? .init(text: "Allowed", tone: .ok)
                                        : .init(text: "Not allowed yet", tone: .warning, fixTitle: "Allow…")
        case .server:
            switch checks.server {
            case .ready(let engine): return .init(text: engine.map { "Connected — \($0)" } ?? "Connected", tone: .ok)
            case .loadingModel: return .init(text: "Loading the speech model…", tone: .warning)
            case .checking: return .init(text: "Checking…", tone: .warning)
            case .unreachable(let host): return .init(text: "Can’t reach \(host)", tone: .error, fixTitle: "Copy Fix Command")
            case .noToken: return .init(text: "No token yet — start chirp-server once", tone: .warning, fixTitle: "Copy Fix Command")
            }
        }
    }

    init(text: String, tone: MenuEntry.Tone, fixTitle: String? = nil) {
        self.text = text; self.tone = tone; self.fixTitle = fixTitle
    }
}

/// The first-run window's content: a header, one step's explanation and live status, and Back / Continue. Plain AppKit
/// like Settings. Controls are exposed so the owner can attach actions.
@MainActor
public final class OnboardingView: NSView {
    public static let size = NSSize(width: 520, height: 400)

    public let primary = NSButton(title: "", target: nil, action: nil)
    public let back = NSButton(title: "Back", target: nil, action: nil)
    public let fix = NSButton(title: "Fix…", target: nil, action: nil)
    /// Where the Try it step's dictation lands.
    public let tryField = NSTextField(string: "")

    private let icon: NSImage?
    private let content = NSStackView()
    private let dots = NSStackView()
    private var shownStep: OnboardingStep?

    public init(icon: NSImage?) {
        self.icon = icon
        super.init(frame: NSRect(origin: .zero, size: Self.size))
        primary.bezelStyle = .rounded
        primary.keyEquivalent = "\r"
        back.bezelStyle = .rounded
        fix.bezelStyle = .rounded
        fix.controlSize = .small
        fix.setAccessibilityLabel("Fix this step")
        tryField.placeholderString = "Your dictation appears here"
        tryField.font = .systemFont(ofSize: 15)
        tryField.setAccessibilityLabel("Try dictation here")

        content.orientation = .vertical
        content.alignment = .centerX
        content.spacing = 10
        content.translatesAutoresizingMaskIntoConstraints = false

        let divider = NSBox()
        divider.boxType = .separator
        divider.translatesAutoresizingMaskIntoConstraints = false

        dots.orientation = .horizontal
        dots.spacing = 6
        let buttons = NSStackView(views: [back, primary])
        buttons.orientation = .horizontal
        buttons.spacing = 8
        let footer = NSStackView(views: [dots, NSView(), buttons])
        footer.orientation = .horizontal
        footer.alignment = .centerY
        footer.translatesAutoresizingMaskIntoConstraints = false

        addSubview(content)
        addSubview(divider)
        addSubview(footer)
        NSLayoutConstraint.activate([
            widthAnchor.constraint(equalToConstant: Self.size.width),
            heightAnchor.constraint(equalToConstant: Self.size.height),
            content.topAnchor.constraint(equalTo: topAnchor, constant: 34),
            content.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 44),
            trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: 44),
            divider.leadingAnchor.constraint(equalTo: leadingAnchor),
            divider.trailingAnchor.constraint(equalTo: trailingAnchor),
            divider.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -14),
            footer.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 24),
            trailingAnchor.constraint(equalTo: footer.trailingAnchor, constant: 24),
            bottomAnchor.constraint(equalTo: footer.bottomAnchor, constant: 18),
        ])
    }

    @available(*, unavailable) required init?(coder: NSCoder) { fatalError("not used") }

    public func apply(_ state: OnboardingState) {
        let step = state.flow.step
        var status = OnboardingStatus.of(step, state.checks)
        if step == .server, state.localModelsAvailable { status?.fixTitle = "Set Up Models…" }
        primary.title = state.flow.primaryTitle(state.checks)
        back.isHidden = state.flow.isFirst
        rebuildDots(current: step.rawValue)

        for view in content.arrangedSubviews { content.removeArrangedSubview(view); view.removeFromSuperview() }
        let (symbol, body) = Self.copy(step, hotkey: state.hotkey, mode: state.activationMode)
        content.addArrangedSubview(header(step: step, symbol: symbol))
        let title = NSTextField(labelWithString: step.title)
        title.font = .systemFont(ofSize: 22, weight: .semibold)
        content.addArrangedSubview(title)
        let text = NSTextField(wrappingLabelWithString: body)
        text.font = MenuStyle.font
        text.textColor = .secondaryLabelColor
        text.alignment = .center
        text.preferredMaxLayoutWidth = Self.size.width - 120
        content.addArrangedSubview(text)
        content.setCustomSpacing(18, after: text)

        if let status {
            content.addArrangedSubview(statusBox(status))
        } else if step == .tryIt {
            for view in tryItViews(state) { content.addArrangedSubview(view) }
        } else {
            content.addArrangedSubview(keycap(state.hotkey.title, caption: "Your hotkey"))
        }
        shownStep = step
    }

    // MARK: pieces

    private static func copy(_ step: OnboardingStep, hotkey: KeyCombo, mode: ActivationMode) -> (symbol: String, body: String) {
        switch step {
        case .welcome:
            ("", "Press a hotkey in any app, talk, and your words appear where you’re typing. Setup takes a minute: Chirp needs the microphone, Accessibility and a speech server.")
        case .microphone:
            ("mic.fill", "Chirp listens only while you dictate. Audio goes to your own server and is never stored.")
        case .accessibility:
            ("text.cursor", "Lets Chirp type into the app you’re using. Without it Chirp copies the text and you press ⌘V.")
        case .server:
            ("server.rack", "Chirp sends audio to chirp-server, which runs on this Mac unless you point Chirp elsewhere in Settings.")
        case .tryIt:
            switch mode {
            case .autoStop: ("waveform", "Click the box, press \(hotkey.title) and say something. Chirp stops when you pause.")
            case .holdToTalk: ("waveform", "Click the box, hold \(hotkey.title) and say something, then let go.")
            case .toggle: ("waveform", "Click the box, press \(hotkey.title) and say something. Press it again to stop recording.")
            }
        }
    }

    private func header(step: OnboardingStep, symbol: String) -> NSView {
        let size: CGFloat = 72
        let view: NSView
        if step == .welcome {
            let image = NSImageView(image: icon ?? NSImage())
            image.imageScaling = .scaleProportionallyUpOrDown
            image.setAccessibilityLabel("Chirp app icon")
            view = image
        } else {
            let disc = ColorView(NSColor.controlAccentColor.withAlphaComponent(0.16), cornerRadius: size / 2)
            let glyph = NSImageView(image: NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
                .withSymbolConfiguration(.init(pointSize: 30, weight: .regular).applying(.init(paletteColors: [.controlAccentColor]))) ?? NSImage())
            glyph.translatesAutoresizingMaskIntoConstraints = false
            disc.addSubview(glyph)
            NSLayoutConstraint.activate([
                glyph.centerXAnchor.constraint(equalTo: disc.centerXAnchor), glyph.centerYAnchor.constraint(equalTo: disc.centerYAnchor),
            ])
            view = disc
        }
        view.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            view.widthAnchor.constraint(equalToConstant: size), view.heightAnchor.constraint(equalToConstant: size),
        ])
        return view
    }

    private func statusBox(_ status: OnboardingStatus) -> NSView {
        let symbolName = switch status.tone {
        case .ok: "checkmark.circle.fill"
        case .error: "xmark.octagon.fill"
        default: "exclamationmark.triangle.fill"
        }
        let image = NSImageView(image: MenuStyle.symbol(symbolName, tone: status.tone) ?? NSImage())
        image.setAccessibilityElement(false)
        let label = NSTextField(labelWithString: status.text)
        label.font = MenuStyle.font
        label.lineBreakMode = .byTruncatingTail
        fix.isHidden = status.fixTitle == nil
        fix.title = status.fixTitle ?? ""
        let row = NSStackView(views: [image, label, NSView(), fix])
        row.orientation = .horizontal
        row.alignment = .centerY
        row.spacing = 8
        row.edgeInsets = NSEdgeInsets(top: 10, left: 14, bottom: 10, right: 12)
        return card(around: row)
    }

    private func keycap(_ title: String, caption: String) -> NSView {
        let label = NSTextField(labelWithString: title)
        label.font = .systemFont(ofSize: 20, weight: .medium)
        label.alignment = .center
        let small = NSTextField(labelWithString: caption)
        small.font = MenuStyle.secondaryFont
        small.textColor = .secondaryLabelColor
        let stack = NSStackView(views: [label, small])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 2
        stack.edgeInsets = NSEdgeInsets(top: 10, left: 26, bottom: 10, right: 26)
        return card(around: stack)
    }

    private func tryItViews(_ state: OnboardingState) -> [NSView] {
        tryField.translatesAutoresizingMaskIntoConstraints = false
        tryField.widthAnchor.constraint(equalToConstant: Self.size.width - 120).isActive = true
        let heard = state.flow.heardHotkey
        let image = NSImageView(image: MenuStyle.symbol(heard ? "checkmark.circle.fill" : "ellipsis.circle", tone: heard ? .ok : .normal) ?? NSImage())
        image.setAccessibilityElement(false)
        let label = NSTextField(labelWithString: heard ? "It works — Chirp heard \(state.hotkey.title)" : "Waiting for \(state.hotkey.title)…")
        label.font = MenuStyle.semiboldFont
        label.setAccessibilityIdentifier("try-it-status")
        let row = NSStackView(views: [image, label])
        row.orientation = .horizontal
        row.spacing = 6
        row.alignment = .centerY
        return [tryField, row]
    }

    /// A rounded, filled container like a grouped-list row.
    private func card(around inner: NSStackView) -> NSView {
        let box = NSBox()
        box.boxType = .custom
        box.cornerRadius = 10
        box.borderWidth = 1
        box.borderColor = .separatorColor
        box.fillColor = .controlBackgroundColor
        box.contentViewMargins = .zero
        let container = NSView()
        inner.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(inner)
        NSLayoutConstraint.activate([
            inner.topAnchor.constraint(equalTo: container.topAnchor), inner.bottomAnchor.constraint(equalTo: container.bottomAnchor),
            inner.leadingAnchor.constraint(equalTo: container.leadingAnchor), inner.trailingAnchor.constraint(equalTo: container.trailingAnchor),
        ])
        box.contentView = container
        box.translatesAutoresizingMaskIntoConstraints = false
        box.widthAnchor.constraint(greaterThanOrEqualToConstant: 200).isActive = true
        return box
    }

    private func rebuildDots(current: Int) {
        for view in dots.arrangedSubviews { dots.removeArrangedSubview(view); view.removeFromSuperview() }
        for index in 0..<OnboardingStep.allCases.count {
            let dot = ColorView(index == current ? .controlAccentColor : .tertiaryLabelColor, cornerRadius: 3.5)
            dot.translatesAutoresizingMaskIntoConstraints = false
            NSLayoutConstraint.activate([dot.widthAnchor.constraint(equalToConstant: 7), dot.heightAnchor.constraint(equalToConstant: 7)])
            dots.addArrangedSubview(dot)
        }
        dots.setAccessibilityLabel("Step \(current + 1) of \(OnboardingStep.allCases.count)")
    }
}

/// A rounded, filled view whose dynamic colour is resolved in the view's own appearance (a plain layer would freeze it
/// at the appearance current when the layer was created).
@MainActor
final class ColorView: NSView {
    private let color: NSColor
    private let radius: CGFloat

    init(_ color: NSColor, cornerRadius: CGFloat) {
        self.color = color
        self.radius = cornerRadius
        super.init(frame: .zero)
        wantsLayer = true
    }

    @available(*, unavailable) required init?(coder: NSCoder) { fatalError("not used") }

    override var wantsUpdateLayer: Bool { true }

    override func updateLayer() {
        layer?.cornerRadius = radius
        effectiveAppearance.performAsCurrentDrawingAppearance { layer?.backgroundColor = color.cgColor }
    }
}
