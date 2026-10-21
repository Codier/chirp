import AppKit
import UniformTypeIdentifiers
import ChirpCore
import ChirpSpeakers
import ChirpUI

@MainActor
final class SettingsWindowController: NSObject, NSTextFieldDelegate {
    /// Called after the hotkey preset changes so the app can re-register it.
    var onHotkeyChange: (() -> Void)?
    /// The shortcut recorder started or stopped listening.
    var onRecordingChanged: ((Bool) -> Void)?
    /// The server's state for the Status row, from the app's last health check.
    var serverStatus: () -> (text: String, tone: MenuEntry.Tone) = { ("Checking…", .warning) }
    /// The server's last health, for the styles it offers.
    var health: () -> Health? = { nil }
    /// The styles with their default prompts (live, or the last-known copy).
    var catalog: () -> StyleCatalog = { .unavailable }
    /// "Set Up Chirp…" in General.
    var onOpenSetup: (() -> Void)?
    var onOpenModels: (() -> Void)?
    var onRemoveSpeakerModel: (() -> Void)?
    private var testTask: Task<Void, Never>?
    private var window: NSWindow?
    private var tabs: NSTabViewController?
    private var form: SettingsForm?
    private var refreshTimer: Timer?
    /// What the server last said about keeping the clean-up model loaded, and the request that is updating it.
    private var keepLoaded = KeepLoadedState()
    private var keepLoadedTask: Task<Void, Never>?

    private var state: SettingsState {
        var s = SettingsState()
        s.hotkey = settings.hotkey
        s.activationMode = settings.activationMode
        s.autoStopSilence = settings.autoStopSilence
        s.serverURL = settings.serverURL
        s.tokenPresent = ServerConfig.readToken() != nil
        s.launch = LaunchAtLogin.state
        s.wordWaveform = settings.wordWaveform
        s.soundsEnabled = settings.soundsEnabled
        s.soundPack = settings.soundPack
        s.soundVolume = settings.soundVolume
        s.customStart = settings.customStartSound
        s.customStop = settings.customStopSound
        s.micGranted = Permissions.microphoneGranted
        s.accessibilityGranted = Permissions.accessibilityGranted
        (s.serverStatus, s.serverStatusTone) = serverStatus()
        s.cleanupStyle = settings.correctionProfile
        s.stylePrompts = settings.correctionPrompts
        s.cleanupEnabled = settings.correction
        s.offerRaw = settings.showRawAfterCleanup
        s.insertRawFirst = settings.insertRawFirst
        s.showChanges = settings.showChangesAfterCleanup
        s.appStyles = settings.appStyles
        let health = health()
        (s.cleanupDetail, s.cleanupAvailable) = SettingsForm.cleanupDetail(health: health)
        let catalog = catalog()
        s.catalog = catalog
        s.profiles = catalog.source == .unavailable ? CorrectionProfile.offered(by: health) : catalog.profiles
        s.serverOffersStyles = health == nil || health?.correctionProfiles != nil
        s.keepLoaded = keepLoaded
        s.speakerModelDownloaded = SpeakerModel.hasFiles()
        s.serverHost = KeepLoaded.hostPhrase(ServerConfig(baseURL: URL(string: settings.serverURL) ?? ServerConfig.defaultURL))
        return s
    }

    /// Opens Settings, on `tab` when given (else on the tab it was left on).
    func show(tab: SettingsTab? = nil) {
        if window == nil { window = makeWindow() }
        form?.apply(state)
        if let tab, let tabs { tabs.selectedTabViewItemIndex = SettingsTab.allCases.firstIndex(of: tab) ?? 0 }
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { if let self { self.form?.apply(self.state) } }
        }
        NSApp.activate()
        window?.makeKeyAndOrderFront(nil)
        // Ordering the window front lets AppKit pick the first text field; park focus on the content instead.
        if let window, let tabs, let current = tabs.tabViewItems[tabs.selectedTabViewItemIndex].viewController?.view {
            window.makeFirstResponder(current)
        }
        if SettingsTab.allCases[tabs?.selectedTabViewItemIndex ?? 0] == .cleanup { refreshKeepLoaded() }
    }

    // MARK: keep clean-up model loaded

    private var serverConfig: ServerConfig { ServerConfig.load(urlString: settings.serverURL) }

    private func showKeepLoaded() {
        form?.showKeepLoaded(keepLoaded, host: state.serverHost)
    }

    /// Asks the server, and keeps asking every 2 s (for up to 90 s) while the model is loading so the status follows it.
    private func refreshKeepLoaded() {
        keepLoadedTask?.cancel()
        let config = serverConfig
        keepLoadedTask = Task { @MainActor [weak self] in
            for _ in 0..<45 {
                do throws(CleanupSettingsError) {
                    let s = try await config.fetchCleanupSettings()
                    guard !Task.isCancelled, let self else { return }
                    self.keepLoaded.fetched(s)
                } catch {
                    guard !Task.isCancelled, let self else { return }
                    self.keepLoaded.fetchFailed(error)
                }
                guard let self else { return }
                self.showKeepLoaded()
                guard self.keepLoaded.isLoading, self.window?.isVisible == true else { return }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    /// The switch changes at once; a refused change puts it back with the reason under it. Either way the server is
    /// asked again afterwards, so the status shows what it really did.
    @objc private func toggleKeepLoaded(_ sender: NSButton) {
        let on = sender.state == .on
        guard keepLoaded.toggle(to: on) else { showKeepLoaded(); return }
        showKeepLoaded()
        keepLoadedTask?.cancel()
        let config = serverConfig
        keepLoadedTask = Task { @MainActor [weak self] in
            do throws(CleanupSettingsError) {
                let s = try await config.setKeepLoaded(on)
                self?.keepLoaded.toggleSucceeded(s)
            } catch {
                self?.keepLoaded.toggleFailed(error)
            }
            self?.showKeepLoaded()
            self?.refreshKeepLoaded()
        }
    }

    /// The Custom sound rows came or went: the tab is a different height now, and the window follows it, top edge fixed.
    private func tabSizeChanged() {
        guard let window, let tabs else { return }
        let index = tabs.selectedTabViewItemIndex
        guard let tabController = tabs.tabViewItems[index].viewController as? SettingsTabController else { return }
        tabController.refreshSize()
        let target = tabController.preferredContentSize
        let current = window.contentRect(forFrameRect: window.frame).size
        guard abs(target.height - current.height) > 0.5 else { return }
        var frame = window.frame
        let newFrame = window.frameRect(forContentRect: NSRect(origin: .zero, size: NSSize(width: current.width, height: target.height)))
        frame.origin.y += frame.height - newFrame.height
        frame.size.height = newFrame.height
        window.setFrame(frame, display: true, animate: true)
    }

    private func makeWindow() -> NSWindow {
        let form = SettingsForm(state)
        form.setupButton.target = self
        form.setupButton.action = #selector(openSetup)
        form.doctorButton.target = self
        form.doctorButton.action = #selector(copyDoctor)
        form.modelsButton.target = self
        form.modelsButton.action = #selector(openModels)
        form.removeSpeakerModel.target = self
        form.removeSpeakerModel.action = #selector(removeSpeakerModel)
        form.cleanupEnabled.target = self
        form.cleanupEnabled.action = #selector(toggleCleanup(_:))
        form.keepLoaded.target = self
        form.keepLoaded.action = #selector(toggleKeepLoaded(_:))
        form.offerRaw.target = self
        form.offerRaw.action = #selector(toggleOfferRaw(_:))
        form.hotkey.target = self
        form.hotkey.action = #selector(hotkeyChanged(_:))
        form.keyboardSettingsButton.target = self
        form.keyboardSettingsButton.action = #selector(openKeyboardSettings)
        form.recorder.onCapture = { [weak self] combo in
            settings.hotkey = combo
            self?.form?.fillHotkeyMenu(selecting: combo)
            self?.tabSizeChanged()
            self?.onHotkeyChange?()
        }
        form.recorder.onRecordingChanged = { [weak self] recording in self?.onRecordingChanged?(recording) }
        form.mode.target = self
        form.mode.action = #selector(modeChanged(_:))
        form.silence.target = self
        form.silence.action = #selector(silenceChanged(_:))
        form.sounds.target = self
        form.sounds.action = #selector(toggleSounds(_:))
        form.soundPack.target = self
        form.soundPack.action = #selector(packChanged(_:))
        form.volume.target = self
        form.volume.action = #selector(volumeChanged(_:))
        for (button, action) in [(form.previewStart, #selector(previewStart)), (form.previewStop, #selector(previewStop)),
                                 (form.chooseStart, #selector(chooseStart)), (form.chooseStop, #selector(chooseStop)),
                                 (form.removeStart, #selector(removeStart)), (form.removeStop, #selector(removeStop))] {
            button.target = self
            button.action = action
        }
        form.style.target = self
        form.style.action = #selector(styleChanged(_:))
        form.prompts.onEdit = { [weak self] id, text in
            guard let self else { return }
            settings.setPrompt(text, for: id, in: self.catalog())
        }
        form.prompts.onReset = { settings.resetPrompt(for: $0) }
        form.appStyles.testButton.target = self
        form.appStyles.testButton.action = #selector(testAppStyle)
        form.showChanges.target = self
        form.showChanges.action = #selector(toggleShowChanges(_:))
        form.insertRawFirst.target = self
        form.insertRawFirst.action = #selector(toggleRawFirst(_:))
        form.appStyles.onChange = { settings.appStyles = $0 }
        form.appStyles.onAdd = { [weak self] button in self?.showAppChooser(from: button) }
        form.serverURL.delegate = self
        form.launch.target = self
        form.launch.action = #selector(toggleLaunch(_:))
        form.wordWaveform.target = self
        form.wordWaveform.action = #selector(toggleWordWaveform(_:))
        form.micButton.target = self
        form.micButton.action = #selector(grantMic)
        form.accessibilityButton.target = self
        form.accessibilityButton.action = #selector(grantAX)
        self.form = form

        let tabs = SettingsTabs.makeController(form: form) { [weak self] tab in
            if tab == .cleanup { self?.refreshKeepLoaded() }
        }
        self.tabs = tabs
        let w = NSWindow(contentViewController: tabs)
        w.styleMask = [.titled, .closable]
        w.isReleasedWhenClosed = false
        w.center()
        return w
    }

    func controlTextDidChange(_ obj: Notification) {
        if let f = obj.object as? NSTextField { settings.serverURL = f.stringValue }
    }

    @objc private func openSetup() { onOpenSetup?() }
    @objc private func openModels() { onOpenModels?() }
    @objc private func openKeyboardSettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.Keyboard-Settings.extension")!)
    }
    @objc private func removeSpeakerModel() { onRemoveSpeakerModel?() }

    /// The speaker model came or went: refresh the General tab's row.
    func speakerModelChanged() { form?.apply(state) }

    @objc private func copyDoctor() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(DoctorHelp.clipboardText(serverURL: settings.serverURL), forType: .string)
    }

    @objc private func toggleCleanup(_ sender: NSButton) { settings.correction = sender.state == .on }
    @objc private func toggleWordWaveform(_ sender: NSButton) { settings.wordWaveform = sender.state == .on }
    @objc private func toggleOfferRaw(_ sender: NSButton) { settings.showRawAfterCleanup = sender.state == .on }

    @objc private func hotkeyChanged(_ sender: NSPopUpButton) {
        settings.hotkey = KeyCombo.resolve(id: sender.selectedItem?.representedObject as? String)
        form?.fillHotkeyMenu(selecting: settings.hotkey)
        tabSizeChanged()
        onHotkeyChange?()
    }

    @objc private func modeChanged(_ sender: NSPopUpButton) {
        settings.activationMode = (sender.selectedItem?.representedObject as? String).flatMap(ActivationMode.init(rawValue:)) ?? .autoStop
        form?.showMode(settings.activationMode, silence: settings.autoStopSilence)
        onHotkeyChange?()
    }

    @objc private func silenceChanged(_ sender: NSPopUpButton) {
        guard let seconds = sender.selectedItem?.representedObject as? Double else { return }
        settings.autoStopSilence = seconds
    }

    @objc private func styleChanged(_ sender: NSPopUpButton) {
        guard let id = sender.selectedItem?.representedObject as? String else { return }
        settings.correctionProfile = id
        form?.fillStyleMenu(state.profiles, selecting: id, offered: state.serverOffersStyles)
    }

    /// "Test": Settings itself is frontmost while the button is pressed, so wait a moment for the user to switch to the
    /// app to check, then say which style applies to it.
    @objc private func testAppStyle() {
        testTask?.cancel()
        form?.appStyles.showTestResult("Switch to the app you want to check. Reading it in 3 seconds…")
        testTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(3))
            guard !Task.isCancelled, let self else { return }
            let app = NSWorkspace.shared.frontmostApplication
            let id = app?.bundleIdentifier
            guard id != Bundle.main.bundleIdentifier else {
                self.form?.appStyles.showTestResult("Chirp itself was frontmost. Press Test, then switch to another app within 3 seconds.")
                return
            }
            let state = self.state
            let resolved = settings.cleanupStyle(frontmostBundleID: id, available: state.serverOffersStyles ? state.profiles : nil)
            let name = state.profiles.first { $0.id == resolved.profile }?.name ?? CorrectionProfile.displayName(for: resolved.profile)
            let appName = id.map { AppNames.name(for: $0) } ?? app?.localizedName
            self.form?.appStyles.showTestResult(CleanupStyle.testSummary(appName: appName, resolved: resolved, styleName: name))
        }
    }

    @objc private func toggleShowChanges(_ sender: NSButton) {
        settings.showChangesAfterCleanup = sender.state == .on
    }

    @objc private func toggleRawFirst(_ sender: NSButton) {
        settings.insertRawFirst = sender.state == .on
    }

    /// "+": the running apps that have no rule yet, then a file chooser for any other app.
    private func showAppChooser(from button: NSView) {
        let menu = NSMenu()
        let have = Set(settings.appStyles.map(\.bundleID))
        let running = NSWorkspace.shared.runningApplications
            .filter { $0.activationPolicy == .regular && $0.bundleIdentifier != nil && $0.bundleIdentifier != Bundle.main.bundleIdentifier }
            .filter { !have.contains($0.bundleIdentifier!) }
            .sorted { ($0.localizedName ?? "").localizedCaseInsensitiveCompare($1.localizedName ?? "") == .orderedAscending }
        for app in running {
            let item = NSMenuItem(title: app.localizedName ?? app.bundleIdentifier!, action: #selector(addApp(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = app.bundleIdentifier
            item.image = app.icon.map { icon in
                let small = icon.copy() as! NSImage
                small.size = NSSize(width: 16, height: 16)
                return small
            }
            menu.addItem(item)
        }
        if !running.isEmpty { menu.addItem(.separator()) }
        let choose = NSMenuItem(title: "Choose App…", action: #selector(chooseApp), keyEquivalent: "")
        choose.target = self
        menu.addItem(choose)
        menu.popUp(positioning: nil, at: NSPoint(x: 0, y: button.bounds.height + 2), in: button)
    }

    @objc private func addApp(_ sender: NSMenuItem) {
        if let id = sender.representedObject as? String { form?.appStyles.add(bundleID: id) }
    }

    @objc private func chooseApp() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.application]
        panel.directoryURL = URL(fileURLWithPath: "/Applications")
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        guard panel.runModal() == .OK, let url = panel.url, let id = Bundle(url: url)?.bundleIdentifier else { return }
        form?.appStyles.add(bundleID: id)
    }

    @objc private func toggleSounds(_ sender: NSButton) {
        settings.soundsEnabled = sender.state == .on
    }

    // MARK: sounds

    @objc private func packChanged(_ sender: NSPopUpButton) {
        let pack = SoundPack.resolve(id: sender.selectedItem?.representedObject as? String)
        let previous = settings.soundPack
        settings.soundPack = pack
        form?.showPack(pack)
        form?.setCustomSounds(start: settings.customStartSound, stop: settings.customStopSound)
        tabSizeChanged()
        // "Custom…" with nothing chosen yet goes straight to the file chooser; giving up keeps the old pack.
        if pack == .custom, settings.customStartSound == nil, settings.customStopSound == nil {
            if !importSound(for: .start) {
                settings.soundPack = previous
                form?.showPack(previous)
                tabSizeChanged()
            }
        }
        Sounds.prepare()
    }

    @objc private func volumeChanged(_ sender: NSSlider) {
        let fraction = sender.doubleValue / 100
        settings.soundVolume = fraction
        form?.setVolume(fraction)
        // Let go of the slider and hear the level (not on every tick of the drag).
        if NSApp.currentEvent?.type == .leftMouseUp { Sounds.play(.start) }
    }

    @objc private func previewStart() { Sounds.prepare(); Sounds.play(.start) }
    @objc private func previewStop() { Sounds.prepare(); Sounds.play(.stop) }
    @objc private func chooseStart() { importSound(for: .start) }
    @objc private func chooseStop() { importSound(for: .stop) }
    @objc private func removeStart() { removeSound(for: .start) }
    @objc private func removeStop() { removeSound(for: .stop) }

    /// Asks for an audio file, copies it into Application Support and remembers the copy. Returns false when the user
    /// cancelled or the file could not be used (the reason shows under the row).
    @discardableResult
    private func importSound(for cue: SoundCue) -> Bool {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.audio]   // aiff, wav, caf, m4a, mp3, …
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.message = cue == .start ? "Choose the sound that plays when dictation starts."
                                      : "Choose the sound that plays when dictation stops."
        panel.prompt = "Choose"
        guard panel.runModal() == .OK, let url = panel.url else { return false }
        do {
            let imported = try Sounds.customStore.importFile(from: url, cue: cue, replacing: settings.customSound(for: cue))
            if cue == .start { settings.customStartSound = imported } else { settings.customStopSound = imported }
            form?.setCustomSounds(start: settings.customStartSound, stop: settings.customStopSound)
            Sounds.prepare()
            Sounds.play(cue)
            return true
        } catch {
            form?.setCustomSounds(start: settings.customStartSound, stop: settings.customStopSound, error: error.localizedDescription)
            return false
        }
    }

    private func removeSound(for cue: SoundCue) {
        Sounds.customStore.remove(settings.customSound(for: cue))
        if cue == .start { settings.customStartSound = nil } else { settings.customStopSound = nil }
        form?.setCustomSounds(start: settings.customStartSound, stop: settings.customStopSound)
        Sounds.prepare()
    }

    @objc private func toggleLaunch(_ sender: NSButton) {
        let error = LaunchAtLogin.toggle()
        sender.state = LaunchAtLogin.state == .on ? .on : .off
        form?.setLaunchError(error)
    }
    @objc private func grantMic() {
        Task {
            if Permissions.microphone == .notDetermined { _ = await Permissions.requestMicrophone() }
            else { Permissions.openSettings(.microphone) }
        }
    }
    @objc private func grantAX() {
        Permissions.promptAccessibility()
        Permissions.openSettings(.accessibility)
    }
}
