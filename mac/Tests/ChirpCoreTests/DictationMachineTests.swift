import Testing
@testable import ChirpCore

@Suite struct DictationMachineTests {
    @Test(arguments: [ActivationMode.autoStop, .toggle])
    func tapTogglesOnAndOff(mode: ActivationMode) {
        var m = DictationMachine(mode: mode)
        #expect(m.handle(.hotkeyDown(at: 0)) == [.start])
        #expect(m.state == .connecting)
        #expect(m.handle(.hotkeyUp(at: 0.1)) == [])          // short press: keep recording
        #expect(m.state == .connecting)
        m.handle(.serverReady)
        #expect(m.state == .listening)
        #expect(m.handle(.hotkeyDown(at: 3)) == [.finalize])  // second tap stops
        #expect(m.state == .finalizing)
        #expect(m.handle(.hotkeyUp(at: 3.05)) == [])          // its release is ignored
        #expect(m.state == .finalizing)
    }

    @Test func holdIsPushToTalk() {
        var m = DictationMachine(mode: .holdToTalk)
        m.handle(.hotkeyDown(at: 10))
        m.handle(.serverReady)
        #expect(m.handle(.hotkeyUp(at: 11.2)) == [.finalize])
        #expect(m.state == .finalizing)
    }

    @Test func autoStopIgnoresTheReleaseEvenAfterALongHold() {
        var m = DictationMachine(mode: .autoStop)
        m.handle(.hotkeyDown(at: 10))
        m.handle(.serverReady)
        #expect(m.handle(.hotkeyUp(at: 12)) == [])
        #expect(m.state == .listening)
    }

    @Test func toggleIgnoresRepeatAndReleaseAndCanStopBeforeServerReady() {
        var m = DictationMachine(mode: .toggle)
        #expect(m.handle(.hotkeyDown(at: 0)) == [.start])
        #expect(m.handle(.hotkeyDown(at: 0.05)) == [])
        #expect(m.handle(.hotkeyUp(at: 2)) == [] && m.state == .connecting)
        #expect(m.handle(.hotkeyDown(at: 3)) == [.finalize])
        #expect(m.handle(.serverReady) == [] && m.state == .finalizing)
        #expect(m.handle(.finalReceived) == [.insert])
    }

    @Test func toggleStillAbortsOnMicrophoneOrConnectionFailure() {
        var m = DictationMachine(mode: .toggle)
        m.handle(.hotkeyDown(at: 0)); m.handle(.hotkeyUp(at: 0.1)); m.handle(.serverReady)
        #expect(m.handle(.failed("No microphone signal")) == [.abort("No microphone signal")])
        #expect(m.handle(.hotkeyDown(at: 5)) == [.start])
    }

    @Test func holdReleasedBeforeServerReadyStillFinalizes() {
        var m = DictationMachine(mode: .holdToTalk)
        m.handle(.hotkeyDown(at: 0))
        #expect(m.handle(.hotkeyUp(at: 2)) == [.finalize])
        #expect(m.state == .finalizing)
    }

    /// Hold to talk: a quick tap was not meant as a dictation; it is dropped with a hint, and the next press works.
    @Test func thresholdBoundary() {
        var tap = DictationMachine(mode: .holdToTalk)
        tap.handle(.hotkeyDown(at: 0))
        #expect(tap.handle(.hotkeyUp(at: 0.299)) == [.abort(DictationMachine.holdHint)])
        #expect(tap.state == .error(DictationMachine.holdHint))
        #expect(tap.handle(.hotkeyDown(at: 1)) == [.start])
        var hold = DictationMachine(mode: .holdToTalk)
        hold.handle(.hotkeyDown(at: 0))
        #expect(hold.handle(.hotkeyUp(at: 0.3)) == [.finalize])
    }

    @Test func silenceStopsOnlyAnAutoStopDictationThatIsListening() {
        var auto = DictationMachine(mode: .autoStop)
        auto.handle(.hotkeyDown(at: 0)); auto.handle(.hotkeyUp(at: 0.1))
        #expect(auto.handle(.silence) == [])          // still connecting
        auto.handle(.serverReady)
        #expect(auto.handle(.silence) == [.finalize])
        #expect(auto.state == .finalizing)
        var hold = DictationMachine(mode: .holdToTalk)
        hold.handle(.hotkeyDown(at: 0)); hold.handle(.serverReady)
        #expect(hold.handle(.silence) == [])
        #expect(hold.state == .listening)
    }

    /// ⌥ as the hotkey, then ⌥← : the dictation that press started is dropped without inserting anything.
    @Test func aChordCancelsTheDictationItsPressStarted() {
        var m = DictationMachine(mode: .holdToTalk)
        m.handle(.hotkeyDown(at: 0)); m.handle(.serverReady)
        #expect(m.handle(.hotkeyChord) == [.cancel])
        #expect(m.state == .idle)
        // A chord after the press was released (toggle-started dictation) leaves it running.
        var auto = DictationMachine(mode: .autoStop)
        auto.handle(.hotkeyDown(at: 0)); auto.handle(.hotkeyUp(at: 0.1))
        #expect(auto.handle(.hotkeyChord) == [])
        #expect(auto.state == .connecting)
    }

    /// The menu's Start/Stop works the same in all modes and never counts as a too-short hold.
    @Test func menuToggleStartsAndStopsInAllModes() {
        for mode in ActivationMode.allCases {
            var m = DictationMachine(mode: mode)
            #expect(m.handle(.toggle) == [.start])
            m.handle(.serverReady)
            #expect(m.handle(.toggle) == [.finalize])
            // Hold to talk: a press during a menu-started dictation finishes it.
            var n = DictationMachine(mode: mode)
            n.handle(.toggle)
            #expect(n.handle(.hotkeyDown(at: 5)) == [.finalize])
        }
    }

    @Test func keyRepeatWhileHeldIsIgnored() {
        var m = DictationMachine()
        m.handle(.hotkeyDown(at: 0))
        #expect(m.handle(.hotkeyDown(at: 0.05)) == [])
        #expect(m.state == .connecting)
    }

    @Test func escapeCancelsInEveryActiveState() {
        for setup in [[], [DictationEvent.serverReady]] {
            var m = DictationMachine()
            m.handle(.hotkeyDown(at: 0))
            m.handle(.hotkeyUp(at: 0.1))
            for e in setup { m.handle(e) }
            #expect(m.handle(.escape) == [.cancel])
            #expect(m.state == .idle)
        }
        var m = DictationMachine()
        m.handle(.hotkeyDown(at: 0)); m.handle(.hotkeyUp(at: 0.1)); m.handle(.serverReady)
        m.handle(.hotkeyDown(at: 1))
        #expect(m.handle(.escape) == [.cancel])      // while finalizing: nothing gets inserted
        #expect(m.handle(.finalReceived) == [])
        #expect(m.state == .idle)
    }

    @Test func escapeWhenIdleDoesNothing() {
        var m = DictationMachine()
        #expect(m.handle(.escape) == [])
        #expect(m.state == .idle)
    }

    @Test func fullHappyPathReturnsToIdle() {
        var m = DictationMachine()
        m.handle(.hotkeyDown(at: 0)); m.handle(.hotkeyUp(at: 0.1)); m.handle(.serverReady)
        m.handle(.hotkeyDown(at: 2))
        #expect(m.handle(.finalReceived) == [.insert])
        #expect(m.state == .inserting)
        m.handle(.insertionFinished)
        #expect(m.state == .idle)
    }

    @Test func failureAbortsAndNextPressRecovers() {
        var m = DictationMachine()
        m.handle(.hotkeyDown(at: 0)); m.handle(.hotkeyUp(at: 0.1))
        #expect(m.handle(.failed("Server unreachable")) == [.abort("Server unreachable")])
        #expect(m.state == .error("Server unreachable"))
        #expect(m.handle(.hotkeyDown(at: 5)) == [.start])
        #expect(m.state == .connecting)
    }

    @Test func staleEventsAreIgnored() {
        var m = DictationMachine()
        #expect(m.handle(.finalReceived) == [])
        #expect(m.handle(.serverReady) == [])
        #expect(m.handle(.hotkeyUp(at: 1)) == [])
        #expect(m.state == .idle)
    }

    @Test func hotkeyIgnoredWhileInserting() {
        var m = DictationMachine()
        m.handle(.hotkeyDown(at: 0)); m.handle(.hotkeyUp(at: 0.1)); m.handle(.serverReady)
        m.handle(.hotkeyDown(at: 1)); m.handle(.finalReceived)
        #expect(m.handle(.hotkeyDown(at: 2)) == [])
        #expect(m.state == .inserting)
    }
}
