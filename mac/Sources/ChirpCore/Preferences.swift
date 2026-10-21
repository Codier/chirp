import Foundation

/// The only persistent client state besides the token file: plain UserDefaults preferences. The defaults are
/// injectable so tests use a throwaway suite.
public final class Preferences: @unchecked Sendable {
    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        migrateLegacyInstructions()
    }

    public var serverURL: String {
        get { defaults.string(forKey: "serverURL") ?? ServerConfig.defaultURL.absoluteString }
        set { defaults.set(newValue, forKey: "serverURL") }
    }
    #if os(macOS)
    /// One of `KeyCombo.presets` (⌃⌥Space by default) or a recorded shortcut that passed `HotKeyValidation`.
    public var hotkey: KeyCombo {
        get { KeyCombo.resolve(id: defaults.string(forKey: "hotkey")) }
        set { defaults.set(newValue.id, forKey: "hotkey") }
    }
    #endif
    /// Auto-stop (the default), hold to talk, or toggle. An unknown stored value falls back to auto-stop.
    public var activationMode: ActivationMode {
        get { defaults.string(forKey: "activationMode").flatMap(ActivationMode.init(rawValue:)) ?? .autoStop }
        set { defaults.set(newValue.rawValue, forKey: "activationMode") }
    }
    /// Auto-stop: seconds of quiet after speech that end a dictation (one of `AutoStopDetector.silenceChoices`).
    public var autoStopSilence: TimeInterval {
        get {
            guard let v = defaults.object(forKey: "autoStopSilence") as? Double, v.isFinite else { return AutoStopDetector.defaultSilence }
            return min(max(v, 0.5), 10)
        }
        set { defaults.set(newValue.isFinite ? min(max(newValue, 0.5), 10) : AutoStopDetector.defaultSilence, forKey: "autoStopSilence") }
    }
    /// Clean-up (the server's `correction` flag). Off by default.
    public var correction: Bool {
        get { defaults.bool(forKey: "correction") }
        set { defaults.set(newValue, forKey: "correction") }
    }
    /// Whether the HUD offers "Use raw" after a cleaned-up insertion. On by default.
    public var showRawAfterCleanup: Bool {
        get { defaults.object(forKey: "showRawAfterCleanup") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "showRawAfterCleanup") }
    }
    /// Insert the raw transcript at once and swap in the cleaned text when it arrives (only for a verified AX
    /// insertion; otherwise the raw text stays). Off by default: the HUD shows the transition and inserts once.
    public var insertRawFirst: Bool {
        get { defaults.bool(forKey: "insertRawFirst") }
        set { defaults.set(newValue, forKey: "insertRawFirst") }
    }
    /// Show what clean-up changed (the diff highlight) and keep "Use raw" in the HUD after a cleaned insertion. Off by
    /// default: the cleaned text is inserted at once, the HUD fades away, and "Use raw" lives on ⌃⌥Z and in the menu.
    public var showChangesAfterCleanup: Bool {
        get { defaults.bool(forKey: "showChangesAfterCleanup") }
        set { defaults.set(newValue, forKey: "showChangesAfterCleanup") }
    }
    /// The HUD's word waveform: each word's audio drawn above it while dictating. Off by default; on asks the server
    /// for word timings.
    public var wordWaveform: Bool {
        get { defaults.bool(forKey: "wordWaveform") }
        set { defaults.set(newValue, forKey: "wordWaveform") }
    }
    /// The default clean-up style (a `correction_profiles` id).
    public var correctionProfile: String {
        get { defaults.string(forKey: "correctionProfile") ?? CorrectionProfile.defaultID }
        set { defaults.set(newValue, forKey: "correctionProfile") }
    }
    /// The prompts the user edited, by style id. A style is in here only while its prompt differs from the server's
    /// default; the others follow the server. Never logged.
    public var correctionPrompts: [String: String] {
        get { defaults.dictionary(forKey: "correctionPrompts") as? [String: String] ?? [:] }
        set { defaults.set(newValue, forKey: "correctionPrompts") }
    }
    /// The styles with their default prompts as last fetched from the server, for showing them when it is unreachable.
    public var cachedProfiles: [CorrectionProfile]? {
        get { defaults.data(forKey: "cachedProfiles").flatMap { try? JSONDecoder().decode([CorrectionProfile].self, from: $0) } }
        set { defaults.set(newValue.flatMap { try? JSONEncoder().encode($0) }, forKey: "cachedProfiles") }
    }
    public func isPromptEdited(_ id: String) -> Bool { correctionPrompts[id] != nil }

    /// Saves what the user typed for style `id`: kept as an edit, or dropped when it is blank or the style's default.
    /// Returns whether the style is edited afterwards. Edits need the live server's default and limit; otherwise nothing changes.
    @discardableResult
    public func setPrompt(_ text: String, for id: String, in catalog: StyleCatalog) -> Bool {
        guard catalog.canEdit else { return isPromptEdited(id) }
        var prompts = correctionPrompts
        if let kept = StylePrompts.override(for: text, default: catalog.defaultPrompt(for: id), limit: catalog.limit(for: id)) {
            prompts[id] = kept
        } else {
            prompts[id] = nil
        }
        correctionPrompts = prompts
        return prompts[id] != nil
    }

    /// Back to the server's prompt for style `id`.
    public func resetPrompt(for id: String) {
        var prompts = correctionPrompts
        prompts[id] = nil
        correctionPrompts = prompts
    }

    /// Before editable prompts, Custom's text lived in `correctionInstructions`; it is now the Custom style's prompt.
    /// Moved once: an existing Custom prompt is kept, and the old key is removed either way.
    private func migrateLegacyInstructions() {
        guard let legacy = defaults.string(forKey: "correctionInstructions") else { return }
        defaults.removeObject(forKey: "correctionInstructions")
        var prompts = correctionPrompts
        guard prompts[CorrectionProfile.customID] == nil else { return }
        let text = (CorrectionProfile.clamp(legacy) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        prompts[CorrectionProfile.customID] = text
        correctionPrompts = prompts
    }
    /// Frontmost app → style. A fresh install has `AppStyleRule.defaults`; once edited the stored list is the truth,
    /// including an empty one.
    public var appStyles: [AppStyleRule] {
        get {
            guard let data = defaults.data(forKey: "appStyles"),
                  let rules = try? JSONDecoder().decode([AppStyleRule].self, from: data) else { return AppStyleRule.defaults }
            return rules
        }
        set {
            var seen = Set<String>()
            let unique = newValue.filter { !$0.bundleID.isEmpty && seen.insert($0.bundleID).inserted }
            defaults.set(try? JSONEncoder().encode(unique), forKey: "appStyles")
        }
    }
    /// UID of the input device the user picked; nil follows the system default input.
    public var inputDeviceUID: String? {
        get { defaults.string(forKey: "inputDeviceUID") }
        set { defaults.set(newValue, forKey: "inputDeviceUID") }
    }
    /// Quiet cues when dictation starts and stops. Off by default.
    public var soundsEnabled: Bool {
        get { defaults.bool(forKey: "soundsEnabled") }
        set { defaults.set(newValue, forKey: "soundsEnabled") }
    }
    /// Which pack plays (Chirp by default; an unknown stored value falls back to it).
    public var soundPack: SoundPack {
        get { SoundPack.resolve(id: defaults.string(forKey: "soundPack")) }
        set { defaults.set(newValue.rawValue, forKey: "soundPack") }
    }
    /// The cue volume, 0…1 (60 % by default). `SoundTiming.gain(volume:)` maps it to a gain.
    public var soundVolume: Double {
        get {
            guard let v = defaults.object(forKey: "soundVolume") as? Double, v.isFinite else { return SoundTiming.defaultVolume }
            return min(max(v, 0), 1)
        }
        set { defaults.set(newValue.isFinite ? min(max(newValue, 0), 1) : SoundTiming.defaultVolume, forKey: "soundVolume") }
    }
    /// The user's own start and stop files (copies in Application Support), used by the Custom pack.
    public var customStartSound: CustomSound? {
        get { customSound(forKey: "customStartSound") }
        set { setCustomSound(newValue, forKey: "customStartSound") }
    }
    public var customStopSound: CustomSound? {
        get { customSound(forKey: "customStopSound") }
        set { setCustomSound(newValue, forKey: "customStopSound") }
    }
    public func customSound(for cue: SoundCue) -> CustomSound? { cue == .start ? customStartSound : customStopSound }

    private func customSound(forKey key: String) -> CustomSound? {
        defaults.data(forKey: key).flatMap { try? JSONDecoder().decode(CustomSound.self, from: $0) }
    }
    private func setCustomSound(_ sound: CustomSound?, forKey key: String) {
        if let sound, let data = try? JSONEncoder().encode(sound) { defaults.set(data, forKey: key) } else { defaults.removeObject(forKey: key) }
    }
    /// Live Notes' "Structure notes" toggle; nil until the user flips it, so the default can follow the server
    /// (on for a local model, off when correction leaves the Mac). A preference, never notes content.
    public var notesStructure: Bool? {
        get { defaults.object(forKey: "notesStructure") as? Bool }
        set { defaults.set(newValue, forKey: "notesStructure") }
    }
    /// Where `notesStructure` was chosen: `local`, or `remote:<host>` when correction left the Mac. A choice only
    /// applies to the same place, so turning it on for this Mac never carries over to another host.
    public var notesStructureScope: String? {
        get { defaults.string(forKey: "notesStructureScope") }
        set { defaults.set(newValue, forKey: "notesStructureScope") }
    }
    public static func notesStructureScope(for health: Health) -> String {
        health.correctionLeavesMachine == true ? "remote:\(health.correctionHost ?? "unknown")" : "local"
    }
    /// The user flipped "Structure notes" against a server with this health (ignored while health is unknown).
    public func setNotesStructure(_ on: Bool, health: Health?) {
        guard let health else { return }
        notesStructure = on
        notesStructureScope = Self.notesStructureScope(for: health)
    }
    /// The Notes window's layout (`notes`, `transcript` or `split`).
    public var notesTab: String {
        get { defaults.string(forKey: "notesTab") ?? "notes" }
        set { defaults.set(newValue, forKey: "notesTab") }
    }
    /// Live Notes' "Auto-reorganize" toggle (on by default). A preference, never notes content.
    public var notesAutoReorganize: Bool {
        get { defaults.object(forKey: "notesAutoReorganize") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "notesAutoReorganize") }
    }
    /// The Notes pane shows the read-only Preview instead of the editor.
    public var notesPreview: Bool {
        get { defaults.bool(forKey: "notesPreview") }
        set { defaults.set(newValue, forKey: "notesPreview") }
    }
    /// Whether structuring is on: the user's choice for this server's scope, else on only when correction stays on
    /// this Mac. Unknown health is always off, since nothing says where the text would go.
    public func notesStructureEnabled(health: Health?) -> Bool {
        guard let health else { return false }
        // A choice saved before scoping was made against a local model.
        if let choice = notesStructure, (notesStructureScope ?? "local") == Self.notesStructureScope(for: health) { return choice }
        return health.correctionLeavesMachine != true
    }
    /// The `start` options for a Live Notes segment: the clean-up toggle (when the server offers it) with the server's
    /// hidden `live_notes` style, whose prompt is tuned to its guard, so no edited prompt goes along. An older server
    /// gets the default style with the user's edited default prompt. Per-app styles never apply; word timings only
    /// with `speakerLabels` (they place the words on the speaker timeline).
    public func notesStartOptions(health: Health?, speakerLabels: Bool = false) -> StartOptions {
        let enabled = correction && (health?.correctionAvailable ?? false)
        if health?.notesCorrection == true {
            return StartOptions(client: "mac", correction: enabled, correctionProfile: CorrectionProfile.liveNotesID,
                                wordTimings: speakerLabels)
        }
        return StartOptions(client: "mac", correction: enabled, correctionProfile: CorrectionProfile.defaultID,
                            correctionPrompt: correctionPrompts[CorrectionProfile.defaultID], wordTimings: speakerLabels)
    }
    /// Live Notes' "Label speakers" toggle: off until the user turns it on and the on-device model is downloaded.
    /// A preference only; no label, name or voice is ever stored.
    public var notesSpeakerLabels: Bool {
        get { defaults.bool(forKey: "notesSpeakerLabels") }
        set { defaults.set(newValue, forKey: "notesSpeakerLabels") }
    }
    /// Set only when the first-run window is finished (never on close), so an interrupted setup shows again.
    public var didOnboard: Bool {
        get { defaults.bool(forKey: "didOnboard") }
        set { defaults.set(newValue, forKey: "didOnboard") }
    }

    public func serverConfig() -> ServerConfig { ServerConfig.load(urlString: serverURL) }

    /// The style for a dictation that starts in `bundleID`'s app. `available` is the server's list, when known.
    public func cleanupStyle(frontmostBundleID bundleID: String?, available: [CorrectionProfile]? = nil) -> CleanupStyle.Resolved {
        CleanupStyle.resolve(bundleID: bundleID, rules: appStyles, defaultProfile: correctionProfile,
                             prompts: correctionPrompts, available: available)
    }

    /// What the next `start` message asks for: the clean-up toggle is the wire `correction` flag, and the style comes
    /// from the frontmost app's rule or the default style, and the prompt goes along only when the user edited that style.
    public func startOptions(frontmostBundleID bundleID: String? = nil, available: [CorrectionProfile]? = nil) -> StartOptions {
        let style = cleanupStyle(frontmostBundleID: bundleID, available: available)
        return StartOptions(client: "mac", correction: correction, correctionProfile: style.profile,
                            correctionInstructions: style.instructions, correctionPrompt: style.prompt,
                            wordTimings: wordWaveform)
    }
}
