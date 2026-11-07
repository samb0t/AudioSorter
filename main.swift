import Cocoa
import AVFoundation

struct Configuration: Decodable {
    var sourceFolder: String = "~/Downloads"
    var destinationRoot: String
    var destinations: [String]
    var startAtThirtyPercent: Bool = true

    enum CodingKeys: String, CodingKey { case sourceFolder, destinationRoot, destinations, startAtThirtyPercent }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sourceFolder = try values.decodeIfPresent(String.self, forKey: .sourceFolder) ?? "~/Downloads"
        destinationRoot = try values.decode(String.self, forKey: .destinationRoot)
        destinations = try values.decode([String].self, forKey: .destinations)
        startAtThirtyPercent = try values.decodeIfPresent(Bool.self, forKey: .startAtThirtyPercent) ?? true
        guard !destinationRoot.isEmpty, (1...9).contains(destinations.count), Set(destinations).count == destinations.count,
              destinations.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." && !$0.contains("/") }) else {
            throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "Configure a destinationRoot and 1–9 unique destination folder names (no slashes)."))
        }
    }

    static func expand(_ path: String) -> URL {
        URL(fileURLWithPath: (path as NSString).expandingTildeInPath, isDirectory: true)
    }
}

final class Sorter: NSObject, NSApplicationDelegate, NSTableViewDataSource, NSTableViewDelegate, NSWindowDelegate {
    var configuration: Configuration!
    var names: [String] { configuration.destinations }
    var destinationRoot: URL { Configuration.expand(configuration.destinationRoot) }
    var files: [URL] = []
    var index = -1
    var player: AVAudioPlayer?
    var originalComment = ""
    var commentReadable = true
    var loading = false
    var undoMoves: [(from: URL, to: URL)] = []
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

    func applicationDidFinishLaunching(_ notification: Notification) {
        makeMenu()
        do {
            let path = try String(contentsOf: Bundle.main.resourceURL!.appendingPathComponent("config-path"), encoding: .utf8)
            configuration = try JSONDecoder().decode(Configuration.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
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
        let toolbar = NSStackView(views: [open, recursive])
        toolbar.spacing = 18
        root.addArrangedSubview(toolbar)
        folderLabel.lineBreakMode = .byTruncatingMiddle
        folderLabel.textColor = .secondaryLabelColor
        root.addArrangedSubview(folderLabel)
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("track"))
        column.title = "MP3s — ↑ / ↓ select, Enter plays"
        table.addTableColumn(column)
        table.delegate = self
        table.dataSource = self
        table.rowHeight = 25
        table.allowsEmptySelection = true
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
        root.addArrangedSubview(NSTextField(labelWithString: "← / →  scrub 5s    •    Shift + ← / →  30s    •    N  preview next    •    E  edit comment"))
        let commentHeader = NSStackView(views: [NSTextField(labelWithString: "Embedded MP3 comment"), button("Save & return  ⌘↵", #selector(saveAndReturn))])
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
        let destinations = NSStackView()
        destinations.spacing = 6
        destinations.distribution = .fillEqually
        for (i, name) in names.enumerated() {
            let b = button("\(i + 1)  \(name)", #selector(moveButton(_:)))
            b.tag = i
            destinations.addArrangedSubview(b)
        }
        root.addArrangedSubview(destinations)
        root.addArrangedSubview(NSTextField(labelWithString: "1–\(names.count)  MOVE + play next    •    ⌘number  save + move from editor    •    ⌘Z  undo move"))
        status.textColor = .secondaryLabelColor
        status.lineBreakMode = .byTruncatingMiddle
        root.addArrangedSubview(status)
        for view in [folderLabel, list, title, transport, commentScroll, destinations, status] {
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
        appMenu.addItem(withTitle: "Quit Audio Sorter", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
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

    func handle(_ event: NSEvent) -> Bool {
        guard NSApp.modalWindow == nil, event.window === window else { return false }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        if flags.contains(.command) {
            if let number = Int(key), (1...names.count).contains(number), !flags.contains(.shift), !flags.contains(.option), !flags.contains(.control) {
                if !event.isARepeat { move(number - 1) }
                return true
            }
            if key == "o" { chooseFolder(); return true }
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

    func loadFolder(_ url: URL, autoplay: Bool) {
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
            player?.stop()
            source = url
            UserDefaults.standard.set(url.path, forKey: "source")
            folderLabel.stringValue = url.path
            files = found
            index = -1
            loading = true
            table.reloadData()
            loading = false
            if files.isEmpty { clear(); status.stringValue = "No MP3s found. Choose another folder or include subfolders." }
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
            try FileManager.default.moveItem(at: original, to: target)
            undoMoves.append((original, target))
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
            status.stringValue = "Moved to \(names[destination]). ⌘Z undoes the move.\(oldIndex >= files.count ? " End of list." : "")"
        } catch {
            player?.currentTime = position
            if wasPlaying { player?.play() }
            report(error)
        }
    }

    func undoMove() {
        guard let last = undoMoves.last, saveComment() else { return }
        do {
            try FileManager.default.moveItem(at: last.to, to: last.from)
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

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = Sorter()
app.delegate = delegate
app.run()
