"""Exercise the production Swift configuration and move/rollback code without a GUI."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which("swift"), "Swift unavailable")
class NativeTests(unittest.TestCase):
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
