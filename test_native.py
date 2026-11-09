"""Exercise the production Swift configuration and move/rollback code without a GUI."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(shutil.which("swift"), "Swift unavailable")
class NativeTests(unittest.TestCase):
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
class Field { var string = ""; var stringValue = ""; var isEditable = false }
class Table { func reloadData() {} }
class Defaults {
    static let standard = Defaults()
    func set(_ value: Any, forKey: String) {}
}
class Configuration { var startAtThirtyPercent = true }
class Sorter {
    let recursive = Toggle()
    let configuration = Configuration()
    let table = Table()
    let folderLabel = Field(), status = Field(), title = Field(), comment = Field()
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
