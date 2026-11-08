"""Exercise the production Swift configuration and move/rollback code without a GUI."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("swift"), "Swift unavailable")
class NativeTests(unittest.TestCase):
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
