"""Exercise the production Swift configuration and move/rollback code without a GUI."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(shutil.which("swift"), "Swift unavailable")
class NativeTests(unittest.TestCase):
    def test_current_genre_display_and_clearing(self):
        source = Path(__file__).with_name("main.swift").read_text().split("let app = NSApplication.shared", 1)[0]
        source = source.replace("final class Sorter:", "class Sorter:")
        harness = r'''
class TestSorter: Sorter {
    var response: [String: Any] = [:]
    var fail = false
    override func helper(_ action: String, file: URL, text: String? = nil) throws -> [String: Any] {
        assert(action == "read-genre" && file.path == "/unused/track.wav")
        if fail { throw NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: "unreadable genre"]) }
        return response
    }
}
let app = NSApplication.shared
let sorter = TestSorter()
sorter.files = [URL(fileURLWithPath: "/unused/track.wav")]
sorter.index = 0
sorter.response = ["genres": ["House", "Deep House"]]
sorter.refreshGenre()
assert(!sorter.genreLabel.isHidden && sorter.genreLabel.stringValue == "Genre: House | Deep House")
assert(sorter.genreLabel.toolTip == sorter.genreLabel.stringValue)
sorter.response = ["genres": ["Breaks", "ID3: House"], "warning": "Conflicting tags"]
sorter.refreshGenre()
assert(sorter.genreLabel.stringValue == "Genre: Breaks | ID3: House")
assert(sorter.genreLabel.toolTip == "Conflicting tags")
sorter.response = ["genres": ["", "  "]]
sorter.refreshGenre()
assert(sorter.genreLabel.isHidden && sorter.genreLabel.stringValue.isEmpty)
sorter.fail = true
sorter.refreshGenre()
assert(!sorter.genreLabel.isHidden && sorter.genreLabel.stringValue == "Genre: unavailable")
assert(sorter.genreLabel.toolTip == "unreadable genre")
sorter.clear()
assert(sorter.genreLabel.isHidden && sorter.genreLabel.stringValue.isEmpty)
assert(sorter.genreLabel.toolTip == nil)
'''
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text(source + harness)
            result = subprocess.run(["swift", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_genre_column_loads_lazily_and_caches(self):
        source = Path(__file__).with_name("main.swift").read_text().split("let app = NSApplication.shared", 1)[0]
        source = source.replace("final class Sorter:", "class Sorter:")
        harness = r'''
class TestSorter: Sorter {
    var reads: [String] = []
    override func helper(_ action: String, file: URL, text: String? = nil) throws -> [String: Any] {
        reads.append(file.lastPathComponent)
        if file.lastPathComponent == "bad.mp3" { throw NSError(domain: "test", code: 1) }
        if file.lastPathComponent == "none.mp3" { return ["genres": [""]] }
        return ["genres": ["House", "Deep House"]]
    }
}
func text(_ view: NSView?) -> String { (view as? NSTextField)?.stringValue ?? "" }
func settle() {
    sorter.genreQueue.sync {}
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.2))
}
let app = NSApplication.shared
let sorter = TestSorter()
let names = ["ok.mp3", "none.mp3", "bad.mp3"]
sorter.files = names.map { URL(fileURLWithPath: "/unused/" + $0) }
let genre = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("genre"))
let track = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("track"))
assert(text(sorter.tableView(sorter.table, viewFor: track, row: 0)) == "ok.mp3")
assert(sorter.reads.isEmpty)
for row in 0..<3 { assert(text(sorter.tableView(sorter.table, viewFor: genre, row: row)) == "…") }
settle()
assert(sorter.genreCache[sorter.files[0]] == "House | Deep House")
assert(sorter.genreCache[sorter.files[1]] == "—")
assert(sorter.genreCache[sorter.files[2]] == "unavailable")
assert(text(sorter.tableView(sorter.table, viewFor: genre, row: 0)) == "House | Deep House")
assert(sorter.reads.count == 3)
sorter.invalidateGenres()
assert(sorter.genreCache.isEmpty)
assert(text(sorter.tableView(sorter.table, viewFor: genre, row: 0)) == "…")
settle()
assert(sorter.reads.count == 4 && sorter.genreCache[sorter.files[0]] == "House | Deep House")
'''
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text(source + harness)
            result = subprocess.run(["swift", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_waveform_levels_loading_and_seek(self):
        import math
        import struct
        import wave
        source = Path(__file__).with_name("main.swift").read_text().split("let app = NSApplication.shared", 1)[0]
        source = source.replace("final class Sorter:", "class Sorter:")
        harness = r'''
let app = NSApplication.shared
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let track = root.appendingPathComponent("breakdown.wav")
// Loud, quiet breakdown, loud: the middle third must read as a clear dip.
let levels = waveformLevels(track, bins: 30)!
assert(levels.count == 30 && levels.max()! == 1)
for bin in [0, 9, 20, 29] { assert(levels[bin] > 0.9, "\(bin): \(levels[bin])") }
for bin in 11...18 { assert(levels[bin] < 0.15, "\(bin): \(levels[bin])") }
assert(waveformLevels(track, cancelled: { true }) == nil)
let bogus = root.appendingPathComponent("bogus.wav")
try Data("not audio".utf8).write(to: bogus)
assert(waveformLevels(bogus) == nil)
func settle(_ sorter: Sorter) {
    sorter.waveformQueue.sync {}
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.1))
}
let sorter = Sorter()
sorter.loadWaveform(track)
settle(sorter)
assert(sorter.waveform.levels.count == 1200 && sorter.waveformCache[track] == sorter.waveform.levels)
// Moving on before the decode lands discards it; nothing stale is shown or cached.
sorter.waveformCache.removeAll()
sorter.loadWaveform(track)
sorter.loadWaveform(nil)
settle(sorter)
assert(sorter.waveform.levels.isEmpty && sorter.waveformCache.isEmpty)
sorter.loadWaveform(track)
settle(sorter)
sorter.waveform.frame = NSRect(x: 0, y: 0, width: 300, height: 56)
assert(sorter.waveform.fraction(at: 150) == 0.5)
assert(sorter.waveform.fraction(at: -20) == 0 && sorter.waveform.fraction(at: 400) == 1)
sorter.player = try AVAudioPlayer(contentsOf: track)
sorter.seek(to: sorter.waveform.fraction(at: 150))
assert(abs(sorter.player!.currentTime - sorter.player!.duration / 2) < 0.01)
assert(abs(sorter.waveform.progress - 0.5) < 0.01)
assert(sorter.waveform.positionDescription == sorter.time.stringValue)
sorter.seek(to: 2)
assert(abs(sorter.waveform.progress - 1) < 0.01)
sorter.clear()
assert(sorter.waveform.levels.isEmpty && sorter.waveform.progress == 0)
// Drawing exercises both the empty line and the bars.
for current in [[], levels] {
    sorter.waveform.levels = current
    let image = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 300, pixelsHigh: 56, bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    sorter.waveform.cacheDisplay(in: sorter.waveform.bounds, to: image)
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rate = 8000
            with wave.open(str(root / "breakdown.wav"), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(rate)
                frames = bytearray()
                for i in range(rate * 3):
                    amplitude = 0.05 if rate <= i < rate * 2 else 0.9
                    sample = int(32767 * amplitude * math.sin(2 * math.pi * 220 * i / rate))
                    frames += struct.pack("<hh", sample, sample)
                output.writeframes(bytes(frames))
            script = root / "main.swift"
            script.write_text(source + harness)
            result = subprocess.run(["swift", str(script), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_targeted_recheck_failure_retains_rows_without_full_scan(self):
        source = Path(__file__).with_name("main.swift").read_text().split("let app = NSApplication.shared", 1)[0]
        source = source.replace("final class Sorter:", "class Sorter:")
        harness = r'''
class TestSorter: Sorter {
    var actions: [String] = []
    var mode = 0
    override func helper(_ action: String, file: URL, text: String? = nil) throws -> [String: Any] {
        actions.append(action)
        if action == "repair-genres" {
            throw NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: "repair interrupted"])
        }
        assert(action == "recheck-genres")
        if mode == 0 { throw NSError(domain: "test", code: 2, userInfo: [NSLocalizedDescriptionKey: "recheck unavailable"]) }
        if mode == 1 { return ["checkedPaths": [], "issues": []] } // Incomplete is not clean.
        return ["checkedPaths": ["/unused/House/a.wav"], "issues": []]
    }
}
let app = NSApplication.shared
let sorter = TestSorter()
sorter.configuration = try JSONDecoder().decode(Configuration.self, from: Data("{\"destinationRoot\":\"/unused\",\"destinations\":[{\"directory\":\"House\",\"genre\":\"House\"}]}".utf8))
sorter.configurationURL = URL(fileURLWithPath: "/unused/config.json")
let controller = LintController(sorter: sorter)
let finding: [String: Any] = ["path": "/unused/House/a.wav", "problem": "Missing Genre", "repairable": true]
for mode in 0...2 {
    sorter.mode = mode
    sorter.actions = []
    controller.issues = [finding]
    controller.table.reloadData()
    controller.startRepair([finding])
    let deadline = Date().addingTimeInterval(10)
    while sorter.repairingGenres && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    assert(!sorter.repairingGenres && controller.scanButton.isEnabled)
    assert(sorter.actions == ["repair-genres", "recheck-genres"])
    assert(controller.repairReport.string.contains("repair interrupted"))
    if mode < 2 {
        assert(controller.issues.count == 1)
        assert(NSDictionary(dictionary: controller.issues[0]).isEqual(to: finding))
        assert(controller.summary.stringValue.contains("Previous rows retained"))
    } else {
        // An interrupted process can still have committed changes; trust a clean recheck.
        assert(controller.issues.isEmpty)
        assert(controller.summary.stringValue.contains("Rechecked 1 attempted files only"))
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text(source + harness)
            result = subprocess.run(["swift", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_activity_and_selection_explanation(self):
        source = Path(__file__).with_name("main.swift").read_text().split("let app = NSApplication.shared", 1)[0]
        source = source.replace("final class Sorter:", "class Sorter:")
        harness = r'''
class TestSorter: Sorter {
    let entered = DispatchSemaphore(value: 0)
    let release = DispatchSemaphore(value: 0)
    var fail = false
    override func helper(_ action: String, file: URL, text: String? = nil) throws -> [String: Any] {
        assert(action == "lint")
        entered.signal()
        release.wait()
        if fail { throw NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: "fixture failure"]) }
        return ["issues": [["problem": "Conflicting WAV RIFF INFO / ID3 Genre (Rekordbox reads IGNR)", "repairable": true], ["problem": "Genre mismatch", "repairable": false]], "scanned": 1, "checkedFolders": 1]
    }
}
let app = NSApplication.shared
let sorter = TestSorter()
sorter.configuration = try JSONDecoder().decode(Configuration.self, from: Data("{\"destinationRoot\":\"/unused\",\"destinations\":[{\"directory\":\"House\",\"genre\":\"House\"}]}".utf8))
let controller = LintController(sorter: sorter)
for fail in [false, true] {
    sorter.fail = fail
    controller.scan()
    assert(controller.scanning && !controller.scanButton.isEnabled && !controller.fixButton.isEnabled)
    assert(!controller.activityIndicator.isDisplayedWhenStopped)
    assert(controller.activityIndicator.accessibilityLabel() == "Checking destination tags")
    assert(controller.repairHint.stringValue.contains("while checking"))
    assert(sorter.entered.wait(timeout: .now() + 5) == .success)
    sorter.release.signal()
    let deadline = Date().addingTimeInterval(10)
    while controller.scanning && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }
    assert(!controller.scanning && controller.scanButton.isEnabled)
    if fail {
        assert(controller.summary.stringValue.contains("Check failed: fixture failure"))
    } else {
        assert(controller.summary.stringValue.contains("1 audio files checked"))
        controller.table.selectRowIndexes(IndexSet(integer: 0), byExtendingSelection: false)
        controller.tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification))
        assert(controller.fixButton.isEnabled && controller.repairHint.stringValue.contains("1 WAV conflicts"))
        controller.table.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
        controller.tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification))
        assert(!controller.fixButton.isEnabled && controller.repairHint.stringValue.contains("report-only"))
        assert(controller.fixButton.toolTip == controller.repairHint.stringValue)
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text(source + harness)
            result = subprocess.run(["swift", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repair_confirmation_selection_background_gate_and_targeted_recheck(self):
        source = Path(__file__).with_name("main.swift").read_text().split("let app = NSApplication.shared", 1)[0]
        source = source.replace("final class Sorter:", "class Sorter:")
        # Exercise the real confirmation construction without displaying a modal UI.
        source = source.replace("guard alert.runModal() == .alertSecondButtonReturn else { return }\n        startRepair(findings)",
                                "guard TestAlerts.confirm(alert) else { return }\n        startRepair(findings)")
        harness = r'''
enum TestAlerts {
    static var accepted = false
    static var latest: NSAlert?
    static func confirm(_ alert: NSAlert) -> Bool { latest = alert; return accepted }
}
class TestSorter: Sorter {
    let entered = DispatchSemaphore(value: 0)
    let release = DispatchSemaphore(value: 0)
    var actions: [String] = []
    var scannedRoot: URL?
    override func helper(_ action: String, file: URL, text: String? = nil) throws -> [String: Any] {
        actions.append(action)
        if action == "repair-genres" {
            let request = try JSONSerialization.jsonObject(with: Data(text!.utf8)) as! [String: Any]
            let findings = request["findings"] as! [[String: Any]]
            assert(findings.count == 2 && findings[1]["expected"] as? String == "House")
            assert(findings[1]["problem"] as? String == "Conflicting WAV RIFF INFO / ID3 Genre (Rekordbox reads IGNR)")
            entered.signal()
            release.wait()
            return ["results": [["path": "fixture.wav", "genre": "House", "status": "success", "detail": "Genre filled"]]]
        }
        if action == "recheck-genres" {
            let request = try JSONSerialization.jsonObject(with: Data(text!.utf8)) as! [String: Any]
            let findings = request["findings"] as! [[String: Any]]
            assert(findings.count == 2)
            var updated = findings[1]
            updated["snapshot"] = ["test": 3]
            updated["actual"] = ["ID3: Changed"]
            return ["checkedPaths": findings.map { $0["path"] as! String }, "issues": [updated]]
        }
        assert(action == "lint")
        scannedRoot = file
        return ["issues": [], "scanned": 1, "checkedFolders": 1, "skippedFolders": 0]
    }
}
let app = NSApplication.shared
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let track = root.appendingPathComponent("fixture.wav")
let sorter = TestSorter()
sorter.configuration = try JSONDecoder().decode(Configuration.self, from: Data("{\"destinationRoot\":\"\(root.path)\",\"destinations\":[{\"directory\":\"House\",\"genre\":\"House\"}]}".utf8))
sorter.configurationURL = root.appendingPathComponent("config.json")
sorter.window = NSWindow()
sorter.files = [track]
sorter.index = 0
sorter.comment.string = "pending comment"
sorter.originalComment = "original"
sorter.commentReadable = true
sorter.player = try AVAudioPlayer(contentsOf: track)
sorter.player!.currentTime = 0.01
let player = sorter.player
sorter.undoMoves = [(track, root.appendingPathComponent("moved.wav"), nil)]
let controller = LintController(sorter: sorter)
let missing: [String: Any] = ["path": track.path, "file": "fixture.wav", "expected": "House", "problem": "Missing Genre", "repairable": true, "snapshot": ["test": 1]]
let conflict: [String: Any] = ["path": root.appendingPathComponent("conflict.wav").path, "actual": ["Breaks", "ID3: Techno"], "expected": "House", "problem": controller.wavGenreConflict, "repairable": true, "snapshot": ["test": 2]]
let untouched: [String: Any] = ["path": root.appendingPathComponent("untouched.wav").path, "problem": "Genre mismatch", "repairable": false, "snapshot": ["test": 99]]
controller.issues = [missing, untouched, ["path": root.appendingPathComponent("absent").path, "problem": "Missing directory", "repairable": false], conflict]
controller.table.reloadData()
controller.table.selectRowIndexes(IndexSet(integer: 1), byExtendingSelection: false)
controller.tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification))
assert(!controller.fixButton.isEnabled)
controller.confirmRepair()
assert(TestAlerts.latest == nil && sorter.actions.isEmpty)
controller.table.selectRowIndexes(IndexSet([0, 1, 2, 3]), byExtendingSelection: false)
controller.tableViewSelectionDidChange(Notification(name: NSTableView.selectionDidChangeNotification))
assert(controller.fixButton.isEnabled && controller.selectedRepairs.count == 2)
assert(controller.repairHint.stringValue.contains("2 selected files eligible (1 WAV conflicts)"))
controller.confirmRepair()
assert(sorter.actions.isEmpty && !sorter.repairingGenres)
let alert = TestAlerts.latest!
assert(alert.buttons[0].title == "Cancel" && alert.buttons[0].keyEquivalent == "\r")
assert(alert.informativeText.contains("no app undo"))
assert(alert.informativeText.contains("BOTH genre tags overwritten"))
assert(alert.buttons[1].title == "Repair Genres" && alert.buttons[1].keyEquivalent == "")
let list = (alert.accessoryView as! NSScrollView).documentView as! NSTextView
assert(list.string.contains(track.path) && list.string.contains("Genre: House"))
assert(list.string.contains("Current: Breaks | ID3: Techno") && list.string.contains("Replace RIFF + ID3 → Genre: House"))
TestAlerts.accepted = true
controller.confirmRepair()
assert(sorter.repairingGenres && !controller.fixButton.isEnabled && !controller.scanButton.isEnabled)
assert(controller.activityIndicator.accessibilityLabel() == "Repairing Genres")
assert(controller.repairHint.stringValue.contains("in progress"))
assert(sorter.entered.wait(timeout: .now() + 5) == .success)
// Every main-app mutation path is gated, even Trash of a noncurrent track.
assert(!sorter.saveComment())
sorter.move(0)
sorter.undoMove()
assert(!sorter.trashFile(track))
let settingsApplied = try sorter.applySettings(sorter.configuration)
assert(!settingsApplied)
assert(sorter.applicationShouldTerminate(app) == .terminateCancel)
assert(sorter.files == [track] && sorter.index == 0 && sorter.undoMoves.count == 1)
assert(sorter.comment.string == "pending comment" && sorter.originalComment == "original")
assert(sorter.player === player && abs(player!.currentTime - 0.01) < 0.01)
controller.confirmRepair() // Cannot launch another repair while busy.
assert(sorter.actions == ["repair-genres"])
sorter.release.signal()
let deadline = Date().addingTimeInterval(10)
while (sorter.repairingGenres || sorter.actions.count < 2 || controller.scanning) && Date() < deadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.01))
}
assert(!sorter.repairingGenres && !controller.scanning)
assert(sorter.actions == ["repair-genres", "recheck-genres"])
assert(sorter.scannedRoot == nil)
assert(controller.repairReport.string.contains("1 success") && controller.repairReport.string.contains("Genre filled"))
assert(controller.issues.count == 3)
assert(NSDictionary(dictionary: controller.issues[0]).isEqual(to: untouched))
assert(controller.issues[2]["actual"] as? [String] == ["ID3: Changed"])
assert(controller.issues[2]["snapshot"] as? [String: Int] == ["test": 3])
assert(controller.table.selectedRowIndexes == IndexSet([0, 1, 2]))
assert(controller.summary.stringValue.contains("Rechecked 2 attempted files only"))
assert(controller.scanButton.isEnabled && controller.fixButton.isEnabled)
assert(sorter.comment.string == "pending comment" && sorter.originalComment == "original")
assert(sorter.player === player && sorter.index == 0 && sorter.files == [track] && sorter.undoMoves.count == 1)
// A manual Check again still checks everything with the latest settings.
sorter.configuration.destinationRoot = root.appendingPathComponent("current-settings").path
controller.scan()
let scanDeadline = Date().addingTimeInterval(10)
while controller.scanning && Date() < scanDeadline {
    RunLoop.current.run(until: Date().addingTimeInterval(0.01))
}
assert(sorter.actions == ["repair-genres", "recheck-genres", "lint"])
assert(sorter.scannedRoot?.path == sorter.configuration.destinationRoot)
assert(controller.issues.isEmpty)
'''
        from test_formats import fixture
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root / "fixture.wav")
            script = root / "main.swift"
            script.write_text(source + harness)
            result = subprocess.run(["swift", str(script), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    def test_discovery_playback_and_refresh_all_formats(self):
        source = Path(__file__).with_name("main.swift").read_text()
        load = "func loadFolder(" + source.split("    func loadFolder(", 1)[1].split("    func numberOfRows", 1)[0]
        select = "func select(" + source.split("    func select(", 1)[1].split("    func highlight()", 1)[0]
        refresh = "func refreshFolder()" + source.split("    @objc func refreshFolder()", 1)[1].split("    func loadFolder(", 1)[0]
        harness = '''
import Foundation
import AVFoundation
enum State { case on, off }
class Toggle { var state = State.off }
class Field { var string = ""; var stringValue = ""; var isEditable = false; var isHidden = false; var toolTip: String? }
class Table {
    func reloadData() {}
    func reloadData(forRowIndexes: IndexSet, columnIndexes: IndexSet) {}
}
class Defaults {
    static let standard = Defaults()
    func set(_ value: Any, forKey: String) {}
}
class Configuration { var startAtThirtyPercent = true }
class Sorter {
    var genreCache: [URL: String] = [:]
    func genreText(_ data: [String: Any]) -> String { "" }
    func invalidateGenres() { genreCache.removeAll() }
    let recursive = Toggle()
    let configuration = Configuration()
    let table = Table()
    let folderLabel = Field(), status = Field(), title = Field(), comment = Field()
    let genreLabel = Field()
    let names = ["House"]
    var source: URL?
    var files: [URL] = []
    var index = -1
    var loading = false, commentReadable = false, saveSucceeds = true
    var originalComment = ""
    var errors = 0
    var player: AVAudioPlayer?
    func saveComment() -> Bool { saveSucceeds }
    func helper(_ action: String, file: URL) throws -> [String: Any] { ["comment": "fixture"] }
    func report(_ error: Error) { errors += 1 }
    func highlight() {}
    func updateTime() {}
    var waveforms: [URL?] = []
    func loadWaveform(_ url: URL?) { waveforms.append(url) }
    func clear() { index = -1; player = nil }
    LOAD
    SELECT
    REFRESH
}
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let sorter = Sorter()
sorter.loadFolder(root, autoplay: false)
assert(sorter.files.count == 4 && sorter.errors == 0)
for i in sorter.files.indices {
    sorter.select(i, autoplay: false)
    assert(sorter.player != nil && sorter.player!.duration > 0)
    assert(!sorter.player!.isPlaying)
    assert(abs(sorter.player!.currentTime - sorter.player!.duration * 0.3) < 0.01)
    assert(sorter.comment.string == "fixture")
    assert(sorter.waveforms.last! == sorter.files[i])
}
let current = sorter.files[sorter.index]
let player = sorter.player
sorter.player!.currentTime = 0.01
sorter.refreshFolder()
assert(sorter.player === player && sorter.files[sorter.index] == current)
assert(abs(sorter.player!.currentTime - 0.01) < 0.01)
sorter.recursive.state = .on
sorter.refreshFolder()
assert(sorter.files.count == 5 && sorter.player === player)
let before = sorter.files
sorter.saveSucceeds = false
try FileManager.default.moveItem(at: current, to: root.appendingPathComponent("renamed." + current.pathExtension))
sorter.refreshFolder()
assert(sorter.files == before && sorter.player === player)
sorter.saveSucceeds = true
sorter.refreshFolder()
assert(!sorter.files.contains(current) && sorter.files.count == 5)
assert(sorter.player !== player && !sorter.player!.isPlaying)
assert(sorter.errors == 0)
'''.replace("LOAD", load.replace("UserDefaults.standard", "Defaults.standard")).replace("SELECT", select).replace("REFRESH", refresh)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for extension in ["mp3", "WAV", "aif", "AIFF"]:
                subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1", "-y", str(root / ("track." + extension))], check=True)
            (root / "nested").mkdir()
            shutil.copy2(root / "track.WAV", root / "nested/extra.wav")
            shutil.copy2(root / "track.WAV", root / ".hidden.wav")
            (root / "unsupported.flac").write_bytes(b"not scanned")
            (root / "unsupported.m4a").write_bytes(b"not scanned")
            (root / "link.wav").symlink_to(root / "track.WAV")
            script = root / "main.swift"
            script.write_text(harness)
            result = subprocess.run(["swift", str(script), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_format_move_tag_undo_and_rollback(self):
        from comments import AudioMetadata, genre_values, read_comment
        from test_formats import fixture, rich_tags
        source = Path(__file__).with_name("main.swift").read_text()
        move = "func moveTaggedFile(" + source.split("    func moveTaggedFile(", 1)[1].split("    func move(_ destination:", 1)[0]
        harness = '''
import Foundation
class Mover {
    var failedRollback = false
    var calls = 0
    func helper(_ action: String, file: URL, text: String?) throws -> [String: Any] {
        calls += 1
        if failedRollback {
            try Data("collision".utf8).write(to: URL(fileURLWithPath: CommandLine.arguments[4]))
            throw NSError(domain: "test", code: 1)
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: CommandLine.arguments[2])
        process.arguments = [CommandLine.arguments[3], action, file.path]
        let input = Pipe(), output = Pipe()
        process.standardInput = input; process.standardOutput = output
        try process.run()
        try input.fileHandleForWriting.write(contentsOf: Data((text ?? "").utf8))
        try input.fileHandleForWriting.close()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus != 0 { throw NSError(domain: "test", code: 2) }
        return try JSONSerialization.jsonObject(with: data) as! [String: Any]
    }
    MOVE
}
let original = URL(fileURLWithPath: CommandLine.arguments[4])
let target = URL(fileURLWithPath: CommandLine.arguments[5])
let mover = Mover()
let restore = try mover.moveTaggedFile(from: original, to: target, change: ["genre": "House"])
assert(restore != nil)
_ = try mover.helper("write", file: target, text: "Comment after move")
_ = try mover.moveTaggedFile(from: target, to: original, change: restore)
let before = try Data(contentsOf: original)
// Real metadata validation failure rolls the move back atomically.
do { _ = try mover.moveTaggedFile(from: original, to: target, change: ["genre": ""]); fatalError("Expected failure") }
catch {}
let afterFailure = try Data(contentsOf: original)
assert(afterFailure == before)
assert(!FileManager.default.fileExists(atPath: target.path))
// Collision prevents tagging, including during undo.
try Data("occupied".utf8).write(to: target)
let calls = mover.calls
do { _ = try mover.moveTaggedFile(from: original, to: target, change: restore); fatalError("Expected collision") }
catch {}
assert(mover.calls == calls)
let afterCollision = try Data(contentsOf: original)
assert(afterCollision == before)
try FileManager.default.removeItem(at: target)
// A rollback collision reports the surviving target instead of hiding it.
mover.failedRollback = true
do { _ = try mover.moveTaggedFile(from: original, to: target, change: ["genre": "House"]); fatalError("Expected failure") }
catch { assert(error.localizedDescription.contains(target.path)) }
let afterRollbackCollision = try Data(contentsOf: target)
assert(afterRollbackCollision == before)
'''.replace("MOVE", move)
        for extension in [".mp3", ".wav", ".aif", ".aiff"]:
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                original, target = root / ("source" + extension), root / ("target" + extension)
                fixture(original, rich_tags())
                before = AudioMetadata(original)
                script = root / "main.swift"
                script.write_text(harness)
                result = subprocess.run(["swift", str(script), directory, sys.executable, str(Path(__file__).with_name("comments.py")), str(original), str(target)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(genre_values(target), ([], None))
                self.assertEqual(read_comment(target), "Comment after move")
                after = AudioMetadata(target)
                if extension == ".mp3":
                    self.assertEqual(after.audio, before.audio)
                else:
                    self.assertEqual([p for p in after.parts if p[0] in (b"data", b"SSND")], [p for p in before.parts if p[0] in (b"data", b"SSND")])

    def test_trash_confirmation_and_context_target(self):
        source = Path(__file__).with_name("main.swift").read_text()
        confirm = source.split("    @objc func confirmTrash(", 1)[1].split("    func sendToTrash(", 1)[0]
        harness = '''
import Foundation
class Menu {}
class NSMenuItem { var menu: Menu?; var representedObject: Any? }
class Table { let menu: Menu? = Menu() }
class Window { var isKeyWindow = true }
class Application { var modalWindow: Window? }
let NSApp = Application()
class Button { var keyEquivalent = ""; var hasDestructiveAction = false }
enum Style { case warning }
enum Response { case alertFirstButtonReturn, alertSecondButtonReturn }
class NSAlert {
    static var response = Response.alertFirstButtonReturn
    static var latest: NSAlert?
    var alertStyle = Style.warning
    var messageText = ""
    var informativeText = ""
    var titles: [String] = []
    var buttons: [Button] = []
    init() { NSAlert.latest = self }
    func addButton(withTitle title: String) -> Button {
        titles.append(title)
        let button = Button()
        buttons.append(button)
        return button
    }
    func runModal() -> Response { NSAlert.response }
}
class Sorter {
    let window = Window()
    let table = Table()
    var files = [URL(fileURLWithPath: "/fixture/current.mp3"), URL(fileURLWithPath: "/fixture/clicked.mp3")]
    var index = 0
    var trashed: [URL] = []
    func trashFile(_ url: URL) -> Bool { trashed.append(url); return true }
    CONFIRM_FUNCTION
}
let sorter = Sorter()
sorter.confirmTrash(nil)
assert(sorter.trashed.isEmpty)
assert(NSAlert.latest?.titles == ["Cancel", "Move to Trash"])
assert(NSAlert.latest?.buttons[0].keyEquivalent == "\\r")
assert(NSAlert.latest?.buttons[1].keyEquivalent == "")
assert(NSAlert.latest?.buttons[1].hasDestructiveAction == true)
NSAlert.response = .alertSecondButtonReturn
sorter.confirmTrash(nil)
assert(sorter.trashed == [sorter.files[0]])
let item = NSMenuItem()
item.menu = sorter.table.menu
item.representedObject = sorter.files[1]
sorter.confirmTrash(item)
assert(sorter.trashed == sorter.files)
assert(NSAlert.latest?.messageText.contains("clicked.mp3") == true)
item.representedObject = nil
sorter.confirmTrash(item)
assert(sorter.trashed.count == 2)
sorter.window.isKeyWindow = false
sorter.confirmTrash(nil)
sorter.window.isKeyWindow = true
NSApp.modalWindow = Window()
sorter.confirmTrash(nil)
assert(sorter.trashed.count == 2)
'''.replace("CONFIRM_FUNCTION", "func confirmTrash(" + confirm)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text(harness)
            result = subprocess.run(["swift", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_trash_queue_updates_and_failures(self):
        source = Path(__file__).with_name("main.swift").read_text()
        trash = source.split("    @discardableResult func trashFile(", 1)[1].split("    // Move first", 1)[0]
        harness = '''
import Foundation
class Field { var string = "unsaved"; var stringValue = "" }
class Table { func reloadData() {} }
class Player {
    var isPlaying = true
    func stop() { isPlaying = false }
}
class Sorter {
    var files: [URL]
    var repairingGenres = false
    var index = 1
    var player: Player? = Player()
    var originalComment = "original"
    let comment = Field()
    let status = Field()
    let table = Table()
    var loading = false
    var saveSucceeds = true
    var trashFails = false
    var saveCalls = 0
    var trashCalls: [URL] = []
    var errors = 0
    var selected: (Int, Bool)?
    var cleared = false
    init(_ urls: [URL]) { files = urls }
    func saveComment() -> Bool { saveCalls += 1; return saveSucceeds }
    func sendToTrash(_ url: URL) throws {
        trashCalls.append(url)
        if trashFails { throw NSError(domain: "test", code: 1) }
    }
    func select(_ position: Int, autoplay: Bool) { index = position; selected = (position, autoplay) }
    func clear() { cleared = true }
    func highlight() {}
    func report(_ error: Error) { errors += 1 }
    TRASH_FUNCTION
}
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let urls = (0..<3).map { root.appendingPathComponent("track \\($0).mp3") }
for url in urls { try Data("audio".utf8).write(to: url) }
// Current track: save first, remove it, and continue an already-playing preview.
let current = Sorter(urls)
assert(current.trashFile(urls[1]))
assert(current.files == [urls[0], urls[2]])
assert(current.saveCalls == 1)
assert(current.trashCalls == [urls[1]])
assert(current.selected?.0 == 1 && current.selected?.1 == true)
assert(current.comment.string.isEmpty)
// Context target before current: preserve playback, current track, and dirty text.
let other = Sorter(urls)
let player = other.player
assert(other.trashFile(urls[0]))
assert(other.index == 0 && other.files[other.index] == urls[1])
assert(other.player === player && other.player?.isPlaying == true)
assert(other.comment.string == "unsaved" && other.saveCalls == 0)
assert(other.selected == nil)
// Last track falls back to previous, without starting paused playback.
let last = Sorter(urls)
last.index = 2
last.player?.isPlaying = false
assert(last.trashFile(urls[2]))
assert(last.selected?.0 == 1 && last.selected?.1 == false)
let only = Sorter([urls[0]])
only.index = 0
assert(only.trashFile(urls[0]))
assert(only.files.isEmpty && only.index == -1 && only.cleared)
// A failed comment save or Trash operation cannot remove a queued track.
let saveFailure = Sorter(urls)
saveFailure.saveSucceeds = false
assert(!saveFailure.trashFile(urls[1]))
assert(saveFailure.trashCalls.isEmpty && saveFailure.files == urls)
assert(saveFailure.comment.string == "unsaved" && saveFailure.player?.isPlaying == true)
let trashFailure = Sorter(urls)
trashFailure.trashFails = true
assert(!trashFailure.trashFile(urls[1]))
assert(trashFailure.errors == 1 && trashFailure.files == urls && trashFailure.index == 1)
assert(trashFailure.player?.isPlaying == true && trashFailure.comment.string == "unsaved")
// Stale, missing, directory, and symlink targets must not reach Trash.
assert(!trashFailure.trashFile(root.appendingPathComponent("not queued.mp3")))
let link = root.appendingPathComponent("link.mp3")
try FileManager.default.createSymbolicLink(at: link, withDestinationURL: urls[0])
for target in [link, root, root.appendingPathComponent("missing.mp3")] {
    let invalid = Sorter([target])
    invalid.index = 0
    assert(!invalid.trashFile(target))
    assert(invalid.trashCalls.isEmpty && invalid.files == [target])
}
// This harness mocks Trash: no files are sent to the real system Trash.
for url in urls { assert(FileManager.default.fileExists(atPath: url.path)) }
'''.replace("TRASH_FUNCTION", "@discardableResult func trashFile(" + trash)
        for extension in [".mp3", ".wav", ".aif", ".aiff"]:
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as directory:
                script = Path(directory) / "main.swift"
                script.write_text(harness.replace(".mp3", extension))
                result = subprocess.run(["swift", str(script), directory], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_command_playback_shortcuts_in_both_focus_states(self):
        source = Path(__file__).with_name("main.swift").read_text()
        command = source.split("        if flags.contains(.command) {", 1)[1].split("            if let number = Int(key)", 1)[0]
        harness = '''
import Cocoa
var offsets: [Double] = []
var selections: [Int] = []
let index = 2
func select(_ newIndex: Int, autoplay: Bool) {
    assert(!autoplay)
    selections.append(newIndex)
}
func scrub(_ seconds: Double) { offsets.append(seconds) }
func handle(_ key: String, flags: NSEvent.ModifierFlags, editing: Bool = true) -> Bool {
    if flags.contains(.command) {
        COMMAND
    }
    return false
}
assert(handle("h", flags: [.command]))
assert(handle("l", flags: [.command]))
assert(handle("h", flags: [.command, .shift]))
assert(handle("l", flags: [.command, .shift]))
assert(offsets == [-5, 5, -30, 30])
assert(handle("j", flags: [.command]))
assert(handle("k", flags: [.command]))
assert(selections == [3, 1])
for editing in [false, true] {
    offsets = []
    selections = []
    assert(handle("h", flags: [.command], editing: editing))
    assert(handle("l", flags: [.command], editing: editing))
    assert(handle("h", flags: [.command, .shift], editing: editing))
    assert(handle("l", flags: [.command, .shift], editing: editing))
    assert(handle("j", flags: [.command], editing: editing))
    assert(handle("k", flags: [.command], editing: editing))
    assert(offsets == [-5, 5, -30, 30])
    assert(selections == [3, 1])
}
for key in ["j", "k"] {
    assert(!handle(key, flags: []))
    assert(!handle(key, flags: [.command, .shift]))
    assert(!handle(key, flags: [.command, .option]))
    assert(!handle(key, flags: [.command, .control]))
}
assert(selections == [3, 1])
for key in ["h", "l"] {
    assert(!handle(key, flags: []))
    assert(!handle(key, flags: [.command, .option]))
    assert(!handle(key, flags: [.command, .control]))
}
assert(!handle("s", flags: [.command]))
assert(offsets == [-5, 5, -30, 30])
'''.replace("COMMAND", command)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text(harness)
            result = subprocess.run(["swift", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_show_in_finder(self):
        source = Path(__file__).with_name("main.swift").read_text()
        reveal = source.split("    @objc func showInFinder(", 1)[1].split("    func refreshDestinations()", 1)[0]
        harness = '''
class Menu {}
class NSMenuItem {
    var menu: Menu?
    var representedObject: Any?
}
class Workspace {
    static let shared = Workspace()
    var revealed: [URL] = []
    func activateFileViewerSelecting(_ urls: [URL]) { revealed = urls }
}
class Revealer {
    var files: [URL] = []
    var index = -1
    let table = Table()
    var errors = 0
    func report(_ error: Error) { errors += 1 }
    REVEAL_FUNCTION
}
class Table { let menu: Menu? = Menu() }
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let first = root.appendingPathComponent("first track.mp3")
let second = root.appendingPathComponent("second.mp3")
try Data().write(to: first)
try Data().write(to: second)
let revealer = Revealer()
revealer.showInFinder(nil)
assert(Workspace.shared.revealed.isEmpty)
revealer.files = [first, second]
revealer.index = 0
revealer.showInFinder(nil)
assert(Workspace.shared.revealed == [first])
let item = NSMenuItem()
item.menu = revealer.table.menu
item.representedObject = second
revealer.showInFinder(item)
assert(Workspace.shared.revealed == [second])
assert(revealer.index == 0)
assert(revealer.files == [first, second])
item.representedObject = nil
revealer.showInFinder(item)
assert(Workspace.shared.revealed == [second])
try FileManager.default.removeItem(at: first)
revealer.showInFinder(nil)
assert(revealer.errors == 1)
assert(Workspace.shared.revealed == [second])
'''
        harness = harness.replace("REVEAL_FUNCTION", "func showInFinder(" + reveal).replace("NSWorkspace.shared", "Workspace.shared")
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text("import Foundation\n" + harness)
            result = subprocess.run(["swift", str(script), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_configuration_and_move_rollback(self):
        source = Path(__file__).with_name("main.swift").read_text()
        configuration = source.split("struct Destination:", 1)[1].split("final class Sorter:", 1)[0]
        move = source.split("    func moveTaggedFile(", 1)[1].split("    func move(_ destination:", 1)[0]
        harness = '''
func decode(_ destinations: String) throws -> Configuration {
    let json = "{\\"destinationRoot\\":\\"/music\\",\\"destinations\\":" + destinations + "}"
    return try JSONDecoder().decode(Configuration.self, from: Data(json.utf8))
}
let legacy = try decode("[\\"House\\"]")
assert(legacy.destinations[0].directory == "House")
assert(legacy.destinations[0].genre == nil)
let paired = try decode("[{\\"directory\\":\\"Tech-House\\",\\"genre\\":\\"Tech House\\"},{\\"directory\\":\\"Review\\"}]")
assert(paired.destinations[0].genre == "Tech House")
assert(paired.destinations[1].genre == nil)
// Settings saves migrate legacy entries without losing optional values.
let encoder = JSONEncoder()
for config in [legacy, paired] {
    let restored = try JSONDecoder().decode(Configuration.self, from: encoder.encode(config))
    assert(restored.sourceFolder == "~/Downloads")
    assert(restored.destinationRoot == "/music")
    assert(restored.startAtThirtyPercent)
    assert(restored.destinations.map(\\.directory) == config.destinations.map(\\.directory))
    assert(restored.destinations.map(\\.genre) == config.destinations.map(\\.genre))
}
var customized = paired
customized.sourceFolder = "/new/source"
customized.destinationRoot = "/new/destinations"
customized.startAtThirtyPercent = false
let saved = try JSONDecoder().decode(Configuration.self, from: encoder.encode(customized))
assert(saved.sourceFolder == customized.sourceFolder)
assert(saved.destinationRoot == customized.destinationRoot)
assert(!saved.startAtThirtyPercent)
for pathKey in ["sourceFolder", "destinationRoot"] {
    var json: [String: Any] = ["sourceFolder": "/source", "destinationRoot": "/music", "destinations": ["House"]]
    json[pathKey] = "  "
    do {
        _ = try JSONDecoder().decode(Configuration.self, from: JSONSerialization.data(withJSONObject: json))
        fatalError("Empty directory accepted")
    } catch { }
}
for invalid in ["[]", "[\\"House\\",{\\"directory\\":\\"House\\"}]", "[\\"../bad\\"]", "[{\\"directory\\":\\"House\\",\\"genre\\":\\"  \\"}]"] {
    do { _ = try decode(invalid); fatalError("Invalid configuration accepted") }
    catch { }
}
class Mover {
    var fail = false
    var calls = 0
    func helper(_ action: String, file: URL, text: String?) throws -> [String: Any] {
        calls += 1
        if fail { throw NSError(domain: "test", code: 1) }
        return ["previous": NSNull()]
    }
    MOVE_FUNCTION
}
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let original = root.appendingPathComponent("original.mp3")
let target = root.appendingPathComponent("target.mp3")
try Data("audio".utf8).write(to: original)
let mover = Mover()
mover.fail = true
do { _ = try mover.moveTaggedFile(from: original, to: target, change: ["genre": "House"]); fatalError("Expected tag failure") }
catch { }
assert(FileManager.default.fileExists(atPath: original.path))
assert(!FileManager.default.fileExists(atPath: target.path))
mover.fail = false
let restore = try mover.moveTaggedFile(from: original, to: target, change: ["genre": "House"])
assert(restore?["restore"] is NSNull)
assert(!FileManager.default.fileExists(atPath: original.path))
assert(FileManager.default.fileExists(atPath: target.path))
_ = try mover.moveTaggedFile(from: target, to: original, change: restore)
// A collision must never invoke the tag writer or overwrite either file.
try Data("existing".utf8).write(to: target)
let calls = mover.calls
do { _ = try mover.moveTaggedFile(from: original, to: target, change: ["genre": "House"]); fatalError("Expected collision") }
catch { }
assert(mover.calls == calls)
let contents = try String(contentsOf: target, encoding: .utf8)
assert(contents == "existing")
try FileManager.default.removeItem(at: target)
_ = try mover.moveTaggedFile(from: original, to: target, change: nil)
assert(mover.calls == calls)
'''
        harness = harness.replace("MOVE_FUNCTION", "func moveTaggedFile(" + move)
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text("import Foundation\nstruct Destination:" + configuration + harness)
            result = subprocess.run(["swift", str(script), directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bar_edit_model_cut_duplicate_undo_and_regrid(self):
        source = Path(__file__).with_name("main.swift").read_text().split("let app = NSApplication.shared", 1)[0]
        harness = r'''
// 120 BPM: two-second bars, first downbeat at 0.5 s, 21-second track.
let duration = 21.0
var session = EditSession(grid: BeatGrid(bpm: 120, anchor: 0.5))
assert(session.grid.firstBar == 0 && session.grid.number(0) == 1)
assert(BeatGrid(bpm: 120, anchor: -0.5).firstBar == 1)
do { try session.edit(duplicate: false, duration: duration); fatalError("Expected missing selection") } catch {}
// Cut bars 3–5 (output bars 2..<5).
session.selection = 2
session.bars = 3
assert(session.selectionDescription(duration) == "bars 3–5")
try session.edit(duplicate: false, duration: duration)
assert(session.pieces == [Piece(from: .start, to: .bar(2)), Piece(from: .bar(5), to: .end)])
assert(abs(session.outputDuration(duration) - 15) < 1e-9)
assert(session.selection == 2 && session.undo.count == 1)
// Unsaved until written; a save marks exactly these pieces as saved.
assert(session.unsaved && !EditSession(grid: session.grid).unsaved)
session.savedPieces = session.pieces
assert(!session.unsaved)
// Duplicate bars 2–3 of the edited track; the copy follows and becomes the selection.
session.selection = 1
session.bars = 2
try session.edit(duplicate: true, duration: duration)
assert(session.pieces == [Piece(from: .start, to: .bar(2)), Piece(from: .bar(5), to: .bar(6)), Piece(from: .bar(1), to: .bar(2)), Piece(from: .bar(5), to: .end)])
assert(session.selection == 3 && abs(session.outputDuration(duration) - 19) < 1e-9)
assert(session.unsaved)
let duplicated = session.pieces
session.savedPieces = duplicated
// Frames at 1 kHz: where each part of the edited track comes from.
let timeline = EditTimeline(session: session, rate: 1000, frames: 21_000)
assert(timeline.length == 19_000)
assert(timeline.segments == [[0, 4500], [10_500, 12_500], [2500, 4500], [10_500, 21_000]])
assert(timeline.junctions == [4500, 6500, 8500])
let ranges = timeline.ranges(from: 4000, count: 1000)
assert(ranges.count == 2 && ranges[0] == (4000, 500) && ranges[1] == (10_500, 500))
assert(timeline.ranges(from: 18_990, count: 100).map(\.count) == [10])
// Out-of-range selections are refused without changing anything.
let before = session.pieces
session.selection = 9
do { try session.edit(duplicate: true, duration: duration); fatalError("Expected end-of-track error") } catch {}
session.selection = -1
do { try session.edit(duplicate: false, duration: duration); fatalError("Expected pickup error") } catch {}
assert(session.pieces == before && session.undo.count == 2)
// Moving the downbeat by whole bars renumbers bounds; the audio they point at is unchanged.
session.regrid(BeatGrid(bpm: 120, anchor: 2.5))
assert(session.pieces == [Piece(from: .start, to: .bar(1)), Piece(from: .bar(4), to: .bar(5)), Piece(from: .bar(0), to: .bar(1)), Piece(from: .bar(4), to: .end)])
assert(EditTimeline(session: session, rate: 1000, frames: 21_000).segments == timeline.segments)
// Renumbering is not an edit: the saved state follows the grid.
assert(!session.unsaved && session.savedPieces == session.pieces)
assert(session.undo[0] == [Piece(from: .start, to: .end)] && session.undo[1] == [Piece(from: .start, to: .bar(1)), Piece(from: .bar(4), to: .end)])
// Cutting from the very first downbeat starts the edit on a bar line.
var intro = EditSession(grid: BeatGrid(bpm: 120, anchor: 0))
intro.selection = 0
intro.bars = 1
try intro.edit(duplicate: false, duration: duration)
assert(intro.pieces == [Piece(from: .bar(1), to: .end)] && intro.outputGrid(duration).anchor == 0)
// Cutting everything that remains between two bars and re-joining merges contiguous pieces.
var merge = EditSession(grid: BeatGrid(bpm: 120, anchor: 0.5))
merge.selection = 2
merge.bars = 2
try merge.edit(duplicate: true, duration: duration)
merge.selection = 4
try merge.edit(duplicate: false, duration: duration)
assert(merge.pieces == EditSession.untouched && !merge.edited)
// Tap tempo averages from the first tap to the last and restarts after a pause.
var tapper = TapTempo()
assert(tapper.tap(at: 10) == nil && tapper.tap(at: 10.5) == nil && tapper.tap(at: 11) == nil)
assert(tapper.tap(at: 11.5) == 120)
assert(tapper.tap(at: 12.01) == 119.4)
assert(tapper.tap(at: 20) == nil && tapper.taps == [20])
// The metronome clicks for 30 ms on each beat, louder on downbeats, never past the end.
let clickGrid = BeatGrid(bpm: 120, anchor: 0.25)
func peak(_ start: Double) -> Float { (0..<2205).map { abs(Metronome.click(at: start + Double($0) / 44_100, grid: clickGrid, end: 10)) }.max()! }
assert(peak(0.25) > peak(0.75) && peak(0.75) > 0.2 && abs(peak(2.25) - peak(0.25)) < 1e-3)
assert(peak(0.29) == 0 && peak(0.1) == 0 && peak(10.25) == 0)
var samples = [Float](repeating: 1, count: 100)
samples.withUnsafeMutableBufferPointer { Metronome.fill($0.baseAddress!, count: 100, from: 0.25, rate: 44_100, state: Metronome.State(enabled: true, grid: clickGrid, end: 10)) }
assert(samples == (0..<100).map { Metronome.click(at: 0.25 + Double($0) / 44_100, grid: clickGrid, end: 10) })
'''
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text(source + harness)
            result = subprocess.run(["swift", str(script)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg unavailable")
    def test_editor_peaks_decoding_and_saving_end_to_end(self):
        import math
        import struct
        import wave
        source = Path(__file__).with_name("main.swift").read_text().split("let app = NSApplication.shared", 1)[0]
        source = source.replace("final class Sorter:", "class Sorter:")
        harness = r'''
// The real Python helper, run from the repository rather than the app bundle.
class TestSorter: Sorter {
    var refreshed = 0
    var sabotage = false
    var trashed: [URL] = []
    var answer = false
    var asked: [[String]] = []
    override func askToDiscard(_ unsaved: [URL]) -> Bool { asked.append(unsaved.map(\.lastPathComponent)); return answer }
    override func helper(_ action: String, file: URL, text: String? = nil) throws -> [String: Any] {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: CommandLine.arguments[2])
        process.arguments = [CommandLine.arguments[3], action, file.path]
        let output = Pipe(), input = Pipe()
        process.standardOutput = output
        process.standardInput = input
        try process.run()
        if let text { try input.fileHandleForWriting.write(contentsOf: Data(text.utf8)) }
        try input.fileHandleForWriting.close()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        if process.terminationStatus != 0 { throw NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: result["error"] as? String ?? "helper failed"]) }
        // Simulates a file damaged after writing; verification must catch it.
        if sabotage, action == "cut", let path = result["output"] as? String { try Data("not audio".utf8).write(to: URL(fileURLWithPath: path)) }
        return result
    }
    override func refreshFolder() { refreshed += 1; super.refreshFolder() }
    // Never touches the real Trash.
    override func sendToTrash(_ url: URL) throws { trashed.append(url); try FileManager.default.removeItem(at: url) }
    override func report(_ error: Error) { fatalError("Unexpected error: \(error)") }
}
let app = NSApplication.shared
let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let sorter = TestSorter()
sorter.configuration = try JSONDecoder().decode(Configuration.self, from: Data("{\"destinationRoot\":\"/unused\",\"destinations\":[\"A\"],\"editSuffix\":\"SamEdit\"}".utf8))
assert(sorter.editURL(for: root.appendingPathComponent("a.b.mp3")).lastPathComponent == "a.b - SamEdit.mp3")
sorter.source = root

// Peaks: every resolution agrees with the loudness layout of the fixture.
let wav = root.appendingPathComponent("loop.wav")
let peaks = waveformPeaks(wav)!
assert(peaks.minimum.count >= 3 && peaks.minimum[0].count == (264_600 + 31) / 32)
let loud = peaks.extremes(from: 0, to: 88_200)!, quiet = peaks.extremes(from: 90_000, to: 170_000)!
assert(loud.1 > 0.8 && loud.0 < -0.8 && quiet.1 < 0.1, "\(loud) \(quiet)")
assert(peaks.extremes(from: 10, to: 10) == nil && waveformPeaks(wav, cancelled: { true }) == nil)

// MP3 decoding for edits yields exactly the frames AVFoundation plays and draws.
let mp3 = root.appendingPathComponent("loop.mp3")
let decoded = root.appendingPathComponent("decoded.wav")
try decodeToWAV(mp3, to: decoded)
let reference = try AVAudioFile(forReading: mp3), copy = try AVAudioFile(forReading: decoded)
assert(copy.length == reference.length && copy.processingFormat.sampleRate == reference.processingFormat.sampleRate)
try FileManager.default.removeItem(at: decoded)

let editor = EditorController(sorter: sorter)
sorter.editorController = editor
func taggedBPM(_ url: URL) -> Double? { (try! sorter.helper("read-bpm", file: url))["bpm"] as? Double }
func frames(_ url: URL) -> Int64 { try! AVAudioFile(forReading: url).length }
func settle(_ condition: () -> Bool) {
    let deadline = Date().addingTimeInterval(60)
    while !condition() && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.02)) }
    assert(condition(), "timed out")
}
for (name, bpm) in [("loop.wav", 120.0), ("loop.mp3", 120.0), ("loop.aiff", 120.0)] {
    let url = root.appendingPathComponent(name)
    editor.open(url)
    assert(editor.session.grid.bpm == bpm && editor.bpmField.stringValue == "120")
    settle { editor.waveform.peaks != nil }
    assert(!editor.saveButton.isEnabled)
    // Clicks snap only after the grid is placed, to the finest division at least 12 px wide.
    assert(!editor.waveform.snapping)
    editor.snapButton.state = .on
    editor.snapChanged()
    editor.playhead = 0
    editor.setDownbeat()
    assert(editor.waveform.snapping && editor.session.downbeatSet)
    let grid = editor.timeline.grid
    editor.waveform.secondsPerPixel = 0.001
    assert(editor.waveform.snapUnit(grid) == 0.125 && editor.waveform.snapped(1.3, to: grid) == 1.25)
    editor.waveform.secondsPerPixel = 0.05
    assert(editor.waveform.snapUnit(grid) == 1 && editor.waveform.snapped(1.3, to: grid) == 1)
    editor.snapButton.state = .off
    editor.snapChanged()
    assert(!editor.waveform.snapping)
    editor.waveform.fit()
    // Arrow keys snap to the next grid line: bars are 2 s, beats 0.5 s.
    editor.playhead = 2.1
    editor.step(1, bars: true)
    assert(abs(editor.playhead - 4) < 1e-9)
    editor.step(-1, bars: false)
    assert(abs(editor.playhead - 3.5) < 1e-9)
    editor.step(-1, bars: true)
    assert(abs(editor.playhead - 2) < 1e-9)
    // 6 s at 120 BPM is three bars: loud, quiet, loud. Cut the quiet bar, then repeat the last.
    editor.playhead = 2.1
    editor.setSelectionStart()
    editor.session.bars = 1
    editor.cutSelection()
    assert(editor.session.edited && editor.saveButton.isEnabled && editor.timeline.junctions == [88_200])
    editor.playhead = 2.2
    editor.setSelectionStart()
    assert(editor.session.selection == 1)
    editor.duplicateSelection()
    assert(editor.session.selection == 2 && editor.timeline.length == 264_600)
    editor.undoEdit()
    assert(editor.timeline.length == 176_400 && editor.redoButton.isEnabled)
    editor.redoEdit()
    editor.save()
    settle { !editor.saving }
    let output = root.appendingPathComponent((name as NSString).deletingPathExtension + " - SamEdit." + (name as NSString).pathExtension)
    assert(editor.status.stringValue.hasPrefix("Saved"), editor.status.stringValue)
    // Back in the main list with the edit selected, ready to sort.
    assert(editor.window?.isVisible == false && sorter.files[sorter.index].lastPathComponent == output.lastPathComponent)
    assert(sorter.status.stringValue.hasPrefix("Saved and selected"))
    let saved = try AVAudioFile(forReading: output)
    assert(saved.length == 264_600, "\(name): \(saved.length)")
    // The edit carries the grid's BPM; the original's tag is untouched.
    assert(taggedBPM(output) == 120)
    assert(taggedBPM(url) == (name == "loop.wav" ? nil : 120))
    // The repeated bar is loud audio, not the cut quiet bar.
    let edited = waveformPeaks(output)!
    assert(edited.extremes(from: 180_000, to: 260_000)!.1 > 0.8)
    // Saving again never overwrites the edit.
    editor.save()
    assert(!editor.saving && editor.status.stringValue.contains("already exists"))
    // The session is remembered when the editor moves on or closes.
    editor.session.bars = 1
}
assert(sorter.refreshed == 3)
editor.window?.close()
assert(sorter.editSessions.count == 3 && sorter.editSessions[root.appendingPathComponent("loop.wav")]?.edited == true)
editor.open(root.appendingPathComponent("loop.wav"))
assert(editor.session.edited && editor.timeline.length == 264_600)
// A saved file that does not read back at the edited length is moved to Trash.
let edit = root.appendingPathComponent("loop - SamEdit.wav")
try FileManager.default.removeItem(at: edit)
let original = try Data(contentsOf: root.appendingPathComponent("loop.wav"))
sorter.sabotage = true
editor.save()
settle { !editor.saving }
assert(editor.status.stringValue.contains("failed verification"), editor.status.stringValue)
assert(sorter.trashed == [edit] && !FileManager.default.fileExists(atPath: edit.path))
let unchanged = try Data(contentsOf: root.appendingPathComponent("loop.wav"))
assert(unchanged == original && sorter.refreshed == 3)
// Unsaved edits: marked in the editor and confirmed before quitting.
assert(!editor.session.unsaved && editor.window?.isDocumentEdited == false && sorter.unsavedEdits().isEmpty)
assert(sorter.applicationShouldTerminate(app) == .terminateNow && sorter.asked.isEmpty)
editor.playhead = 0.1
editor.setSelectionStart()
editor.session.bars = 1
editor.duplicateSelection()
assert(editor.session.unsaved && editor.window?.isDocumentEdited == true && editor.editLabel.stringValue.hasSuffix("unsaved"))
// Closing the editor keeps the edit, still unsaved.
editor.window?.close()
assert(sorter.unsavedEdits().map(\.lastPathComponent) == ["loop.wav"])
assert(sorter.applicationShouldTerminate(app) == .terminateCancel && sorter.asked == [["loop.wav"]])
assert(!sorter.windowShouldClose(NSWindow()) && sorter.asked.count == 2)
sorter.answer = true
assert(sorter.windowShouldClose(NSWindow()) && sorter.asked.count == 3)
// Closing the last window then quits without asking again.
assert(sorter.applicationShouldTerminate(app) == .terminateNow && sorter.asked.count == 3)
// Tapping sets the tempo; the metronome follows the edited track's grid.
editor.open(root.appendingPathComponent("loop.wav"))
assert(editor.status.stringValue.hasSuffix("Restored this track's grid and edits."))
// The waveform finishing must not overwrite a newer message.
editor.status.stringValue = "newer"
settle { editor.waveform.peaks != nil }
RunLoop.main.run(until: Date().addingTimeInterval(0.1))
assert(editor.status.stringValue == "newer")
for (i, time) in [100.0, 100.6, 101.2, 101.8].enumerated() {
    editor.tap(at: time)
    assert(editor.status.stringValue.hasPrefix(i < 3 ? "Tap along" : "Tapped 100 BPM"), editor.status.stringValue)
}
assert(editor.session.grid.bpm == 100 && editor.bpmField.stringValue == "100")
// Writing the BPM to the original is explicit, and only offered when it would change the tag.
let wavURL = root.appendingPathComponent("loop.wav")
assert(editor.taggedBPM == nil && editor.writeBPMButton.isEnabled)
let wavAudio = frames(wavURL)
editor.writeBPM()
assert(taggedBPM(wavURL) == 100 && editor.taggedBPM == 100)
assert(!editor.writeBPMButton.isEnabled && editor.status.stringValue.hasPrefix("Wrote BPM 100"))
assert(frames(wavURL) == wavAudio)
assert(editor.format(124.3) == "124.3" && editor.format(123.25) == "123.25" && editor.format(124) == "124")
editor.clickButton.state = .on
editor.clickChanged()
assert(editor.metronome.snapshot.enabled && editor.metronome.snapshot.grid == editor.timeline.grid)
assert(editor.metronome.snapshot.end == editor.timeline.duration && editor.metronome.snapshot.sync == nil)
editor.clickButton.state = .off
editor.clickChanged()
assert(!editor.metronome.snapshot.enabled)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rate = 44100
            frames = bytearray()
            for i in range(rate * 6):
                amplitude = 0.05 if rate * 2 <= i < rate * 4 else 0.9
                sample = int(32767 * amplitude * math.sin(2 * math.pi * 220 * i / rate))
                frames += struct.pack("<hh", sample, sample)
            with wave.open(str(root / "loop.wav"), "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(rate)
                output.writeframes(bytes(frames))
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(root / "loop.wav"), "-c:a", "libmp3lame", "-b:a", "320k",
                            "-metadata", "TBPM=120", str(root / "loop.mp3")], check=True)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(root / "loop.wav"), "-c:a", "pcm_s16be", "-write_id3v2", "1",
                            "-metadata", "TBPM=120", str(root / "loop.aiff")], check=True)
            # The WAV has no BPM tag; 120 is the editor's default.
            script = root / "main.swift"
            script.write_text(source + harness)
            helper = str(Path(__file__).with_name("comments.py"))
            result = subprocess.run(["swift", str(script), directory, sys.executable, helper], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
