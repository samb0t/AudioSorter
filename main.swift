import Cocoa
import AVFoundation

struct Destination: Codable {
    var directory: String
    var genre: String?

    enum CodingKeys: String, CodingKey { case directory, genre }

    init(from decoder: Decoder) throws {
        if let name = try? decoder.singleValueContainer().decode(String.self) {
            directory = name
            genre = nil
        } else {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            directory = try values.decode(String.self, forKey: .directory)
            genre = try values.decodeIfPresent(String.self, forKey: .genre)
        }
        if let genre, genre.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Genre must not be empty; omit it to preserve the existing genre."))
        }
    }
}

struct Configuration: Codable {
    var sourceFolder: String = "~/Downloads"
    var destinationRoot: String
    var destinations: [Destination]
    var startAtThirtyPercent: Bool = true

    enum CodingKeys: String, CodingKey { case sourceFolder, destinationRoot, destinations, startAtThirtyPercent }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sourceFolder = try values.decodeIfPresent(String.self, forKey: .sourceFolder) ?? "~/Downloads"
        destinationRoot = try values.decode(String.self, forKey: .destinationRoot)
        destinations = try values.decode([Destination].self, forKey: .destinations)
        startAtThirtyPercent = try values.decodeIfPresent(Bool.self, forKey: .startAtThirtyPercent) ?? true
        let directories = destinations.map(\.directory)
        guard !sourceFolder.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              !destinationRoot.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              (1...9).contains(destinations.count), Set(directories).count == destinations.count,
              directories.allSatisfy({ !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && $0 != "." && $0 != ".." && !$0.contains("/") }) else {
            throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "Source and destination parent directories are required. Configure 1–9 unique destination folder names (no slashes, . or ..)."))
        }
    }

    static func expand(_ path: String) -> URL {
        URL(fileURLWithPath: (path as NSString).expandingTildeInPath, isDirectory: true)
    }
}

final class Sorter: NSObject, NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate, NSWindowDelegate, NSMenuDelegate, NSMenuItemValidation {
    var configuration: Configuration!
    var names: [String] { configuration.destinations.map(\.directory) }
    var destinationRoot: URL { Configuration.expand(configuration.destinationRoot) }
    var files: [URL] = []
    var index = -1
    var player: AVAudioPlayer?
    var originalComment = ""
    var commentReadable = true
    var loading = false
    var undoMoves: [(from: URL, to: URL, genreRestore: [String: Any]?)] = []
    var monitor: Any?
    var timer: Timer?
    var window: NSWindow!
    let table = NSTableView()
    let title = NSTextField(labelWithString: "Choose a folder to begin")
    let folderLabel = NSTextField(labelWithString: "")
    let status = NSTextField(labelWithString: "No files are moved until you press a destination key.")
    let time = NSTextField(labelWithString: "0:00 / 0:00")
    let slider = NSSlider(value: 0, minValue: 0, maxValue: 1, target: nil, action: nil)
    let comment = NSTextView()
    let recursive = NSButton(checkboxWithTitle: "Include subfolders", target: nil, action: nil)
    var source: URL?
    var configurationURL: URL!
    var settingsController: SettingsController?
    let destinationButtons = NSStackView()
    let destinationHelp = NSTextField(labelWithString: "")
    let revealButton = NSButton(title: "Show in Finder  ⌘⇧R", target: nil, action: nil)

    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenu()
        do {
            let path = try String(contentsOf: Bundle.main.resourceURL!.appendingPathComponent("config-path"), encoding: .utf8)
            configurationURL = URL(fileURLWithPath: path)
            configuration = try JSONDecoder().decode(Configuration.self, from: Data(contentsOf: configurationURL))
        } catch {
            NSAlert(error: NSError(domain: "AudioSorter", code: 3, userInfo: [NSLocalizedDescriptionKey: "Cannot load config.json: \(error.localizedDescription)"])).runModal()
            NSApp.terminate(nil)
            return
        }
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 940, height: 720), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Audio Sorter"
        window.minSize = NSSize(width: 780, height: 620)
        window.delegate = self
        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 12
        root.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24),
            root.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24),
            root.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24),
            root.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -24)
        ])
        let open = button("Open folder…  ⌘O", #selector(chooseFolder))
        recursive.target = self
        recursive.action = #selector(reloadFolder)
        revealButton.target = self
        revealButton.action = #selector(showInFinder(_:))
        revealButton.isEnabled = false
        let toolbar = NSStackView(views: [open, recursive, revealButton, button("Settings…", #selector(showSettings))])
        toolbar.spacing = 18
        root.addArrangedSubview(toolbar)
        folderLabel.lineBreakMode = .byTruncatingMiddle
        folderLabel.textColor = .secondaryLabelColor
        let folderRow = NSStackView(views: [folderLabel, button("Refresh  ⌘R", #selector(refreshFolder))])
        folderRow.spacing = 12
        root.addArrangedSubview(folderRow)
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("track"))
        column.title = "MP3s — J / K or ↓ / ↑ select, Enter plays"
        table.addTableColumn(column)
        table.delegate = self
        table.dataSource = self
        table.rowHeight = 25
        table.allowsEmptySelection = true
        let trackMenu = NSMenu()
        trackMenu.delegate = self
        let reveal = trackMenu.addItem(withTitle: "Show in Finder", action: #selector(showInFinder(_:)), keyEquivalent: "")
        reveal.target = self
        table.menu = trackMenu
        let list = NSScrollView()
        list.documentView = table
        list.hasVerticalScroller = true
        list.borderType = .bezelBorder
        root.addArrangedSubview(list)
        list.heightAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        title.lineBreakMode = .byTruncatingMiddle
        root.addArrangedSubview(title)
        slider.target = self
        slider.action = #selector(seek)
        let transport = NSStackView(views: [button("Play / pause  ↵", #selector(playPause)), slider, time])
        transport.spacing = 12
        root.addArrangedSubview(transport)
        root.addArrangedSubview(NSTextField(labelWithString: "H / L or ← / →  scrub 5s    •    Shift  30s    •    J / K  down/up    •    N  next    •    E  comment"))
        let commentHeader = NSStackView(views: [NSTextField(labelWithString: "Comment • ⌘H/L scrub • Shift 30s • ⌘J/K down/up"), button("Save & return  ⌘↵", #selector(saveAndReturn))])
        commentHeader.spacing = 20
        root.addArrangedSubview(commentHeader)
        comment.isRichText = false
        comment.isAutomaticQuoteSubstitutionEnabled = false
        comment.isAutomaticDashSubstitutionEnabled = false
        comment.font = .systemFont(ofSize: 14)
        comment.textContainerInset = NSSize(width: 8, height: 8)
        comment.isVerticallyResizable = true
        comment.isHorizontallyResizable = false
        comment.autoresizingMask = [.width]
        comment.textContainer?.widthTracksTextView = true
        let commentScroll = NSScrollView()
        commentScroll.documentView = comment
        commentScroll.hasVerticalScroller = true
        commentScroll.borderType = .bezelBorder
        root.addArrangedSubview(commentScroll)
        commentScroll.heightAnchor.constraint(equalToConstant: 90).isActive = true
        let destinations = destinationButtons
        destinations.spacing = 6
        destinations.distribution = .fillEqually
        refreshDestinations()
        root.addArrangedSubview(destinations)
        root.addArrangedSubview(destinationHelp)
        status.textColor = .secondaryLabelColor
        status.lineBreakMode = .byTruncatingMiddle
        root.addArrangedSubview(status)
        for view in [folderRow, list, title, transport, commentScroll, destinations, status] {
            view.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        }
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handle(event) == true ? nil : event
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in self?.updateTime() }
        let initial = UserDefaults.standard.string(forKey: "source") ?? configuration.sourceFolder
        loadFolder(Configuration.expand(initial), autoplay: false)
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func button(_ text: String, _ action: Selector) -> NSButton {
        NSButton(title: text, target: self, action: action)
    }

    func makeMenu() {
        let menu = NSMenu()
        let appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        let settings = appMenu.addItem(withTitle: "Settings…", action: #selector(showSettings), keyEquivalent: ",")
        settings.target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Audio Sorter", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let file = NSMenuItem()
        menu.addItem(file)
        let fileMenu = NSMenu(title: "File")
        let refresh = fileMenu.addItem(withTitle: "Refresh", action: #selector(refreshFolder), keyEquivalent: "r")
        refresh.target = self
        let reveal = fileMenu.addItem(withTitle: "Show in Finder", action: #selector(showInFinder(_:)), keyEquivalent: "r")
        reveal.keyEquivalentModifierMask = [.command, .shift]
        reveal.target = self
        file.submenu = fileMenu
        let edit = NSMenuItem()
        menu.addItem(edit)
        let editMenu = NSMenu(title: "Edit")
        for (label, action, key) in [("Undo", Selector(("undo:")), "z"), ("Cut", #selector(NSText.cut(_:)), "x"), ("Copy", #selector(NSText.copy(_:)), "c"), ("Paste", #selector(NSText.paste(_:)), "v"), ("Select All", #selector(NSText.selectAll(_:)), "a")] {
            editMenu.addItem(withTitle: label, action: action, keyEquivalent: key)
        }
        edit.submenu = editMenu
        NSApp.mainMenu = menu
    }

    var editing: Bool { window.firstResponder === comment }

    func menuNeedsUpdate(_ menu: NSMenu) {
        guard menu === table.menu, let item = menu.items.first else { return }
        item.representedObject = files.indices.contains(table.clickedRow) ? files[table.clickedRow] : nil
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(refreshFolder) {
            return window?.isKeyWindow == true && NSApp.modalWindow == nil && source != nil
        }
        guard menuItem.action == #selector(showInFinder(_:)) else { return true }
        guard window?.isKeyWindow == true, NSApp.modalWindow == nil else { return false }
        if menuItem.menu === table.menu { return menuItem.representedObject is URL }
        return files.indices.contains(index)
    }

    @objc func showInFinder(_ sender: Any?) {
        let url: URL
        if let item = sender as? NSMenuItem, item.menu === table.menu {
            guard let clickedURL = item.representedObject as? URL else { return }
            url = clickedURL
        } else {
            guard files.indices.contains(index) else { return }
            url = files[index]
        }
        guard FileManager.default.fileExists(atPath: url.path) else {
            report(NSError(domain: "AudioSorter", code: 6, userInfo: [NSLocalizedDescriptionKey: "File no longer exists at \(url.path). Reload the source folder."]))
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func refreshDestinations() {
        for view in destinationButtons.arrangedSubviews {
            destinationButtons.removeArrangedSubview(view)
            view.removeFromSuperview()
        }
        for (i, name) in names.enumerated() {
            let b = button("\(i + 1)  \(name)", #selector(moveButton(_:)))
            b.toolTip = configuration.destinations[i].genre.map { "Set Genre to \($0) and move" } ?? "Move without changing Genre"
            b.tag = i
            destinationButtons.addArrangedSubview(b)
        }
        destinationHelp.stringValue = "1–\(names.count)  MOVE + play next    •    ⌘number  save + move from editor    •    ⌘Z  undo move"
    }

    @objc func showSettings() {
        if settingsController == nil { settingsController = SettingsController(sorter: self) }
        settingsController?.present()
    }

    func applySettings(_ updated: Configuration) throws -> Bool {
        guard saveComment() else { return false }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try encoder.encode(updated).write(to: configurationURL, options: .atomic)
        let sourceChanged = configuration.sourceFolder != updated.sourceFolder
        configuration = updated
        refreshDestinations()
        if sourceChanged {
            // A configured source takes precedence over the last ad-hoc folder, even after restart.
            UserDefaults.standard.removeObject(forKey: "source")
            loadFolder(Configuration.expand(updated.sourceFolder), autoplay: false)
        } else {
            status.stringValue = "Settings saved. Destination and genre changes apply to future moves."
        }
        return true
    }

    func handle(_ event: NSEvent) -> Bool {
        guard NSApp.modalWindow == nil, event.window === window else { return false }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        if flags.contains(.command) {
            if editing, !flags.contains(.shift), !flags.contains(.option), !flags.contains(.control), key == "j" || key == "k" {
                select(index + (key == "j" ? 1 : -1), autoplay: false)
                return true
            }
            if editing, !flags.contains(.option), !flags.contains(.control), key == "h" || key == "l" {
                let seconds: Double = flags.contains(.shift) ? 30 : 5
                scrub(key == "h" ? -seconds : seconds)
                return true
            }
            if let number = Int(key), (1...names.count).contains(number), !flags.contains(.shift), !flags.contains(.option), !flags.contains(.control) {
                if !event.isARepeat { move(number - 1) }
                return true
            }
            if key == "o" { chooseFolder(); return true }
            if key == "r", !flags.contains(.shift), !flags.contains(.option), !flags.contains(.control) { refreshFolder(); return true }
            if event.keyCode == 36 || key == "s" { saveAndReturn(); return true }
            if key == "z" && !editing { undoMove(); return true }
            return false
        }
        if editing {
            if event.keyCode == 53 { comment.string = originalComment; window.makeFirstResponder(table); return true }
            return false
        }
        guard !flags.contains(.control), !flags.contains(.option) else { return false }
        switch event.keyCode {
        case 36, 76, 49: playPause(); return true
        case 123: scrub(flags.contains(.shift) ? -30 : -5); return true
        case 124: scrub(flags.contains(.shift) ? 30 : 5); return true
        case 125: select(index + 1, autoplay: false); return true
        case 126: select(index - 1, autoplay: false); return true
        default: break
        }
        switch key {
        case "h": scrub(flags.contains(.shift) ? -30 : -5); return true
        case "l": scrub(flags.contains(.shift) ? 30 : 5); return true
        case "j": select(index + 1, autoplay: false); return true
        case "k": select(index - 1, autoplay: false); return true
        default: break
        }
        if key == "e" { window.makeFirstResponder(comment); return true }
        if key == "n" { next(); return true }
        if let number = Int(key), (1...names.count).contains(number) {
            // Ignore key repeats so holding a destination cannot move several tracks.
            if !event.isARepeat { move(number - 1) }
            return true
        }
        return false
    }

    @objc func chooseFolder() {
        guard saveComment() else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.directoryURL = source
        if panel.runModal() == .OK, let url = panel.url { loadFolder(url, autoplay: false) }
    }

    @objc func reloadFolder() {
        guard saveComment(), let source else { return }
        loadFolder(source, autoplay: false)
    }

    @objc func refreshFolder() {
        guard let source, saveComment() else { return }
        loadFolder(source, autoplay: false, preservingCurrent: true)
    }

    func loadFolder(_ url: URL, autoplay: Bool, preservingCurrent: Bool = false) {
        do {
            let keys: [URLResourceKey] = [.isRegularFileKey, .isSymbolicLinkKey]
            let candidates: [URL]
            if recursive.state == .on {
                var scanError: Error?
                guard let enumerator = FileManager.default.enumerator(at: url, includingPropertiesForKeys: keys, options: [.skipsHiddenFiles], errorHandler: { _, error in scanError = error; return false }) else {
                    throw NSError(domain: "AudioSorter", code: 1, userInfo: [NSLocalizedDescriptionKey: "Cannot read folder"])
                }
                candidates = enumerator.compactMap { $0 as? URL }
                if let scanError { throw scanError }
            } else {
                candidates = try FileManager.default.contentsOfDirectory(at: url, includingPropertiesForKeys: keys, options: [.skipsHiddenFiles])
            }
            let found = try candidates.filter {
                let values = try $0.resourceValues(forKeys: Set(keys))
                return $0.pathExtension.lowercased() == "mp3" && values.isRegularFile == true && values.isSymbolicLink != true
            }.sorted { $0.path.localizedStandardCompare($1.path) == .orderedAscending }
            let current = preservingCurrent && files.indices.contains(index) ? files[index] : nil
            let retainedIndex = current.flatMap { found.firstIndex(of: $0) }
            if retainedIndex == nil { player?.stop() }
            source = url
            UserDefaults.standard.set(url.path, forKey: "source")
            folderLabel.stringValue = url.path
            files = found
            index = retainedIndex ?? -1
            loading = true
            table.reloadData()
            loading = false
            if retainedIndex != nil {
                highlight()
                status.stringValue = "Refreshed. \(files.count) MP3s ready. Current track and playback position preserved."
            }
            else if files.isEmpty { clear(); status.stringValue = "No MP3s found. Choose another folder or include subfolders." }
            else { select(0, autoplay: autoplay); status.stringValue = "\(files.count) MP3s ready. Enter plays; 1–\(names.count) moves the selected file." }
        } catch { report(error) }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { files.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let field = NSTextField(labelWithString: files[row].lastPathComponent)
        field.lineBreakMode = .byTruncatingMiddle
        return field
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        guard !loading, table.selectedRow >= 0, table.selectedRow != index else { return }
        select(table.selectedRow, autoplay: false)
    }

    func select(_ newIndex: Int, autoplay: Bool) {
        guard files.indices.contains(newIndex) else { return }
        guard saveComment() else { highlight(); return }
        player?.stop()
        player = nil
        index = newIndex
        title.stringValue = files[index].lastPathComponent
        comment.string = ""
        originalComment = ""
        commentReadable = false
        do {
            let data = try helper("read", file: files[index])
            originalComment = data["comment"] as? String ?? ""
            comment.string = originalComment
            commentReadable = true
        } catch { report(error) }
        comment.isEditable = commentReadable
        do {
            player = try AVAudioPlayer(contentsOf: files[index])
            player?.prepareToPlay()
            if configuration.startAtThirtyPercent, let player { player.currentTime = player.duration * 0.3 }
            if autoplay { player?.play() }
        } catch { report(error) }
        highlight()
        updateTime()
    }

    func highlight() {
        loading = true
        if files.indices.contains(index) {
            table.selectRowIndexes(IndexSet(integer: index), byExtendingSelection: false)
            table.scrollRowToVisible(index)
        } else { table.deselectAll(nil) }
        revealButton.isEnabled = files.indices.contains(index)
        loading = false
    }

    func helper(_ action: String, file: URL, text: String? = nil) throws -> [String: Any] {
        let resources = Bundle.main.resourceURL!
        let python = try String(contentsOf: resources.appendingPathComponent("python-path"), encoding: .utf8)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [resources.appendingPathComponent("comments.py").path, action, file.path]
        let output = Pipe()
        process.standardOutput = output
        // Error details are returned as JSON; inherit stderr for interpreter startup failures.
        let input = Pipe()
        process.standardInput = input
        try process.run()
        if let text { try input.fileHandleForWriting.write(contentsOf: Data(text.utf8)) }
        try input.fileHandleForWriting.close()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        if process.terminationStatus != 0 {
            throw NSError(domain: "AudioSorter", code: 2, userInfo: [NSLocalizedDescriptionKey: result["error"] as? String ?? "Cannot access MP3 tags. Try rebuilding the app."])
        }
        return result
    }

    @discardableResult func saveComment() -> Bool {
        guard files.indices.contains(index), comment.string != originalComment else { return true }
        guard commentReadable else { return false }
        do {
            _ = try helper("write", file: files[index], text: comment.string)
            originalComment = comment.string
            status.stringValue = "Comment saved to MP3."
            return true
        } catch { report(error); return false }
    }

    @objc func saveAndReturn() {
        if saveComment() { window.makeFirstResponder(table) }
    }

    @objc func playPause() {
        guard let player else { return }
        if player.isPlaying { player.pause() }
        else { if player.currentTime >= player.duration - 0.05 { player.currentTime = 0 }; player.play() }
    }

    func scrub(_ seconds: Double) {
        guard let player else { return }
        player.currentTime = min(max(0, player.currentTime + seconds), player.duration)
        updateTime()
    }

    @objc func seek() { player?.currentTime = slider.doubleValue; updateTime() }

    func updateTime() {
        let duration = player?.duration ?? 0
        let current = player?.currentTime ?? 0
        slider.maxValue = max(1, duration)
        slider.doubleValue = current
        func stamp(_ value: Double) -> String { String(format: "%d:%02d", Int(value) / 60, Int(value) % 60) }
        time.stringValue = "\(stamp(current)) / \(stamp(duration))"
    }

    func next() {
        if index + 1 < files.count { select(index + 1, autoplay: true) }
        else { if saveComment() { player?.stop(); status.stringValue = "End of list. Use ↑ to revisit tracks or open another folder." } }
    }

    @objc func moveButton(_ sender: NSButton) { move(sender.tag) }

    // Move first so collisions cannot change tags. A failed atomic tag write rolls back the move.
    func moveTaggedFile(from: URL, to: URL, change: [String: Any]?) throws -> [String: Any]? {
        try FileManager.default.moveItem(at: from, to: to)
        guard let change else { return nil }
        do {
            let input = try JSONSerialization.data(withJSONObject: change)
            let result = try helper("write-genre", file: to, text: String(decoding: input, as: UTF8.self))
            return ["restore": result["previous"] ?? NSNull()]
        } catch {
            let tagError = error
            do { try FileManager.default.moveItem(at: to, to: from) }
            catch {
                throw NSError(domain: "AudioSorter", code: 4, userInfo: [NSLocalizedDescriptionKey: "Tag update failed: \(tagError.localizedDescription). Could not move the file back: \(error.localizedDescription). File remains at \(to.path). Reload the source folder after recovering it."])
            }
            throw tagError
        }
    }

    func move(_ destination: Int) {
        guard files.indices.contains(index), saveComment() else { return }
        let original = files[index]
        let target = destinationRoot.appendingPathComponent(names[destination], isDirectory: true).appendingPathComponent(original.lastPathComponent)
        guard original.standardizedFileURL != target.standardizedFileURL else {
            status.stringValue = "Already in \(names[destination]); not moved."
            return
        }
        let position = player?.currentTime ?? 0
        let wasPlaying = player?.isPlaying ?? false
        player?.stop()
        do {
            // FileManager refuses an existing target; never overwrite or auto-rename.
            try FileManager.default.createDirectory(at: target.deletingLastPathComponent(), withIntermediateDirectories: true)
            let genre = configuration.destinations[destination].genre
            let restore = try moveTaggedFile(from: original, to: target, change: genre.map { ["genre": $0] })
            undoMoves.append((original, target, restore))
            let oldIndex = index
            files.remove(at: oldIndex)
            index = -1
            originalComment = ""
            comment.string = ""
            loading = true
            table.reloadData()
            loading = false
            if oldIndex < files.count { select(oldIndex, autoplay: true) }
            else { clear() }
            window.makeFirstResponder(table)
            let tagged = genre.map { " Genre: \($0)." } ?? ""
            status.stringValue = "Moved to \(names[destination]).\(tagged) ⌘Z undoes move/tag.\(oldIndex >= files.count ? " End of list." : "")"
        } catch {
            player?.currentTime = position
            if wasPlaying { player?.play() }
            report(error)
        }
    }

    func undoMove() {
        guard let last = undoMoves.last, saveComment() else { return }
        do {
            _ = try moveTaggedFile(from: last.to, to: last.from, change: last.genreRestore)
            undoMoves.removeLast()
            // Only add it to this queue if it belongs to the current source scope.
            if let source, last.from.deletingLastPathComponent() == source || (recursive.state == .on && last.from.path.hasPrefix(source.path + "/")) {
                let current = files.indices.contains(index) ? files[index] : nil
                files.append(last.from)
                files.sort { $0.path.localizedStandardCompare($1.path) == .orderedAscending }
                index = current.flatMap { files.firstIndex(of: $0) } ?? -1
                loading = true
                table.reloadData()
                loading = false
                if index < 0, let restored = files.firstIndex(of: last.from) { select(restored, autoplay: false) }
                else { highlight() }
            }
            status.stringValue = "Restored \(last.from.lastPathComponent). Comment edits are not undone."
        } catch { report(error) }
    }

    func clear() {
        player?.stop()
        player = nil
        index = -1
        originalComment = ""
        comment.string = ""
        title.stringValue = "End of list"
        highlight()
        updateTime()
    }

    func report(_ error: Error) {
        status.stringValue = error.localizedDescription
        let alert = NSAlert(error: error)
        alert.runModal()
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { saveComment() }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply { saveComment() ? .terminateNow : .terminateCancel }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

final class SettingsController: NSWindowController {
    unowned let sorter: Sorter
    let sourceField = NSTextField(string: "")
    let rootField = NSTextField(string: "")
    let rows = NSStackView()
    let add = NSButton(title: "Add destination", target: nil, action: nil)
    let preview = NSButton(checkboxWithTitle: "Start newly loaded tracks at 30%", target: nil, action: nil)

    init(sorter: Sorter) {
        self.sorter = sorter
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 680, height: 720), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Audio Sorter Settings"
        window.isReleasedWhenClosed = false
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 12
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 24),
            content.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -24),
            content.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 24),
            content.bottomAnchor.constraint(lessThanOrEqualTo: window.contentView!.bottomAnchor, constant: -24)
        ])
        for (label, field, action) in [("Source directory", sourceField, #selector(browseSource)), ("Destination parent directory", rootField, #selector(browseRoot))] {
            content.addArrangedSubview(NSTextField(labelWithString: label))
            let browse = NSButton(title: "Choose…", target: self, action: action)
            let line = NSStackView(views: [field, browse])
            line.spacing = 8
            content.addArrangedSubview(line)
            line.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        }
        content.addArrangedSubview(NSTextField(labelWithString: "Destinations / genre mapping (in keyboard shortcut order)"))
        let hint = NSTextField(wrappingLabelWithString: "Folders are created inside the destination parent. Leave Genre blank to preserve the existing tag. Changes affect future moves only.")
        hint.textColor = .secondaryLabelColor
        content.addArrangedSubview(hint)
        hint.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        rows.orientation = .vertical
        rows.alignment = .leading
        rows.spacing = 6
        content.addArrangedSubview(rows)
        rows.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        add.target = self
        add.action = #selector(addDestination)
        content.addArrangedSubview(add)
        content.addArrangedSubview(preview)
        let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancelSettings))
        cancel.keyEquivalent = "\u{1b}"
        let save = NSButton(title: "Save", target: self, action: #selector(saveSettings))
        save.keyEquivalent = "\r"
        let actions = NSStackView(views: [cancel, save])
        actions.spacing = 8
        content.addArrangedSubview(actions)
        actions.trailingAnchor.constraint(equalTo: content.trailingAnchor).isActive = true
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func present() {
        if window?.isVisible != true {
            sourceField.stringValue = sorter.configuration.sourceFolder
            rootField.stringValue = sorter.configuration.destinationRoot
            preview.state = sorter.configuration.startAtThirtyPercent ? .on : .off
            for row in rows.arrangedSubviews { rows.removeArrangedSubview(row); row.removeFromSuperview() }
            for destination in sorter.configuration.destinations { appendRow(directory: destination.directory, genre: destination.genre ?? "") }
            window?.center()
        }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }

    func appendRow(directory: String = "", genre: String = "") {
        let number = NSTextField(labelWithString: "")
        number.widthAnchor.constraint(equalToConstant: 20).isActive = true
        let directoryField = NSTextField(string: directory)
        directoryField.placeholderString = "Destination folder name"
        let genreField = NSTextField(string: genre)
        genreField.placeholderString = "Genre (preserve if blank)"
        let remove = NSButton(title: "−", target: self, action: #selector(removeDestination(_:)))
        remove.toolTip = "Remove destination"
        let row = NSStackView(views: [number, directoryField, genreField, remove])
        row.spacing = 8
        rows.addArrangedSubview(row)
        row.widthAnchor.constraint(equalTo: rows.widthAnchor).isActive = true
        directoryField.widthAnchor.constraint(equalTo: genreField.widthAnchor).isActive = true
        updateRows()
    }

    func updateRows() {
        for (index, row) in rows.arrangedSubviews.enumerated() {
            guard let row = row as? NSStackView else { continue }
            (row.arrangedSubviews[0] as? NSTextField)?.stringValue = "\(index + 1)"
            (row.arrangedSubviews[3] as? NSButton)?.isEnabled = rows.arrangedSubviews.count > 1
        }
        add.isEnabled = rows.arrangedSubviews.count < 9
    }

    @objc func addDestination() {
        guard rows.arrangedSubviews.count < 9 else { return }
        appendRow()
    }

    @objc func removeDestination(_ sender: NSButton) {
        guard rows.arrangedSubviews.count > 1, let row = sender.superview as? NSStackView else { return }
        rows.removeArrangedSubview(row)
        row.removeFromSuperview()
        updateRows()
    }

    func browse(_ field: NSTextField) {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = Configuration.expand(field.stringValue)
        panel.beginSheetModal(for: window!) { response in
            if response == .OK, let url = panel.url { field.stringValue = url.path }
        }
    }

    @objc func browseSource() { browse(sourceField) }
    @objc func browseRoot() { browse(rootField) }
    @objc func cancelSettings() { close() }

    @objc func saveSettings() {
        func trimmed(_ field: NSTextField) -> String { field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines) }
        do {
            let destinations: [[String: String]] = rows.arrangedSubviews.compactMap { view in
                guard let row = view as? NSStackView,
                      let directory = row.arrangedSubviews[1] as? NSTextField,
                      let genre = row.arrangedSubviews[2] as? NSTextField else { return nil }
                var entry = ["directory": trimmed(directory)]
                if !trimmed(genre).isEmpty { entry["genre"] = trimmed(genre) }
                return entry
            }
            let data = try JSONSerialization.data(withJSONObject: [
                "sourceFolder": trimmed(sourceField), "destinationRoot": trimmed(rootField),
                "destinations": destinations, "startAtThirtyPercent": preview.state == .on
            ])
            let updated = try JSONDecoder().decode(Configuration.self, from: data)
            for path in [updated.sourceFolder, updated.destinationRoot] {
                let expanded = (path as NSString).expandingTildeInPath
                guard (expanded as NSString).isAbsolutePath else {
                    throw NSError(domain: "AudioSorter", code: 5, userInfo: [NSLocalizedDescriptionKey: "Choose an absolute directory path or a path starting with ~/."])
                }
            }
            let values = try Configuration.expand(updated.sourceFolder).resourceValues(forKeys: [.isDirectoryKey, .isReadableKey])
            guard values.isDirectory == true, values.isReadable == true else {
                throw NSError(domain: "AudioSorter", code: 5, userInfo: [NSLocalizedDescriptionKey: "The source must be an existing, readable directory."])
            }
            if try sorter.applySettings(updated) { close() }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Cannot save settings"
            if let error = error as? DecodingError, case .dataCorrupted(let context) = error {
                alert.informativeText = context.debugDescription
            } else { alert.informativeText = error.localizedDescription }
            alert.beginSheetModal(for: window!)
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = Sorter()
app.delegate = delegate
app.run()
