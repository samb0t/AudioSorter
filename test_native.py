"""Exercise the production Swift configuration and move/rollback code without a GUI."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("swift"), "Swift unavailable")
class NativeTests(unittest.TestCase):
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
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "main.swift"
            script.write_text(harness)
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
