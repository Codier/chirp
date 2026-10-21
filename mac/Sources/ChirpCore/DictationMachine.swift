import Foundation

public enum DictationState: Sendable, Equatable {
    case idle, connecting, listening, finalizing
    /// The server is cleaning up the transcript (it sent `correcting`); the raw text is on screen as "Polishing…".
    case correcting
    case inserting
    case error(String)

    public var isActive: Bool {
        switch self {
        case .connecting, .listening, .finalizing, .correcting, .inserting: true
        case .idle, .error: false
        }
    }
}

/// How the dictation hotkey works.
public enum ActivationMode: String, CaseIterable, Sendable {
    /// Press once and talk; the dictation ends when you pause (`AutoStopDetector`), or at the next press.
    case autoStop = "auto-stop"
    /// Talk while the hotkey is held; letting go finishes.
    case holdToTalk = "hold"
    /// Press once to start and again to finish, without stopping on pauses.
    case toggle = "toggle"

    public var title: String {
        switch self {
        case .autoStop: "Auto-stop"
        case .holdToTalk: "Hold to talk"
        case .toggle: "Toggle recording"
        }
    }

    /// One sentence for Settings.
    public var help: String {
        switch self {
        case .autoStop: "Press once and talk. Chirp stops when you pause, or when you press again."
        case .holdToTalk: "Hold the hotkey while you talk; let go to finish."
        case .toggle: "Press the hotkey once to start recording and again to stop. Pauses do not stop recording."
        }
    }
}

public enum DictationEvent: Sendable, Equatable {
    case hotkeyDown(at: TimeInterval)
    case hotkeyUp(at: TimeInterval)
    /// The hotkey, a modifier on its own, was part of another shortcut (⌥←): a dictation it just started is dropped.
    case hotkeyChord
    /// "Start/Stop Dictation" in the menu: starts a dictation, or finishes the one under way, in any mode.
    case toggle
    /// Auto-stop: the speaker paused long enough, or never spoke.
    case silence
    case escape
    case serverReady
    /// The server started clean-up on the final transcript (`correcting`).
    case correctingReceived
    case finalReceived
    /// Clean-up did not deliver: the client timeout elapsed, or the server errored or the socket closed while it
    /// ran. The raw transcript is inserted instead.
    case correctionFailed
    case insertionFinished
    /// Server error, lost connection, mic failure, finalize timeout, no signal.
    case failed(String)
}

public enum DictationEffect: Sendable, Equatable {
    /// Start capturing and open the socket.
    case start
    /// Stop capturing and ask the server for the final transcript.
    case finalize
    /// Stop capturing, tell the server to discard, insert nothing.
    case cancel
    /// Release everything after a failure and show the message.
    case abort(String)
    case insert
}

/// Pure state machine: idle → connecting → listening → finalizing → [correcting →] inserting → idle (+ error).
/// Toggle: a press starts, the next press finishes. Auto-stop also finishes on `silence`. Hold to talk: the release
/// finishes; a press shorter than `tapThreshold` was not meant as dictation and is dropped with a hint.
public struct DictationMachine: Sendable {
    public static let tapThreshold: TimeInterval = 0.3
    public static let holdHint = "Hold the hotkey while you talk"

    public var mode: ActivationMode
    public private(set) var state: DictationState = .idle
    /// Set while the activating key is still down.
    private var pressStart: TimeInterval?

    public init(mode: ActivationMode = .autoStop) { self.mode = mode }

    @discardableResult
    public mutating func handle(_ event: DictationEvent) -> [DictationEffect] {
        switch (state, event) {
        case (.idle, .hotkeyDown(let t)), (.error, .hotkeyDown(let t)):
            state = .connecting
            pressStart = t
            return [.start]

        case (.idle, .toggle), (.error, .toggle):
            state = .connecting
            pressStart = nil
            return [.start]

        case (.connecting, .hotkeyDown), (.listening, .hotkeyDown):
            // Already recording. A new press while the first is still down is key repeat; otherwise it stops.
            guard pressStart == nil else { return [] }
            state = .finalizing
            return [.finalize]

        case (.connecting, .toggle), (.listening, .toggle):
            pressStart = nil
            state = .finalizing
            return [.finalize]

        case (.connecting, .hotkeyUp(let t)), (.listening, .hotkeyUp(let t)):
            guard let start = pressStart else { return [] }
            pressStart = nil
            guard mode == .holdToTalk else { return [] }      // auto-stop and toggle ignore releases
            if t - start < Self.tapThreshold {
                state = .error(Self.holdHint)
                return [.abort(Self.holdHint)]
            }
            state = .finalizing
            return [.finalize]

        case (.connecting, .hotkeyChord), (.listening, .hotkeyChord):
            guard pressStart != nil else { return [] }
            pressStart = nil
            state = .idle
            return [.cancel]

        case (.listening, .silence):
            guard mode == .autoStop else { return [] }
            state = .finalizing
            return [.finalize]

        case (.connecting, .serverReady):
            state = .listening
            return []

        case (.connecting, .escape), (.listening, .escape), (.finalizing, .escape), (.correcting, .escape):
            pressStart = nil
            state = .idle
            return [.cancel]

        case (.finalizing, .correctingReceived):
            state = .correcting
            return []

        case (.finalizing, .finalReceived), (.correcting, .finalReceived), (.correcting, .correctionFailed):
            state = .inserting
            return [.insert]

        case (.inserting, .insertionFinished):
            state = .idle
            return []

        case (.connecting, .failed(let m)), (.listening, .failed(let m)), (.finalizing, .failed(let m)),
             (.correcting, .failed(let m)):
            pressStart = nil
            state = .error(m)
            return [.abort(m)]

        case (.inserting, .failed(let m)):
            state = .error(m)
            return []

        default:
            return []
        }
    }

    /// Dismiss an error banner without starting a new session.
    public mutating func dismissError() {
        if case .error = state { state = .idle }
    }
}
