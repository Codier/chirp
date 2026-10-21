import AppKit
import ChirpCore

/// What the Settings window shows. The window controller reads it from the system; the snapshots pass fixed values.
public struct SettingsState: Sendable {
    public var hotkey: KeyCombo = .controlOptionSpace
    public var activationMode: ActivationMode = .autoStop
    /// Auto-stop: seconds of quiet that end a dictation.
    public var autoStopSilence = AutoStopDetector.defaultSilence
    public var serverURL = ServerConfig.defaultURL.absoluteString
    public var tokenPresent = true
    public var launch: LaunchState = .off
    /// General: the HUD's word waveform (each word's audio above it). Off by default.
    public var wordWaveform = false
    public var soundsEnabled = false
    public var soundPack: SoundPack = .default
    /// 0…1.
    public var soundVolume = SoundTiming.defaultVolume
    /// The user's own files for the Custom pack, as imported.
    public var customStart: CustomSound?
    public var customStop: CustomSound?
    public var micGranted = true
    public var accessibilityGranted = true
    /// The server's state in words ("Connected", "Unreachable", …) and its tone.
    public var serverStatus = "Connected"
    public var serverStatusTone: MenuEntry.Tone = .ok
    /// Clean-up: on or off, the default style, the edited style prompts, the raw-first option and the per-app styles.
    public var cleanupEnabled = false
    /// Where clean-up runs, or that the server has it off (under the checkbox), and whether it can be turned on.
    public var cleanupDetail = "Tidies punctuation and wording before the text is inserted."
    public var cleanupAvailable = true
    public var cleanupStyle = CorrectionProfile.defaultID
    /// The prompts the user edited, by style id (only styles that differ from the server's default).
    public var stylePrompts: [String: String] = [:]
    /// The styles with their default prompts, and whether they are live, the last-known copy, or not loaded.
    public var catalog = StyleCatalog.unavailable
    /// The style the prompt editor opens on.
    public var editingStyle = CorrectionProfile.defaultID
    public var insertRawFirst = false
    /// "Use raw" stays on offer for 5 seconds after a cleaned insertion (the hotkey and the menu row).
    public var offerRaw = true
    /// "Show changes after clean-up": the diff highlight and the HUD's Use raw button. Off by default.
    public var showChanges = false
    public var appStyles = AppStyleRule.defaults
    /// The styles the server offers (the built-ins until it says otherwise) and whether it offers any at all.
    public var profiles = CorrectionProfile.builtIn
    public var serverOffersStyles = true
    /// "Keep clean-up model loaded": what the server last said, and where its memory is used ("this Mac" or the host).
    public var keepLoaded = KeepLoadedState()
    public var serverHost = "this Mac"
    /// Some of Live Notes' speaker-label model is on this Mac, complete or not (Remove deletes it).
    public var speakerModelDownloaded = false

    public init() {}
}

/// The preferences window's toolbar tabs, in order.
public enum SettingsTab: String, CaseIterable, Sendable {
    case general, sounds, cleanup, server, permissions

    public var title: String {
        switch self {
        case .general: "General"
        case .sounds: "Sounds"
        case .cleanup: "Clean-up"
        case .server: "Server"
        case .permissions: "Permissions"
        }
    }

    /// The toolbar icon (SF Symbol).
    public var symbol: String {
        switch self {
        case .general: "gearshape"
        case .sounds: "speaker.wave.2"
        case .cleanup: "sparkles"
        case .server: "server.rack"
        case .permissions: "hand.raised"
        }
    }
}

/// A tab's content view. It accepts first responder so the window can park focus here instead of on a text field,
/// which would otherwise open selected.
public final class SettingsContentView: NSView {
    override public var acceptsFirstResponder: Bool { true }
}

/// The Settings window content, plain AppKit on purpose: the Command Line Tools toolchain has no SwiftUI macro
/// plugins (@State etc.). One view per toolbar tab (`SettingsTab`), each sized to its content: labels right-aligned in
/// one column and controls left-aligned in a second, with help text below its control. All controls live here and are
/// exposed so the owner can attach targets and actions.
@MainActor
public final class SettingsForm {
    static let margin: CGFloat = 20
    static let controlWidth: CGFloat = 340

    /// One content view per tab, each with its 20 pt margin pinned by constraints so `fittingSize` includes it on every side.
    private var tabViews: [SettingsTab: SettingsContentView] = [:]
    public let hotkey = NSPopUpButton(frame: .zero, pullsDown: false)
    public let recorder = HotkeyRecorder()
    /// Under the hotkey: how it works, or why the last recording was refused.
    public let hotkeyHelp = SettingsForm.help("")
    /// Fn/Globe has a separate macOS action that the user must turn off in Keyboard settings.
    public let fnKeyHelp = SettingsForm.help("To use fn / 🌐 without opening emoji or another macOS action, open System Settings → Keyboard and set “Press 🌐 key to” to “Do Nothing”.")
    public let keyboardSettingsButton = NSButton(title: "Open Keyboard Settings…", target: nil, action: nil)
    private var fnKeySetupRow: NSGridRow?
    /// Auto-stop or hold to talk, and (auto-stop only) how long a pause ends the dictation.
    public let mode = NSPopUpButton(frame: .zero, pullsDown: false)
    public let silence = NSPopUpButton(frame: .zero, pullsDown: false)
    private let modeHelp = SettingsForm.help("")
    private var shownHotkey = KeyCombo.controlOptionSpace
    public let sounds = NSButton(checkboxWithTitle: "Play start and stop sounds", target: nil, action: nil)
    /// Sounds: the pack, ▶ previews, the volume, and (for the Custom pack) the user's files.
    public let soundPack = NSPopUpButton(frame: .zero, pullsDown: false)
    public let previewStart = NSButton(title: "▶ Start", target: nil, action: nil)
    public let previewStop = NSButton(title: "▶ Stop", target: nil, action: nil)
    public let volume = NSSlider(value: SoundTiming.defaultVolume * 100, minValue: 0, maxValue: 100, target: nil, action: nil)
    public let chooseStart = NSButton(title: "Choose…", target: nil, action: nil)
    public let removeStart = NSButton(title: "Remove", target: nil, action: nil)
    public let chooseStop = NSButton(title: "Choose…", target: nil, action: nil)
    public let removeStop = NSButton(title: "Remove", target: nil, action: nil)
    /// Under the start file: the 0.3 s rule, or why the last import failed.
    public let customStartHelp = SettingsForm.help("")
    private let packHelp = SettingsForm.help("")
    private let volumeLabel = NSTextField(labelWithString: "")
    private let startFile = NSTextField(labelWithString: "")
    private let stopFile = NSTextField(labelWithString: "")
    private var customStartSound: CustomSound?
    private var customStopSound: CustomSound?
    private var soundError: String?
    /// The Start sound and Stop sound rows of the Sounds grid, shown only for the Custom pack.
    private var customRows: [Int] = []
    private var soundsGrid: NSGridView { grids[.sounds]! }
    public let serverURL = NSTextField(string: "")
    public let launch = NSButton(checkboxWithTitle: "Launch Chirp at login", target: nil, action: nil)
    /// General: the HUD's word waveform.
    public let wordWaveform = NSButton(checkboxWithTitle: "Word waveform overlay", target: nil, action: nil)
    /// General: reopens the setup walk-through.
    public let setupButton = NSButton(title: "Set Up Chirp…", target: nil, action: nil)
    /// Server: puts the chirp-server doctor command on the pasteboard.
    public let doctorButton = NSButton(title: "Copy Doctor Command", target: nil, action: nil)
    public let modelsButton = NSButton(title: "Manage Local Models…", target: nil, action: nil)
    /// General: deletes Live Notes' on-device speaker model.
    public let removeSpeakerModel = NSButton(title: "Remove Speaker Model", target: nil, action: nil)
    public let micButton = NSButton(title: "Fix…", target: nil, action: nil)
    public let accessibilityButton = NSButton(title: "Fix…", target: nil, action: nil)
    /// Under the checkbox: what it does, or why the last change failed.
    public let launchHelp = SettingsForm.help("")
    /// Clean-up: default style, style prompts, raw-first and the per-app table.
    public let cleanupEnabled = NSButton(checkboxWithTitle: "Clean up dictated text", target: nil, action: nil)
    public let style = NSPopUpButton(frame: .zero, pullsDown: false)
    public let prompts = StylePromptsEditor()
    public let insertRawFirst = NSButton(checkboxWithTitle: "Insert raw immediately, then replace with cleaned", target: nil, action: nil)
    public let showChanges = NSButton(checkboxWithTitle: "Show changes after clean-up", target: nil, action: nil)
    public let offerRaw = NSButton(checkboxWithTitle: "Offer “Use raw” for 5 seconds after clean-up", target: nil, action: nil)
    public let appStyles = AppStylesTable()
    /// Clean-up: keeps the server's clean-up model in memory. Its state, memory cost and any error sit under it.
    public let keepLoaded = NSButton(checkboxWithTitle: KeepLoaded.title, target: nil, action: nil)
    private let keepLoadedStatus = StatusLine()
    private let keepLoadedHelp = SettingsForm.help("")
    private let keepLoadedError = SettingsForm.help("")
    private let styleHelp = SettingsForm.help("")
    private let cleanupHelp = SettingsForm.help("")
    private var grids: [SettingsTab: NSGridView] = [:]
    private var shownProfileIDs: [String] = []
    private var shownCatalog: StyleCatalog?
    private let tokenStatus = StatusLine()
    private let tokenHelp = SettingsForm.help("")
    private let serverStatus = StatusLine()
    private let micStatus = StatusLine()
    private let accessibilityStatus = StatusLine()
    private var launchUnavailable = false

    public init(_ state: SettingsState) {
        recorder.onStatusChange = { [weak self] _ in self?.refreshHotkeyHelp() }
        serverURL.placeholderString = ServerConfig.defaultURL.absoluteString
        serverURL.font = MenuStyle.font
        serverURL.lineBreakMode = .byTruncatingMiddle
        serverURL.widthAnchor.constraint(equalToConstant: Self.controlWidth).isActive = true
        for button in [micButton, accessibilityButton] { button.bezelStyle = .rounded; button.controlSize = .small }
        micStatus.accessory = micButton
        accessibilityStatus.accessory = accessibilityButton
        micButton.setAccessibilityLabel("Fix Microphone access")
        accessibilityButton.setAccessibilityLabel("Fix Accessibility access")

        for button in [setupButton, doctorButton, modelsButton, removeSpeakerModel] { button.bezelStyle = .rounded }
        setupButton.setAccessibilityLabel("Set up Chirp")
        keyboardSettingsButton.bezelStyle = .rounded
        keyboardSettingsButton.controlSize = .small

        func row(_ title: String, _ control: NSView, help: NSTextField? = nil) -> [NSView] {
            var content = control
            if let help {
                let stack = NSStackView(views: [control, help])
                stack.orientation = .vertical
                stack.alignment = .leading
                stack.spacing = 4
                content = stack
            }
            return [Self.label(title), content]
        }

        let hotkeyRow = NSStackView(views: [hotkey, recorder])
        hotkeyRow.orientation = .horizontal
        hotkeyRow.spacing = 10
        let fnKeySetup = NSStackView(views: [fnKeyHelp, keyboardSettingsButton])
        fnKeySetup.orientation = .vertical
        fnKeySetup.alignment = .leading
        fnKeySetup.spacing = 4
        build(.general, [
            row("Hotkey:", hotkeyRow, help: hotkeyHelp),
            row("", fnKeySetup),
            row("Mode:", mode, help: modeHelp),
            row("Auto-stop:", silence),
            row("Overlay:", wordWaveform, help: Self.help("Draws each word's audio above it as you speak, so you can see what was heard.")),
            row("Startup:", launch, help: launchHelp),
            row("Setup:", setupButton, help: Self.help("Check the permissions and the server, and try a dictation.")),
            row("Speakers:", removeSpeakerModel),
        ])
        fnKeySetupRow = grids[.general]?.row(at: 1)

        let packRow = NSStackView(views: [soundPack, previewStart, previewStop])
        packRow.orientation = .horizontal
        packRow.spacing = 8
        let volumeRow = NSStackView(views: [volume, volumeLabel])
        volumeRow.orientation = .horizontal
        volumeRow.spacing = 8
        build(.sounds, [
            row("Cues:", sounds, help: Self.help("Quiet sounds when dictation starts and stops. Off keeps Chirp silent.")),
            row("Pack:", packRow, help: packHelp),
            row("Volume:", volumeRow),
            row("Start sound:", Self.fileRow(startFile, choose: chooseStart, remove: removeStart), help: customStartHelp),
            row("Stop sound:", Self.fileRow(stopFile, choose: chooseStop, remove: removeStop),
                help: Self.help("Plays after you stop talking, up to \(SoundTiming.stopLimit) s.")),
        ], customRows: [3, 4])

        let keepLoadedStack = NSStackView(views: [keepLoaded, keepLoadedStatus, keepLoadedHelp, keepLoadedError])
        keepLoadedStack.orientation = .vertical
        keepLoadedStack.alignment = .leading
        keepLoadedStack.spacing = 4
        keepLoadedError.textColor = .systemRed
        keepLoadedError.isHidden = true
        keepLoadedStatus.setAccessibilityLabel("Clean-up model status")

        build(.cleanup, [
            row("Clean-up:", cleanupEnabled, help: cleanupHelp),
            row("Model:", keepLoadedStack),
            row("Style:", style, help: styleHelp),
            row("Prompts:", prompts.view),
            row("Per-app:", appStyles.view, help: Self.help("The frontmost app when you start dictating picks the style. Other apps use the default style.")),
            row("Insertion:", insertRawFirst, help: Self.help("Swaps in the cleaned text in place where Chirp can verify the insert; elsewhere it inserts once.")),
            row("Review:", showChanges, help: Self.help("Off: the cleaned text is inserted at once. On: the HUD highlights what changed and keeps a Use raw button.")),
            row("Undo:", offerRaw, help: Self.help("Puts the original words back, with ⌃⌥Z or from the menu.")),
        ])

        build(.server, [
            row("URL:", serverURL),
            row("Token:", tokenStatus, help: tokenHelp),
            row("Status:", serverStatus),
            row("Doctor:", doctorButton, help: Self.help("Copies the command that checks the server, to run in Terminal.")),
            row("Models:", modelsButton, help: Self.help("Download speech and local Qwen models, see progress, and start this Mac's server.")),
        ])

        build(.permissions, [
            row("Microphone:", micStatus, help: Self.help("Lets Chirp hear you.")),
            row("Accessibility:", accessibilityStatus, help: Self.help("Lets Chirp insert text into other apps and send ⌘V.")),
        ])
        for m in ActivationMode.allCases {
            mode.addItem(withTitle: m.title)
            mode.lastItem?.representedObject = m.rawValue
        }
        for seconds in AutoStopDetector.silenceChoices {
            silence.addItem(withTitle: "After \(seconds.formatted(.number.precision(.fractionLength(0...1)))) s of quiet")
            silence.lastItem?.representedObject = seconds
        }
        refreshHotkeyHelp()
        configureSoundControls()
        apply(state, full: true)
    }

    private func configureSoundControls() {
        for pack in SoundPack.allCases {
            soundPack.addItem(withTitle: pack.title)
            soundPack.lastItem?.representedObject = pack.rawValue
        }
        for b in [previewStart, previewStop, chooseStart, removeStart, chooseStop, removeStop] {
            b.bezelStyle = .rounded
            b.controlSize = .small
        }
        previewStart.setAccessibilityLabel("Preview the start sound")
        previewStop.setAccessibilityLabel("Preview the stop sound")
        chooseStart.setAccessibilityLabel("Choose a start sound file")
        chooseStop.setAccessibilityLabel("Choose a stop sound file")
        removeStart.setAccessibilityLabel("Remove the custom start sound")
        removeStop.setAccessibilityLabel("Remove the custom stop sound")
        soundPack.setAccessibilityLabel("Sound pack")
        volume.isContinuous = true
        volume.setAccessibilityLabel("Volume")
        volume.widthAnchor.constraint(equalToConstant: 200).isActive = true
        volumeLabel.font = MenuStyle.font
        volumeLabel.alignment = .right
        volumeLabel.widthAnchor.constraint(equalToConstant: 40).isActive = true
        for f in [startFile, stopFile] {
            f.font = MenuStyle.font
            f.lineBreakMode = .byTruncatingMiddle
            f.widthAnchor.constraint(equalToConstant: 170).isActive = true
        }
    }

    /// The label under the slider follows it live.
    public func setVolume(_ fraction: Double) {
        volume.doubleValue = fraction * 100
        volumeLabel.stringValue = "\(SoundTiming.percent(volume: fraction))%"
    }

    /// The Custom rows show only for the Custom pack.
    public func showPack(_ pack: SoundPack) {
        if let i = soundPack.itemArray.firstIndex(where: { $0.representedObject as? String == pack.rawValue }) { soundPack.selectItem(at: i) }
        packHelp.stringValue = pack.detail
        for index in customRows { soundsGrid.row(at: index).isHidden = pack != .custom }
    }

    /// Shows the imported files (name and length) and the trimming warning; `error` replaces the warning once.
    public func setCustomSounds(start: CustomSound?, stop: CustomSound?, error: String? = nil) {
        customStartSound = start
        customStopSound = stop
        soundError = error
        startFile.stringValue = start.map { "\($0.displayName) · \(SoundTiming.durationText($0.duration))" } ?? "None (uses Chirp)"
        stopFile.stringValue = stop.map { "\($0.displayName) · \(SoundTiming.durationText($0.duration))" } ?? "None (uses Chirp)"
        startFile.textColor = start == nil ? .secondaryLabelColor : .labelColor
        stopFile.textColor = stop == nil ? .secondaryLabelColor : .labelColor
        removeStart.isEnabled = start != nil
        removeStop.isEnabled = stop != nil
        if let error {
            customStartHelp.stringValue = error
            customStartHelp.textColor = .systemRed
        } else if let start, SoundTiming.isTrimmed(.start, duration: start.duration) {
            customStartHelp.stringValue = "\(SoundTiming.longStartWarning) Yours is \(SoundTiming.durationText(start.duration))."
            customStartHelp.textColor = MenuStyle.textColor(.warning)
        } else {
            customStartHelp.stringValue = SoundTiming.longStartWarning
            customStartHelp.textColor = .secondaryLabelColor
        }
    }

    /// Lays `rows` out as `tab`'s content: a grid with a 20 pt margin on every side.
    private func build(_ tab: SettingsTab, _ rows: [[NSView]], customRows custom: [Int] = []) {
        let grid = NSGridView(views: rows)
        grid.rowSpacing = 10
        grid.columnSpacing = 10
        grid.rowAlignment = .firstBaseline
        grid.column(at: 0).xPlacement = .trailing
        grid.column(at: 1).xPlacement = .leading
        grid.translatesAutoresizingMaskIntoConstraints = false
        let view = SettingsContentView()
        view.addSubview(grid)
        NSLayoutConstraint.activate([
            grid.topAnchor.constraint(equalTo: view.topAnchor, constant: Self.margin),
            grid.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: Self.margin),
            view.bottomAnchor.constraint(equalTo: grid.bottomAnchor, constant: Self.margin),
            view.trailingAnchor.constraint(equalTo: grid.trailingAnchor, constant: Self.margin),
        ])
        grids[tab] = grid
        tabViews[tab] = view
        customRows = custom.isEmpty ? customRows : custom
    }

    /// The content of `tab`.
    public func view(for tab: SettingsTab) -> SettingsContentView { tabViews[tab]! }

    /// The size `tab` needs: its grid at its natural size plus the margin. Measured on the grid, not the content view,
    /// because the window sizes the content view (its fitting size would then just echo the size it was given).
    public func fittingSize(for tab: SettingsTab) -> NSSize {
        let grid = grids[tab]!
        let size = grid.fittingSize
        return NSSize(width: size.width + 2 * Self.margin, height: size.height + 2 * Self.margin)
    }

    /// Fills the window. The periodic refresh passes `full: false` so it only follows the server and permission
    /// states and never overwrites what the user is typing or a login-item error shown under the checkbox.
    public func apply(_ s: SettingsState, full: Bool = false) {
        if full {
            fillHotkeyMenu(selecting: s.hotkey)
            showMode(s.activationMode, silence: s.autoStopSilence)
            sounds.state = s.soundsEnabled ? .on : .off
            showPack(s.soundPack)
            setVolume(s.soundVolume)
            setCustomSounds(start: s.customStart, stop: s.customStop)
            serverURL.stringValue = s.serverURL
            launch.state = s.launch == .on ? .on : .off
            launch.isEnabled = s.launch != .unavailable
            wordWaveform.state = s.wordWaveform ? .on : .off
            launchUnavailable = s.launch == .unavailable
            setLaunchError(nil)
            style.controlSize = .regular
            cleanupEnabled.state = s.cleanupEnabled ? .on : .off
            offerRaw.state = s.offerRaw ? .on : .off
            fillStyleMenu(s.profiles, selecting: s.cleanupStyle, offered: s.serverOffersStyles)
            prompts.configure(catalog: s.catalog, overrides: s.stylePrompts, selecting: s.editingStyle)
            shownCatalog = s.catalog
            insertRawFirst.state = s.insertRawFirst ? .on : .off
            showChanges.state = s.showChanges ? .on : .off
            appStyles.setProfiles(s.profiles)
            appStyles.setRules(s.appStyles)
        } else if s.profiles.map(\.id) != shownProfileIDs {
            // The server's style list arrived after the window opened: follow it without touching what is being edited.
            let current = style.selectedItem?.representedObject as? String ?? s.cleanupStyle
            fillStyleMenu(s.profiles, selecting: current, offered: s.serverOffersStyles)
            appStyles.setProfiles(s.profiles)
        }
        if !full, s.catalog != shownCatalog {
            // The server's prompts arrived (or it went away) after the window opened. Edits are saved as they are
            // made, so reloading from the saved ones loses nothing.
            prompts.configure(catalog: s.catalog, overrides: s.stylePrompts)
            shownCatalog = s.catalog
        }
        showKeepLoaded(s.keepLoaded, host: s.serverHost)
        cleanupHelp.stringValue = s.cleanupDetail
        cleanupEnabled.isEnabled = s.cleanupAvailable
        tokenStatus.set(symbol: s.tokenPresent ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                        tone: s.tokenPresent ? .ok : .warning, text: "~/.config/chirp/token")
        tokenHelp.stringValue = s.tokenPresent ? "Chirp sends this token with every request."
                                               : "No token yet — start chirp-server once to create it."
        serverStatus.set(symbol: "circle.fill", tone: s.serverStatusTone, text: s.serverStatus)
        modelsButton.isEnabled = ServerConfig.load(urlString: s.serverURL).isLoopback
        removeSpeakerModel.isEnabled = s.speakerModelDownloaded
        removeSpeakerModel.title = s.speakerModelDownloaded ? "Remove Speaker Model" : "Speaker Model Not Downloaded"
        removeSpeakerModel.toolTip = s.speakerModelDownloaded
            ? "Live Notes labels speakers with a model on this Mac. Removing it turns labels off until you download it again."
            : "Live Notes downloads its speaker model only when you turn on Label speakers, and asks first."
        micStatus.set(symbol: s.micGranted ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                      tone: s.micGranted ? .ok : .warning, text: s.micGranted ? "Allowed" : "Not allowed")
        accessibilityStatus.set(symbol: s.accessibilityGranted ? "checkmark.circle.fill" : "exclamationmark.triangle.fill",
                                tone: s.accessibilityGranted ? .ok : .warning, text: s.accessibilityGranted ? "Allowed" : "Not allowed")
        micButton.isHidden = s.micGranted
        accessibilityButton.isHidden = s.accessibilityGranted
    }

    /// The "Keep clean-up model loaded" switch, its status and help, or why it is unavailable; the periodic refresh
    /// calls this too, so it only sets what changed.
    public func showKeepLoaded(_ state: KeepLoadedState, host: String) {
        let p = KeepLoadedPresentation(state, host: host)
        keepLoaded.state = p.isOn ? .on : .off
        keepLoaded.isEnabled = p.isEnabled
        keepLoaded.setAccessibilityHelp(p.help)
        keepLoadedHelp.stringValue = p.help
        keepLoadedStatus.isHidden = p.status == nil
        if let status = p.status {
            switch p.statusTone {
            case .ok: keepLoadedStatus.set(symbol: "circle.fill", tone: .ok, text: status)
            case .busy: keepLoadedStatus.set(symbol: "circle.fill", tone: .warning, text: status)
            case .idle: keepLoadedStatus.set(symbol: "circle", tone: .normal, text: status)
            }
            keepLoadedStatus.setAccessibilityLabel("Clean-up model: \(status)")
        }
        keepLoadedError.isHidden = p.error == nil
        if let error = p.error {
            keepLoadedError.stringValue = error
            keepLoadedError.setAccessibilityLabel("Error: \(error)")
        }
    }

    /// The presets, plus the recorded shortcut when one is in use, with `current` selected.
    public func fillHotkeyMenu(selecting current: KeyCombo) {
        shownHotkey = current
        refreshHotkeyHelp()
        hotkey.removeAllItems()
        for combo in KeyCombo.presets + (current.isCustom ? [current] : []) {
            hotkey.addItem(withTitle: combo.isCustom ? "\(combo.title) (custom)" : combo.title)
            hotkey.lastItem?.representedObject = combo.id
        }
        if let index = hotkey.itemArray.firstIndex(where: { $0.representedObject as? String == current.id }) {
            hotkey.selectItem(at: index)
        }
    }

    /// The styles, with `current` selected (an id the server no longer lists stays selectable, so it is not silently changed).
    public func fillStyleMenu(_ profiles: [CorrectionProfile], selecting current: String, offered: Bool = true) {
        var list = profiles
        if !list.contains(where: { $0.id == current }) {
            list.append(CorrectionProfile(id: current, name: CorrectionProfile.displayName(for: current)))
        }
        shownProfileIDs = profiles.map(\.id)
        style.removeAllItems()
        for p in list {
            style.addItem(withTitle: p.name)
            style.lastItem?.representedObject = p.id
            style.lastItem?.toolTip = p.description
        }
        if let i = list.firstIndex(where: { $0.id == current }) { style.selectItem(at: i) }
        let chosen = list.first { $0.id == current }
        var text = chosen?.description ?? ""
        if chosen?.rewrites == true { text += (text.isEmpty ? "" : " ") + "May restructure your text." }
        if !offered { text = "This server does not offer styles yet; update chirp-server to use them." }
        styleHelp.stringValue = text
    }

    /// Selects `mode` and the auto-stop pause (the nearest offered one), which only matters in auto-stop.
    public func showMode(_ m: ActivationMode, silence seconds: TimeInterval) {
        if let i = mode.itemArray.firstIndex(where: { $0.representedObject as? String == m.rawValue }) { mode.selectItem(at: i) }
        modeHelp.stringValue = m.help
        let nearest = AutoStopDetector.silenceChoices.enumerated().min { abs($0.element - seconds) < abs($1.element - seconds) }
        if let nearest { silence.selectItem(at: nearest.offset) }
        silence.isEnabled = m == .autoStop
    }

    private func refreshHotkeyHelp() {
        fnKeySetupRow?.isHidden = !(shownHotkey.isModifierOnly && shownHotkey.modifiers == ModifierKeys.fn)
        hotkeyHelp.textColor = .secondaryLabelColor
        switch recorder.status {
        case .idle:
            hotkeyHelp.stringValue = shownHotkey.isModifierOnly
                ? "Only on its own; with another key (\(HotKeyValidation.modifierSymbols(shownHotkey.modifiers))←) it does nothing. Esc cancels."
                : "Any key combination, or a modifier alone like ⌥. Esc cancels."
        case .recording: hotkeyHelp.stringValue = "Press the shortcut, or press and release a modifier such as ⌥. Esc gives up."
        case .rejected(let why):
            hotkeyHelp.stringValue = why.reason
            hotkeyHelp.textColor = .systemRed
        }
    }

    /// Shows why toggling launch at login failed under the checkbox; nil restores the usual help text.
    public func setLaunchError(_ message: String?) {
        if let message {
            launchHelp.stringValue = "Couldn’t change it: \(message)"
            launchHelp.textColor = .systemRed
        } else {
            launchHelp.stringValue = launchUnavailable ? "Available when Chirp runs from its app bundle."
                                                       : "Opens Chirp when you log in."
            launchHelp.textColor = .secondaryLabelColor
        }
    }

    // MARK: pieces

    /// A file's name and length, then Choose… and Remove.
    private static func fileRow(_ name: NSTextField, choose: NSButton, remove: NSButton) -> NSView {
        let stack = NSStackView(views: [name, choose, remove])
        stack.orientation = .horizontal
        stack.alignment = .centerY
        stack.spacing = 8
        return stack
    }

    private static func label(_ text: String) -> NSTextField {
        let f = NSTextField(labelWithString: text)
        f.font = MenuStyle.font
        f.alignment = .right
        return f
    }

    private static func heading(_ text: String) -> NSTextField {
        let f = NSTextField(labelWithString: text)
        f.font = MenuStyle.semiboldFont
        return f
    }

    /// What the server says about clean-up: where it runs, or that it is off.
    public static func cleanupDetail(health: Health?) -> (text: String, available: Bool) {
        guard let h = health else { return ("Needs the server. Tidies punctuation and wording before the text is inserted.", true) }
        if !h.correctionAvailable { return ("The server has clean-up turned off; see the README to enable it.", false) }
        if h.correctionLeavesMachine == true { return ("Sends the text to \(h.correctionHost ?? "a remote LLM") to tidy it.", true) }
        return ("Runs on this Mac: tidies punctuation and wording before the text is inserted.", true)
    }

    /// Secondary-colour help text under a control, wrapped to the control column's width.
    static func help(_ text: String) -> NSTextField {
        let f = NSTextField(wrappingLabelWithString: text)
        f.font = MenuStyle.secondaryFont
        f.textColor = .secondaryLabelColor
        f.preferredMaxLayoutWidth = controlWidth
        return f
    }
}

/// A status symbol, a label and an optional trailing button (hidden when it has nothing to fix).
@MainActor
private final class StatusLine: NSStackView {
    private let icon = NSImageView()
    private let text = NSTextField(labelWithString: "")
    var accessory: NSView? { didSet { if let accessory { addArrangedSubview(accessory) } } }

    init() {
        super.init(frame: .zero)
        orientation = .horizontal
        alignment = .centerY
        spacing = 6
        text.font = MenuStyle.font
        icon.setContentHuggingPriority(.required, for: .horizontal)
        icon.widthAnchor.constraint(equalToConstant: 14).isActive = true
        icon.setAccessibilityElement(false)
        addArrangedSubview(icon)
        addArrangedSubview(text)
        setCustomSpacing(10, after: text)
    }

    @available(*, unavailable) required init?(coder: NSCoder) { fatalError("not used") }

    func set(symbol: String, tone: MenuEntry.Tone, text string: String) {
        icon.image = MenuStyle.symbol(symbol, tone: tone)
        icon.contentTintColor = tone == .normal ? .tertiaryLabelColor : nil
        text.stringValue = string
    }
}
