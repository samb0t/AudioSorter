import Cocoa
import AVFoundation
import Accelerate

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
    // Bar edits are saved beside the original as "<name> - <editSuffix>.<ext>".
    var editSuffix: String = "Edit"
    // Regenerated from the recorded grids and cues after every change; Rekordbox imports it.
    var rekordboxXML: String = "~/Music/Audio Sorter/rekordbox.xml"

    enum CodingKeys: String, CodingKey { case sourceFolder, destinationRoot, destinations, startAtThirtyPercent, editSuffix, rekordboxXML }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sourceFolder = try values.decodeIfPresent(String.self, forKey: .sourceFolder) ?? "~/Downloads"
        destinationRoot = try values.decode(String.self, forKey: .destinationRoot)
        destinations = try values.decode([Destination].self, forKey: .destinations)
        startAtThirtyPercent = try values.decodeIfPresent(Bool.self, forKey: .startAtThirtyPercent) ?? true
        editSuffix = try values.decodeIfPresent(String.self, forKey: .editSuffix) ?? "Edit"
        rekordboxXML = try values.decodeIfPresent(String.self, forKey: .rekordboxXML) ?? "~/Music/Audio Sorter/rekordbox.xml"
        guard !rekordboxXML.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !rekordboxXML.hasSuffix("/") else {
            throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "The Rekordbox XML path must name a file."))
        }
        guard !editSuffix.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !editSuffix.contains("/"), !editSuffix.hasPrefix(".") else {
            throw DecodingError.dataCorrupted(.init(codingPath: [], debugDescription: "The edit suffix must be nonempty, contain no slashes, and not start with a period."))
        }
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
    let genreLabel = NSTextField(labelWithString: "")
    let folderLabel = NSTextField(labelWithString: "")
    let status = NSTextField(labelWithString: "No files are moved until you press a destination key.")
    let time = NSTextField(labelWithString: "0:00 / 0:00")
    let waveform = WaveformView()
    // Waveform levels per file. Cancelling the job stops an in-flight decode when the selection moves on.
    var waveformCache: [URL: [Float]] = [:]
    var waveformJob: CancellationFlag?
    let waveformQueue = DispatchQueue(label: "AudioSorter.waveform", qos: .userInitiated)
    let comment = NSTextView()
    let recursive = NSButton(checkboxWithTitle: "Include subfolders", target: nil, action: nil)
    var source: URL?
    var configurationURL: URL!
    var settingsController: SettingsController?
    var lintController: LintController?
    var editorController: EditorController?
    // Grids and cuts set in the editor, remembered for the session.
    var editSessions: [URL: EditSession] = [:]
    // Grids and cues recorded for Rekordbox, kept across launches; see GridStore.
    lazy var gridStore = GridStore(url: GridStore.defaultURL)
    var recordsController: RecordsController?
    let exportQueue = DispatchQueue(label: "AudioSorter.rekordbox", qos: .utility)
    var exporting = false
    var exportPending = false
    // The last export's report from rekordbox.py (tracks, missing, failed), or why it failed.
    var exportResult: Result<[String: Any], Error>?
    // Records that a sort created (by destination key): undoing that move removes them again.
    var sortRecords = Set<String>()
    // Main-thread gate: existing sorting/comment/undo operations are synchronous.
    // Set before launching the worker, release only after every helper has exited.
    var repairingGenres = false
    // Genre column values, read lazily for visible rows. Bumping the generation drops in-flight reads.
    var genreCache: [URL: String] = [:]
    var genreGeneration = 0
    var genrePending = Set<URL>()
    let genreQueue = DispatchQueue(label: "AudioSorter.genre", qos: .utility)
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
        let toolbar = NSStackView(views: [open, recursive, revealButton, button("Cut bars…  B", #selector(showEditor)), button("Settings…", #selector(showSettings))])
        toolbar.spacing = 18
        root.addArrangedSubview(toolbar)
        folderLabel.lineBreakMode = .byTruncatingMiddle
        folderLabel.textColor = .secondaryLabelColor
        let folderRow = NSStackView(views: [folderLabel, button("Refresh  ⌘R", #selector(refreshFolder))])
        folderRow.spacing = 12
        root.addArrangedSubview(folderRow)
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("track"))
        column.title = "Audio (MP3, WAV, AIFF) — J / K or ↓ / ↑ select, Enter plays"
        column.minWidth = 200
        table.addTableColumn(column)
        let genreColumn = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("genre"))
        genreColumn.title = "Genre"
        genreColumn.width = 170
        genreColumn.minWidth = 80
        table.addTableColumn(genreColumn)
        // Resizing the window grows the file column; Genre keeps its width (still draggable).
        table.columnAutoresizingStyle = .firstColumnOnlyAutoresizingStyle
        table.delegate = self
        table.dataSource = self
        table.rowHeight = 25
        table.allowsEmptySelection = true
        let trackMenu = NSMenu()
        trackMenu.delegate = self
        let reveal = trackMenu.addItem(withTitle: "Show in Finder", action: #selector(showInFinder(_:)), keyEquivalent: "")
        reveal.target = self
        trackMenu.addItem(.separator())
        let trash = trackMenu.addItem(withTitle: "Move to Trash…", action: #selector(confirmTrash(_:)), keyEquivalent: "")
        trash.target = self
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
        genreLabel.textColor = .secondaryLabelColor
        genreLabel.lineBreakMode = .byTruncatingTail
        genreLabel.isHidden = true
        root.addArrangedSubview(genreLabel)
        waveform.onSeek = { [weak self] fraction in self?.seek(to: fraction) }
        waveform.onStep = { [weak self] seconds in self?.scrub(seconds) }
        waveform.setContentHuggingPriority(.defaultLow - 1, for: .horizontal)
        // Monospaced digits plus room for "00:00 / 00:00" keep the waveform from shifting as time ticks.
        time.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        time.alignment = .right
        let widestTime = ("00:00 / 00:00" as NSString).size(withAttributes: [.font: time.font!]).width
        time.widthAnchor.constraint(greaterThanOrEqualToConstant: ceil(widestTime) + 6).isActive = true
        let transport = NSStackView(views: [button("Play / pause  ↵", #selector(playPause)), waveform, time])
        transport.spacing = 12
        root.addArrangedSubview(transport)
        root.addArrangedSubview(NSTextField(labelWithString: "H / L or ← / →  scrub 5s    •    Shift  30s    •    J / K  down/up    •    N  next    •    E  comment"))
        let commentHeader = NSStackView(views: [NSTextField(labelWithString: "Comment • ⌘H/L scrub • Shift 30s • ⌘J/K down/up"), button("Save & return  ⌘↵", #selector(saveAndReturn))])
        commentHeader.spacing = 20
        root.addArrangedSubview(commentHeader)
        comment.isRichText = false
        comment.toolTip = "MP3/AIFF: ID3 comment. WAV: RIFF INFO ICMT (ASCII edits only); existing WAV ID3 comments are preserved separately."
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
        for view in [folderRow, list, title, genreLabel, transport, commentScroll, destinations, status] {
            view.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        }
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handle(event) == true ? nil : event
        }
        timer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in self?.updateTime() }
        let initial = UserDefaults.standard.string(forKey: "source") ?? configuration.sourceFolder
        loadFolder(Configuration.expand(initial), autoplay: false)
        // Files renamed or deleted in Finder since the last launch drop out of the XML now.
        if !gridStore.contents.tracks.isEmpty || gridStore.loadError != nil {
            let missing = gridStore.contents.tracks.keys.filter { gridStore.status($0) != .current }.count
            if gridStore.loadError != nil { status.stringValue += "  •  The Rekordbox record store cannot be read; see File → Rekordbox Records…" }
            else if missing > 0 { status.stringValue += "  •  \(missing) Rekordbox record\(missing == 1 ? "" : "s") point to missing files; see File → Rekordbox Records…" }
            exportRekordbox()
        }
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
        let trash = fileMenu.addItem(withTitle: "Move to Trash…", action: #selector(confirmTrash(_:)), keyEquivalent: "\u{8}")
        trash.target = self
        fileMenu.addItem(.separator())
        let cut = fileMenu.addItem(withTitle: "Cut Bars…", action: #selector(showEditor), keyEquivalent: "b")
        cut.target = self
        let lint = fileMenu.addItem(withTitle: "Check destination tags…", action: #selector(checkDestinationTags), keyEquivalent: "")
        lint.target = self
        let records = fileMenu.addItem(withTitle: "Rekordbox Records…", action: #selector(showRecords), keyEquivalent: "")
        records.target = self
        fileMenu.addItem(.separator())
        fileMenu.addItem(withTitle: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
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
        guard menu === table.menu else { return }
        for item in menu.items where !item.isSeparatorItem {
            item.representedObject = files.indices.contains(table.clickedRow) ? files[table.clickedRow] : nil
        }
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        if menuItem.action == #selector(refreshFolder) {
            return window?.isKeyWindow == true && NSApp.modalWindow == nil && source != nil
        }
        if menuItem.action == #selector(showEditor) {
            return window?.isKeyWindow == true && NSApp.modalWindow == nil && files.indices.contains(index)
        }
        guard menuItem.action == #selector(showInFinder(_:)) || menuItem.action == #selector(confirmTrash(_:)) else { return true }
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

    @objc func showEditor() {
        guard files.indices.contains(index), saveComment() else { return }
        player?.pause()
        if editorController == nil { editorController = EditorController(sorter: self) }
        editorController?.open(files[index])
    }

    func editURL(for url: URL) -> URL {
        let name = url.deletingPathExtension().lastPathComponent + " - " + configuration.editSuffix
        return url.deletingLastPathComponent().appendingPathComponent(name).appendingPathExtension(url.pathExtension)
    }

    // Back to sorting: the editor closes (its session is kept) and the new edit is selected,
    // ready for a destination key. The original stays in the list just above or below it.
    func editSaved(_ output: URL) {
        editorController?.close()
        window?.makeKeyAndOrderFront(nil)
        if source != nil { refreshFolder() }
        // Folder listings can spell a path through a symlink differently (/var vs /private/var).
        let target = output.resolvingSymlinksInPath().path
        if let row = files.firstIndex(where: { $0.resolvingSymlinksInPath().path == target }) {
            select(row, autoplay: false)
            window?.makeFirstResponder(table)
            status.stringValue = "Saved and selected \(output.lastPathComponent). Press 1–\(names.count) to move it; the original is unchanged and still listed."
        } else {
            status.stringValue = "Saved \(output.lastPathComponent) beside the original; it is outside the listed folder. The original is unchanged."
        }
    }

    @objc func showRecords() {
        if recordsController == nil { recordsController = RecordsController(sorter: self) }
        recordsController?.present()
    }

    var rekordboxXML: URL { URL(fileURLWithPath: (configuration.rekordboxXML as NSString).expandingTildeInPath) }

    // The beat_this model for grid detection, present when the app was built with --detect.
    var detectModel: URL? {
        guard let file = Bundle.main.resourceURL?.appendingPathComponent("detect-model"),
              let path = try? String(contentsOf: file, encoding: .utf8), FileManager.default.fileExists(atPath: path) else { return nil }
        return URL(fileURLWithPath: path)
    }

    func recordGrid(_ url: URL, grid: BeatGrid, cues: [Double], duration: Double, sampleRate: Double) throws {
        try gridStore.set(url, grid: grid, cues: cues, duration: duration, sampleRate: sampleRate)
        exportRekordbox()
    }

    // What ⌘⇧S records for an original: the session's grid, with its cues mapped back to where
    // they play in the original. Returns how many cues were recorded and how many were left out.
    func recordOriginal(_ url: URL, session: EditSession, rate: Double, frames: Int64) throws -> (cues: Int, dropped: Int) {
        let timeline = EditTimeline(session: session, rate: rate, frames: frames)
        let duration = Double(frames) / rate
        let mapped = session.cueTimes(duration).map { timeline.uniqueSourceTime($0) }
        let cues = mapped.compactMap { $0 }
        try recordGrid(url, grid: session.grid, cues: cues, duration: duration, sampleRate: rate)
        return (cues.count, mapped.count - cues.count)
    }

    static func droppedNote(_ dropped: Int) -> String {
        guard dropped > 0 else { return "" }
        return " Left out \(dropped) cue\(dropped == 1 ? "" : "s") in duplicated audio, which has no single place in the original; save the edit (⌘S) to keep \(dropped == 1 ? "it" : "them")."
    }

    // Sorting a track whose downbeat was set in Cut Bars records its grid and cues, as ⌘⇧S
    // would: sending it to a folder is the sign the work on it is done.
    func recordSession(for url: URL) -> String {
        editorController?.storeSession()
        guard var session = editSessions[url], session.downbeatSet else { return "" }
        do {
            let file = try AVAudioFile(forReading: url)
            let recorded = try recordOriginal(url, session: session, rate: file.processingFormat.sampleRate, frames: file.length)
            session.savedCues = session.cues
            editSessions[url] = session
            if editorController?.url == url { editorController?.session.savedCues = session.cues }
            return " Recorded its grid and \(recorded.cues) memory cue\(recorded.cues == 1 ? "" : "s") for Rekordbox." + Self.droppedNote(recorded.dropped)
        } catch {
            return " Grid not recorded for Rekordbox: \(error.localizedDescription)"
        }
    }

    // The editor's session for a file follows it when the app moves it.
    func moveSession(from old: URL, to new: URL) {
        if let session = editSessions.removeValue(forKey: old) { editSessions[new] = session }
        if editorController?.url == old {
            editorController?.url = new
            editorController?.window?.title = "Cut Bars — \(new.lastPathComponent)"
        }
    }

    // Keeps a record on its file when the app moves or trashes it. A failure is reported
    // after the move, never blocking it; the record then shows as missing.
    func updateRecords(_ change: () throws -> Bool) -> String {
        do {
            if try change() { exportRekordbox() }
            return ""
        } catch { return " Rekordbox record not updated: \(error.localizedDescription)" }
    }

    // Regenerates the whole XML in the background. Changes during an export queue one more.
    func exportRekordbox() {
        guard !exporting else { exportPending = true; return }
        exporting = true
        let store = gridStore.url, output = rekordboxXML
        exportQueue.async { [self] in
            let result = Result { () throws -> [String: Any] in
                let request = String(decoding: try JSONSerialization.data(withJSONObject: ["output": output.path]), as: UTF8.self)
                return try helper("rekordbox-xml", file: store, text: request)
            }
            DispatchQueue.main.async { [self] in
                exporting = false
                exportResult = result
                recordsController?.reload()
                if exportPending {
                    exportPending = false
                    exportRekordbox()
                }
            }
        }
    }

    @objc func checkDestinationTags() {
        if lintController == nil { lintController = LintController(sorter: self) }
        lintController?.present()
    }

    func applySettings(_ updated: Configuration) throws -> Bool {
        guard saveComment() else { return false }
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try encoder.encode(updated).write(to: configurationURL, options: .atomic)
        let sourceChanged = configuration.sourceFolder != updated.sourceFolder
        let xmlChanged = configuration.rekordboxXML != updated.rekordboxXML
        configuration = updated
        refreshDestinations()
        if xmlChanged { exportRekordbox() }
        if sourceChanged {
            // A configured source takes precedence over the last ad-hoc folder, even after restart.
            UserDefaults.standard.removeObject(forKey: "source")
            loadFolder(Configuration.expand(updated.sourceFolder), autoplay: false)
        } else {
            status.stringValue = "Settings saved. Destination and Genre changes apply to future moves and confirmed Genre repairs."
        }
        return true
    }

    func handle(_ event: NSEvent) -> Bool {
        guard NSApp.modalWindow == nil, event.window === window else { return false }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        if flags.contains(.command) {
            if !flags.contains(.shift), !flags.contains(.option), !flags.contains(.control), key == "j" || key == "k" {
                select(index + (key == "j" ? 1 : -1), autoplay: false)
                return true
            }
            if !flags.contains(.option), !flags.contains(.control), key == "h" || key == "l" {
                let seconds: Double = flags.contains(.shift) ? 30 : 5
                scrub(key == "h" ? -seconds : seconds)
                return true
            }
            if let number = Int(key), (1...names.count).contains(number), !flags.contains(.shift), !flags.contains(.option), !flags.contains(.control) {
                if !event.isARepeat { move(number - 1) }
                return true
            }
            if event.keyCode == 51, !flags.contains(.shift), !flags.contains(.option), !flags.contains(.control) {
                if !event.isARepeat { confirmTrash(nil) }
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
        if key == "b" { showEditor(); return true }
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
                return ["mp3", "wav", "aif", "aiff"].contains($0.pathExtension.lowercased()) && values.isRegularFile == true && values.isSymbolicLink != true
            }.sorted { $0.path.localizedStandardCompare($1.path) == .orderedAscending }
            let current = preservingCurrent && files.indices.contains(index) ? files[index] : nil
            let retainedIndex = current.flatMap { found.firstIndex(of: $0) }
            if retainedIndex == nil { player?.stop() }
            source = url
            UserDefaults.standard.set(url.path, forKey: "source")
            folderLabel.stringValue = url.path
            files = found
            invalidateGenres()
            index = retainedIndex ?? -1
            loading = true
            table.reloadData()
            loading = false
            if retainedIndex != nil {
                refreshGenre()
                highlight()
                status.stringValue = "Refreshed. \(files.count) audio files ready. Current track and playback position preserved."
            }
            else if files.isEmpty { clear(); status.stringValue = "No supported audio files found (MP3, WAV, AIFF). Choose another folder or include subfolders." }
            else { select(0, autoplay: autoplay); status.stringValue = "\(files.count) audio files ready. Enter plays; 1–\(names.count) moves the selected file." }
        } catch { report(error) }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { files.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        if tableColumn?.identifier.rawValue == "genre" {
            let url = files[row]
            let field = NSTextField(labelWithString: genreCache[url] ?? "…")
            field.textColor = .secondaryLabelColor
            field.lineBreakMode = .byTruncatingTail
            field.toolTip = genreCache[url]
            if genreCache[url] == nil { loadGenre(url) }
            return field
        }
        let field = NSTextField(labelWithString: files[row].lastPathComponent)
        field.lineBreakMode = .byTruncatingMiddle
        return field
    }

    func genreText(_ data: [String: Any]) -> String {
        let genres = (data["genres"] as? [String] ?? []).filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        return genres.isEmpty ? "—" : genres.joined(separator: " | ")
    }

    func loadGenre(_ url: URL) {
        guard !genrePending.contains(url) else { return }
        genrePending.insert(url)
        let generation = genreGeneration
        genreQueue.async { [self] in
            let text = (try? helper("read-genre", file: url)).map(genreText) ?? "unavailable"
            DispatchQueue.main.async { [self] in
                guard generation == genreGeneration else { return }
                genrePending.remove(url)
                genreCache[url] = text
                if let row = files.firstIndex(of: url) {
                    table.reloadData(forRowIndexes: IndexSet(integer: row), columnIndexes: IndexSet(integer: 1))
                }
            }
        }
    }

    func invalidateGenres() {
        genreGeneration += 1
        genreCache.removeAll()
        genrePending.removeAll()
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
        refreshGenre()
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
        loadWaveform(files[index])
        highlight()
        updateTime()
    }

    func refreshGenre() {
        genreLabel.stringValue = ""
        genreLabel.toolTip = nil
        genreLabel.isHidden = true
        guard files.indices.contains(index) else { return }
        do {
            let data = try helper("read-genre", file: files[index])
            let genres = (data["genres"] as? [String] ?? []).filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            guard !genres.isEmpty else { return }
            genreLabel.stringValue = "Genre: " + genres.joined(separator: " | ")
            genreLabel.toolTip = data["warning"] as? String ?? genreLabel.stringValue
            genreCache[files[index]] = genreText(data)
            table.reloadData(forRowIndexes: IndexSet(integer: index), columnIndexes: IndexSet(integer: 1))
        } catch {
            // A genre read failure must not prevent playback or comment editing.
            genreLabel.stringValue = "Genre: unavailable"
            genreLabel.toolTip = error.localizedDescription
        }
        genreLabel.isHidden = false
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
            throw NSError(domain: "AudioSorter", code: 2, userInfo: [NSLocalizedDescriptionKey: result["error"] as? String ?? "Cannot access audio tags. Try rebuilding the app."])
        }
        return result
    }

    @discardableResult func saveComment() -> Bool {
        guard !repairingGenres else {
            status.stringValue = "Genre repair is running. File changes are temporarily unavailable; pending comments are unchanged."
            return false
        }
        guard files.indices.contains(index), comment.string != originalComment else { return true }
        guard commentReadable else { return false }
        do {
            _ = try helper("write", file: files[index], text: comment.string)
            originalComment = comment.string
            status.stringValue = "Comment saved to audio file."
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

    // Positions are fractions of the player's duration, so the playhead stays aligned even if
    // AVAudioFile and AVAudioPlayer disagree slightly on a VBR file's length.
    func seek(to fraction: Double) {
        guard let player else { return }
        player.currentTime = min(max(0, fraction), 1) * player.duration
        updateTime()
    }

    func updateTime() {
        let duration = player?.duration ?? 0
        let current = player?.currentTime ?? 0
        waveform.progress = duration > 0 ? current / duration : 0
        func stamp(_ value: Double) -> String { String(format: "%d:%02d", Int(value) / 60, Int(value) % 60) }
        time.stringValue = "\(stamp(current)) / \(stamp(duration))"
        waveform.positionDescription = time.stringValue
    }

    func loadWaveform(_ url: URL?) {
        waveformJob?.cancel()
        waveformJob = nil
        waveform.levels = url.flatMap { waveformCache[$0] } ?? []
        guard let url, waveformCache[url] == nil else { return }
        let job = CancellationFlag()
        waveformJob = job
        waveformQueue.async { [weak self] in
            // Unreadable audio simply leaves the plain playback line; playback errors are reported elsewhere.
            guard let levels = waveformLevels(url, cancelled: { job.isCancelled }) else { return }
            DispatchQueue.main.async {
                guard let self, !job.isCancelled else { return }
                if self.waveformCache.count >= 200 { self.waveformCache.removeAll() }
                self.waveformCache[url] = levels
                self.waveform.levels = levels
            }
        }
    }

    func next() {
        if index + 1 < files.count { select(index + 1, autoplay: true) }
        else { if saveComment() { player?.stop(); status.stringValue = "End of list. Use ↑ to revisit tracks or open another folder." } }
    }

    @objc func moveButton(_ sender: NSButton) { move(sender.tag) }

    @objc func confirmTrash(_ sender: Any?) {
        guard window.isKeyWindow, NSApp.modalWindow == nil else { return }
        let url: URL
        if let item = sender as? NSMenuItem, item.menu === table.menu {
            guard let clickedURL = item.representedObject as? URL else { return }
            url = clickedURL
        } else {
            guard files.indices.contains(index) else { return }
            url = files[index]
        }
        guard files.contains(url) else { return }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Move “\(url.lastPathComponent)” to Trash?"
        alert.informativeText = "\(url.path)\n\nYou can recover it from Trash in Finder. This cannot be undone with the app’s Undo Move command. Any pending comment on this track will be saved first."
        alert.addButton(withTitle: "Cancel").keyEquivalent = "\r"
        let confirm = alert.addButton(withTitle: "Move to Trash")
        confirm.keyEquivalent = ""
        confirm.hasDestructiveAction = true
        guard alert.runModal() == .alertSecondButtonReturn else { return }
        _ = trashFile(url)
    }

    func sendToTrash(_ url: URL) throws {
        try FileManager.default.trashItem(at: url, resultingItemURL: nil)
    }

    @discardableResult func trashFile(_ url: URL) -> Bool {
        guard !repairingGenres else { return false }
        guard let removedIndex = files.firstIndex(of: url) else { return false }
        let current = files.indices.contains(index) ? files[index] : nil
        let deletingCurrent = current == url
        do {
            let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            guard values.isRegularFile == true, values.isSymbolicLink != true else {
                throw NSError(domain: "AudioSorter", code: 7, userInfo: [NSLocalizedDescriptionKey: "Refusing to trash a non-regular file or symbolic link. Refresh the source folder."])
            }
            if deletingCurrent && !saveComment() { return false }
            let wasPlaying = player?.isPlaying ?? false
            let recordKey = GridStore.key(url)
            // Do not stop playback or alter the queue until Trash succeeds.
            try sendToTrash(url)
            let recordNote = updateRecords { try gridStore.markTrashed(recordKey) }
            files.remove(at: removedIndex)
            if deletingCurrent {
                player?.stop()
                player = nil
                index = -1
                originalComment = ""
                comment.string = ""
            } else {
                index = current.flatMap { files.firstIndex(of: $0) } ?? -1
            }
            loading = true
            table.reloadData()
            loading = false
            if deletingCurrent {
                if files.isEmpty { clear() }
                else { select(min(removedIndex, files.count - 1), autoplay: wasPlaying) }
            } else { highlight() }
            status.stringValue = "Moved \(url.lastPathComponent) to Trash. Recover it in Finder; ⌘Z only undoes sorting moves.\(recordNote)"
            return true
        } catch { report(error); return false }
    }

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
            let recordKey = GridStore.key(original)
            let sessionNote = recordSession(for: original)
            let restore = try moveTaggedFile(from: original, to: target, change: genre.map { ["genre": $0] })
            undoMoves.append((original, target, restore))
            moveSession(from: original, to: target)
            let recordNote = sessionNote + updateRecords {
                if try gridStore.sorted(from: recordKey, to: target) { sortRecords.insert(GridStore.key(target)) }
                return true
            }
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
            status.stringValue = "Moved to \(names[destination]).\(tagged) ⌘Z undoes move/tag.\(oldIndex >= files.count ? " End of list." : "")\(recordNote)"
        } catch {
            player?.currentTime = position
            if wasPlaying { player?.play() }
            report(error)
        }
    }

    func undoMove() {
        guard let last = undoMoves.last, saveComment() else { return }
        do {
            let recordKey = GridStore.key(last.to)
            _ = try moveTaggedFile(from: last.to, to: last.from, change: last.genreRestore)
            undoMoves.removeLast()
            moveSession(from: last.to, to: last.from)
            let recordNote = updateRecords {
                // A grid recorded since the sort stays with the file.
                guard sortRecords.remove(recordKey) != nil, gridStore.contents.tracks[recordKey]?.hasGrid == false else {
                    return try gridStore.move(from: recordKey, to: last.from)
                }
                try gridStore.forget([recordKey])
                return true
            }
            invalidateGenres()
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
            status.stringValue = "Restored \(last.from.lastPathComponent). Comment edits are not undone.\(recordNote)"
        } catch { report(error) }
    }

    func clear() {
        player?.stop()
        player = nil
        index = -1
        refreshGenre()
        originalComment = ""
        comment.string = ""
        title.stringValue = "End of list"
        loadWaveform(nil)
        highlight()
        updateTime()
    }

    func report(_ error: Error) {
        status.stringValue = error.localizedDescription
        let alert = NSAlert(error: error)
        alert.runModal()
    }

    // Tracks whose bar edits changed since they were last saved, including the open editor's.
    func unsavedEdits() -> [URL] {
        editorController?.storeSession()
        return editSessions.filter { $0.value.unsaved }.map(\.key).sorted { $0.path.localizedStandardCompare($1.path) == .orderedAscending }
    }

    // Closing the main window quits the app, so both paths ask; once confirmed, never twice.
    var discardConfirmed = false
    func confirmDiscardingEdits() -> Bool {
        guard !discardConfirmed else { return true }
        let unsaved = unsavedEdits()
        guard unsaved.isEmpty || askToDiscard(unsaved) else { return false }
        discardConfirmed = !unsaved.isEmpty
        return true
    }

    func askToDiscard(_ unsaved: [URL]) -> Bool {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = unsaved.count == 1 ? "Quit without saving your bar edit or cues?" : "Quit without saving bar edits or cues on \(unsaved.count) tracks?"
        alert.informativeText = unsaved.map(\.lastPathComponent).joined(separator: "\n")
            + "\n\nUnsaved edits and cues only last while the app is open. To keep them, cancel, select the track, press B, then ⌘S to save the edit, or ⌘⇧S to record the grid and cues."
        alert.addButton(withTitle: "Cancel").keyEquivalent = "\r"
        let quit = alert.addButton(withTitle: "Quit Anyway")
        quit.keyEquivalent = ""
        quit.hasDestructiveAction = true
        return alert.runModal() == .alertSecondButtonReturn
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool { saveComment() && confirmDiscardingEdits() }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        saveComment() && confirmDiscardingEdits() ? .terminateNow : .terminateCancel
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

final class CancellationFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false
    var isCancelled: Bool { lock.withLock { cancelled } }
    func cancel() { lock.withLock { cancelled = true } }
}

// Loudness (RMS) per bin, normalized to the loudest bin. Peaks of modern masters sit near
// full scale throughout, so RMS is what makes breakdowns show up as dips.
func waveformLevels(_ url: URL, bins: Int = 1200, cancelled: () -> Bool = { false }) -> [Float]? {
    guard bins > 0, let file = try? AVAudioFile(forReading: url), file.length > 0,
          let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 65_536) else { return nil }
    let channels = Int(file.processingFormat.channelCount)
    let length = file.length
    var sums = [Double](repeating: 0, count: bins)
    var counts = [Int](repeating: 0, count: bins)
    var position: Int64 = 0
    while !cancelled() {
        do { try file.read(into: buffer) } catch { if position == 0 { return nil }; break }
        let frames = Int(buffer.frameLength)
        guard frames > 0, let data = buffer.floatChannelData else { break }
        var offset = 0
        while offset < frames {
            let frame = position + Int64(offset)
            let bin = min(bins - 1, Int(frame * Int64(bins) / length))
            // A file can decode a few frames past its reported length; those land in the last bin.
            let binEnd = bin == bins - 1 ? Int64.max : (Int64(bin + 1) * length + Int64(bins) - 1) / Int64(bins)
            let count = Int(min(Int64(frames - offset), binEnd - frame))
            for channel in 0..<channels {
                var squares: Float = 0
                vDSP_svesq(data[channel] + offset, 1, &squares, vDSP_Length(count))
                sums[bin] += Double(squares)
            }
            counts[bin] += count * channels
            offset += count
        }
        position += Int64(frames)
    }
    if cancelled() { return nil }
    let levels = zip(sums, counts).map { $1 > 0 ? Float(($0 / Double($1)).squareRoot()) : 0 }
    let loudest = levels.max() ?? 0
    return loudest > 0 ? levels.map { $0 / loudest } : levels
}

// SoundCloud-style scrubber: played bars take the accent color; click or drag to seek.
final class WaveformView: NSView, NSAccessibilitySlider {
    var levels: [Float] = [] { didSet { needsDisplay = true } }
    var progress: Double = 0 { didSet { if progress != oldValue { needsDisplay = true } } }
    var positionDescription = ""
    var onSeek: ((Double) -> Void)?
    var onStep: ((Double) -> Void)?

    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: 56) }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { seek(event) }
    override func mouseDragged(with event: NSEvent) { seek(event) }

    func fraction(at x: CGFloat) -> Double {
        bounds.width > 0 ? Double(min(max(0, x / bounds.width), 1)) : 0
    }

    func seek(_ event: NSEvent) { onSeek?(fraction(at: convert(event.locationInWindow, from: nil).x)) }

    override func draw(_ dirtyRect: NSRect) {
        let middle = bounds.midY
        let playhead = bounds.width * CGFloat(progress)
        if levels.isEmpty {
            NSColor.secondaryLabelColor.setFill()
            NSRect(x: 0, y: middle - 0.5, width: bounds.width, height: 1).fill()
        } else {
            let step: CGFloat = 3, bar: CGFloat = 2
            let columns = max(1, Int(bounds.width / step))
            for column in 0..<columns {
                let start = column * levels.count / columns
                let end = min(levels.count, max(start + 1, (column + 1) * levels.count / columns))
                let height = max(1, CGFloat(levels[start..<end].max() ?? 0) * (bounds.height - 2))
                let x = CGFloat(column) * step
                (x + bar / 2 <= playhead ? NSColor.controlAccentColor : NSColor.secondaryLabelColor).setFill()
                NSRect(x: x, y: middle - height / 2, width: bar, height: height).fill()
            }
        }
        NSColor.labelColor.setFill()
        NSRect(x: min(playhead, bounds.width - 1), y: 0, width: 1, height: bounds.height).fill()
    }

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .slider }
    override func accessibilityLabel() -> String? { "Playback position" }
    override func accessibilityValue() -> Any? { positionDescription }
    override func accessibilityPerformIncrement() -> Bool { onStep?(5); return onStep != nil }
    override func accessibilityPerformDecrement() -> Bool { onStep?(-5); return onStep != nil }
}

final class LintController: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {
    unowned let sorter: Sorter
    let summary = NSTextField(wrappingLabelWithString: "")
    let rootLabel = NSTextField(labelWithString: "")
    let table = NSTableView()
    let scanButton = NSButton(title: "Check again", target: nil, action: nil)
    let revealButton = NSButton(title: "Show in Finder", target: nil, action: nil)
    let fixButton = NSButton(title: "Repair selected Genres…", target: nil, action: nil)
    let wavGenreConflict = "Conflicting WAV RIFF INFO / ID3 Genre (Rekordbox reads IGNR)"
    let activityIndicator = NSProgressIndicator()
    let repairHint = NSTextField(wrappingLabelWithString: "Select Missing Genre or WAV RIFF/ID3 conflicts to repair. Genre mismatches and unreadable tags are report-only.")
    let repairReport = NSTextView()
    var issues: [[String: Any]] = []
    var scanning = false

    init(sorter: Sorter) {
        self.sorter = sorter
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 640), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Check destination tags"
        window.minSize = NSSize(width: 760, height: 560)
        window.isReleasedWhenClosed = false
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 12
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 20),
            content.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -20),
            content.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 20),
            content.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -20)
        ])
        content.addArrangedSubview(NSTextField(labelWithString: "Read-only scan • Includes subfolders • Repairs require confirmation"))
        rootLabel.lineBreakMode = .byTruncatingMiddle
        rootLabel.textColor = .secondaryLabelColor
        content.addArrangedSubview(rootLabel)
        activityIndicator.style = .spinning
        activityIndicator.controlSize = .small
        activityIndicator.isIndeterminate = true
        activityIndicator.isDisplayedWhenStopped = false
        activityIndicator.setAccessibilityLabel("Checking destination tags")
        let scanStatus = NSStackView(views: [activityIndicator, summary])
        scanStatus.spacing = 8
        content.addArrangedSubview(scanStatus)
        for (identifier, heading, width) in [("file", "File / directory", 380.0), ("expected", "Expected Genre", 140.0), ("actual", "Actual Genre", 140.0), ("problem", "Issue", 240.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
            column.title = heading
            column.width = width
            table.addTableColumn(column)
        }
        table.delegate = self
        table.dataSource = self
        table.rowHeight = 26
        table.allowsMultipleSelection = true
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = true
        scroll.borderType = .bezelBorder
        content.addArrangedSubview(scroll)
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true
        scanButton.target = self
        scanButton.action = #selector(scan)
        revealButton.target = self
        revealButton.action = #selector(reveal)
        revealButton.isEnabled = false
        fixButton.target = self
        fixButton.action = #selector(confirmRepair)
        fixButton.isEnabled = false
        let actions = NSStackView(views: [scanButton, revealButton, fixButton])
        actions.spacing = 12
        content.addArrangedSubview(actions)
        repairHint.textColor = .secondaryLabelColor
        content.addArrangedSubview(repairHint)
        repairReport.isEditable = false
        repairReport.isRichText = false
        repairReport.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        let reportScroll = NSScrollView()
        reportScroll.documentView = repairReport
        reportScroll.hasVerticalScroller = true
        repairReport.isVerticallyResizable = true
        repairReport.isHorizontallyResizable = false
        repairReport.autoresizingMask = [.width]
        repairReport.textContainer?.widthTracksTextView = true
        content.addArrangedSubview(reportScroll)
        reportScroll.heightAnchor.constraint(equalToConstant: 90).isActive = true
        reportScroll.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        for view in [rootLabel, scanStatus, scroll, repairHint] {
            view.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func present() {
        let wasVisible = window?.isVisible == true
        if !wasVisible { window?.center() }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        if !wasVisible { scan() }
    }

    @objc func scan() {
        guard !scanning, !sorter.repairingGenres else { return }
        // Snapshot settings on the main thread; the worker only reads disk.
        let root = sorter.destinationRoot
        let configuration: String
        do {
            configuration = String(decoding: try JSONEncoder().encode(sorter.configuration!), as: UTF8.self)
        } catch {
            summary.stringValue = "Cannot start check: \(error.localizedDescription)"
            return
        }
        scanning = true
        activityIndicator.setAccessibilityLabel("Checking destination tags")
        activityIndicator.startAnimation(nil)
        scanButton.isEnabled = false
        revealButton.isEnabled = false
        fixButton.isEnabled = false
        issues = []
        table.reloadData()
        rootLabel.stringValue = root.path
        summary.stringValue = "Checking destination tags… Playback and sorting remain available."
        updateSelectionStatus()
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let result = Result { try sorter.helper("lint", file: root, text: configuration) }
            DispatchQueue.main.async { [self] in
                scanning = false
                activityIndicator.stopAnimation(nil)
                scanButton.isEnabled = true
                switch result {
                case .success(let report):
                    issues = report["issues"] as? [[String: Any]] ?? []
                    let scanned = report["scanned"] as? Int ?? 0
                    let checked = report["checkedFolders"] as? Int ?? 0
                    let skipped = report["skippedFolders"] as? Int ?? 0
                    summary.stringValue = "\(scanned) audio files checked across \(checked) mapped folders. \(issues.count) issues. \(skipped) folders skipped (no Genre mapping). Results are a snapshot; check again after sorting or changing settings."
                    table.reloadData()
                case .failure(let error):
                    summary.stringValue = "Check failed: \(error.localizedDescription)"
                }
                updateSelectionStatus()
            }
        }
    }

    func numberOfRows(in tableView: NSTableView) -> Int { issues.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        let key = tableColumn?.identifier.rawValue ?? "file"
        let value: String
        if key == "actual" {
            let genres = issues[row]["actual"] as? [String] ?? []
            value = genres.isEmpty ? "—" : genres.joined(separator: " | ")
        } else {
            value = issues[row][key] as? String ?? ""
        }
        let field = NSTextField(labelWithString: value)
        field.lineBreakMode = .byTruncatingMiddle
        field.toolTip = value
        return field
    }

    func tableViewSelectionDidChange(_ notification: Notification) {
        updateSelectionStatus()
    }

    func updateSelectionStatus() {
        revealButton.isEnabled = !scanning && issues.indices.contains(table.selectedRow)
        let eligible = selectedRepairs.count
        fixButton.isEnabled = !scanning && !sorter.repairingGenres && eligible > 0
        if scanning {
            repairHint.stringValue = "Repairs are unavailable while checking files."
        } else if sorter.repairingGenres {
            repairHint.stringValue = "Repairs are in progress. File changes are temporarily unavailable."
        } else if eligible > 0 {
            let conflicts = selectedRepairs.filter { $0["problem"] as? String == wavGenreConflict }.count
            repairHint.stringValue = "\(eligible) selected files eligible (\(conflicts) WAV conflicts). Repairs use Expected Genre; WAV conflicts overwrite both RIFF and ID3 Genres. Other selected issues will be skipped."
        } else if table.selectedRow >= 0 {
            repairHint.stringValue = "No selected files are eligible. Only Missing Genre and WAV RIFF/ID3 conflicts can be repaired; other issues are report-only."
        } else {
            repairHint.stringValue = "Select Missing Genre or WAV RIFF/ID3 conflicts to repair. Genre mismatches and unreadable tags are report-only."
        }
        fixButton.toolTip = repairHint.stringValue
    }

    var selectedRepairs: [[String: Any]] {
        table.selectedRowIndexes.compactMap { row in
            guard issues.indices.contains(row), issues[row]["repairable"] as? Bool == true,
                  let problem = issues[row]["problem"] as? String,
                  problem == "Missing Genre" || problem == wavGenreConflict else { return nil }
            return issues[row]
        }
    }

    @objc func confirmRepair() {
        guard !scanning, !sorter.repairingGenres else { return }
        let findings = selectedRepairs
        guard !findings.isEmpty else { return }
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Repair Genres in \(findings.count) selected files?"
        alert.informativeText = "Use the mapped Expected Genre. Missing Genres will be filled; WAV RIFF/ID3 conflicts will have BOTH genre tags overwritten to match it, even if neither currently matches. Only eligible selected findings are included. Each file and mapping will be revalidated before writing. No files are moved or directories created. This repair has no app undo; the existing Undo Move history is unchanged. Keep backups."
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 650, height: 240))
        let list = NSTextView(frame: scroll.bounds)
        list.isEditable = false
        list.isRichText = false
        list.isVerticallyResizable = true
        list.isHorizontallyResizable = false
        list.autoresizingMask = [.width]
        list.textContainer?.widthTracksTextView = true
        list.string = findings.map {
            let actual = ($0["actual"] as? [String] ?? []).joined(separator: " | ")
            let operation = $0["problem"] as? String == wavGenreConflict ? "Replace RIFF + ID3" : "Fill missing Genre"
            return "\($0["path"] as? String ?? "")\n  Current: \(actual.isEmpty ? "—" : actual)\n  \(operation) → Genre: \($0["expected"] as? String ?? "")"
        }.joined(separator: "\n\n")
        scroll.documentView = list
        scroll.hasVerticalScroller = true
        alert.accessoryView = scroll
        alert.addButton(withTitle: "Cancel").keyEquivalent = "\r"
        alert.addButton(withTitle: "Repair Genres").keyEquivalent = ""
        guard alert.runModal() == .alertSecondButtonReturn else { return }
        startRepair(findings)
    }

    func startRepair(_ findings: [[String: Any]]) {
        guard !scanning, !sorter.repairingGenres, !findings.isEmpty else { return }
        let root = sorter.destinationRoot
        let request: String
        do {
            let data = try JSONSerialization.data(withJSONObject: ["findings": findings, "configurationPath": sorter.configurationURL.path])
            request = String(decoding: data, as: UTF8.self)
        } catch {
            summary.stringValue = "Cannot start repair: \(error.localizedDescription)"
            return
        }
        sorter.repairingGenres = true
        activityIndicator.setAccessibilityLabel("Repairing Genres")
        activityIndicator.startAnimation(nil)
        scanButton.isEnabled = false
        fixButton.isEnabled = false
        summary.stringValue = "Repairing Genres… Playback and pending comment text are unchanged. File changes are temporarily unavailable."
        updateSelectionStatus()
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let result = Result { try sorter.helper("repair-genres", file: root, text: request) }
            DispatchQueue.main.async { [self] in
                activityIndicator.setAccessibilityLabel("Rechecking repaired files")
                summary.stringValue = "Rechecking only the attempted files… Other results will remain unchanged."
            }
            // Recheck even if the repair process failed: some writes may have completed.
            let recheck = Result { try sorter.helper("recheck-genres", file: root, text: request) }
            DispatchQueue.main.async { [self] in
                sorter.repairingGenres = false
                activityIndicator.stopAnimation(nil)
                scanButton.isEnabled = true
                switch result {
                case .success(let report):
                    let results = report["results"] as? [[String: Any]] ?? []
                    let counts = ["success", "skipped", "failed"].map { state in
                        "\(results.filter { $0["status"] as? String == state }.count) \(state)"
                    }.joined(separator: ", ")
                    repairReport.string = counts + "\n" + results.map {
                        "\($0["status"] as? String ?? "failed"): \($0["path"] as? String ?? "") → \($0["genre"] as? String ?? ""): \($0["detail"] as? String ?? "")"
                    }.joined(separator: "\n")
                case .failure(let error):
                    repairReport.string = "Repair process failed: \(error.localizedDescription). See the targeted recheck for current file state."
                }
                // Never refresh the sorting queue, load a track, save comments, or touch undo.
                do {
                    try applyRecheck(try recheck.get(), findings: findings)
                    summary.stringValue = "\(issues.count) issues shown. Rechecked \(findings.count) attempted files only. Other results remain snapshots; Check again performs a full scan."
                } catch {
                    summary.stringValue = "Targeted recheck failed: \(error.localizedDescription). Previous rows retained; use Check again for current file state."
                    repairReport.string += "\nTargeted recheck failed: \(error.localizedDescription)"
                }
                updateSelectionStatus()
            }
        }
    }

    func applyRecheck(_ report: [String: Any], findings: [[String: Any]]) throws {
        let attempted = Set(findings.compactMap { $0["path"] as? String })
        guard let checked = report["checkedPaths"] as? [String], Set(checked) == attempted,
              let refreshed = report["issues"] as? [[String: Any]],
              refreshed.allSatisfy({ finding in
                  guard let path = finding["path"] as? String else { return false }
                  return attempted.contains(path)
              }) else {
            throw NSError(domain: "AudioSorter", code: 5, userInfo: [NSLocalizedDescriptionKey: "Incomplete file recheck"])
        }
        let selectedPaths = Set(table.selectedRowIndexes.compactMap { row in
            issues.indices.contains(row) ? issues[row]["path"] as? String : nil
        })
        let clip = table.enclosingScrollView?.contentView
        let origin = clip?.bounds.origin
        let byPath = Dictionary(grouping: refreshed) { $0["path"] as! String }
        // Replace attempted rows in place; preserve every unrelated finding verbatim.
        issues = issues.flatMap { finding -> [[String: Any]] in
            guard let path = finding["path"] as? String, attempted.contains(path) else { return [finding] }
            return byPath[path] ?? []
        }
        table.reloadData()
        table.selectRowIndexes(IndexSet(issues.indices.filter { row in
            guard let path = issues[row]["path"] as? String else { return false }
            return selectedPaths.contains(path)
        }), byExtendingSelection: false)
        if let clip, let origin {
            clip.scroll(to: clip.constrainBoundsRect(NSRect(origin: origin, size: clip.bounds.size)).origin)
            table.enclosingScrollView?.reflectScrolledClipView(clip)
        }
    }

    @objc func reveal() {
        guard issues.indices.contains(table.selectedRow), let path = issues[table.selectedRow]["path"] as? String else { return }
        var url = URL(fileURLWithPath: path)
        // Missing files/folders can still be located via the nearest existing parent.
        while !FileManager.default.fileExists(atPath: url.path), url.path != "/" {
            url.deleteLastPathComponent()
        }
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }
}

// Every recorded grid, with files that went missing (renamed or deleted outside the app)
// or to Trash flagged rather than dropped. Only current files are in the XML.
final class RecordsController: NSWindowController, NSTableViewDataSource, NSTableViewDelegate {
    unowned let sorter: Sorter
    let summary = NSTextField(wrappingLabelWithString: "")
    let table = NSTableView()
    let revealButton = NSButton(title: "Show in Finder", target: nil, action: nil)
    let forgetButton = NSButton(title: "Forget selected…", target: nil, action: nil)
    var keys: [String] = []

    init(sorter: Sorter) {
        self.sorter = sorter
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 480), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Rekordbox Records"
        window.minSize = NSSize(width: 640, height: 320)
        window.isReleasedWhenClosed = false
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 12
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 20),
            content.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -20),
            content.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 20),
            content.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -20)
        ])
        content.addArrangedSubview(summary)
        for (identifier, heading, width) in [("status", "Status", 90.0), ("file", "File", 560.0), ("bpm", "BPM", 70.0), ("cues", "Cues", 50.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(identifier))
            column.title = heading
            column.width = width
            table.addTableColumn(column)
        }
        table.delegate = self
        table.dataSource = self
        table.rowHeight = 24
        table.allowsMultipleSelection = true
        let scroll = NSScrollView()
        scroll.documentView = table
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        content.addArrangedSubview(scroll)
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 150).isActive = true
        revealButton.target = self
        revealButton.action = #selector(reveal)
        forgetButton.target = self
        forgetButton.action = #selector(confirmForget)
        let actions = NSStackView(views: [NSButton(title: "Export now", target: self, action: #selector(export)), revealButton,
                                          NSButton(title: "Show XML in Finder", target: self, action: #selector(revealXML)), forgetButton])
        actions.spacing = 12
        content.addArrangedSubview(actions)
        for view in [summary, scroll] { view.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func present() {
        if window?.isVisible != true { window?.center() }
        reload()
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
    }

    func statusText(_ key: String) -> String {
        switch sorter.gridStore.status(key) {
        case .current: return "OK"
        case .missing: return "Missing"
        case .trashed: return "In Trash"
        }
    }

    // Problems first, so a renamed file is the first thing you see.
    func reload() {
        let store = sorter.gridStore
        keys = store.contents.tracks.keys.sorted { a, b in
            let (x, y) = (store.status(a) == .current, store.status(b) == .current)
            return x != y ? !x : a.localizedStandardCompare(b) == .orderedAscending
        }
        table.reloadData()
        let missing = keys.filter { store.status($0) != .current }.count
        let grids = keys.filter { store.contents.tracks[$0]?.hasGrid == true }.count
        var lines = ["\(keys.count) track\(keys.count == 1 ? "" : "s") (\(grids) with a recorded grid) → \(sorter.rekordboxXML.path)"]
        if let error = store.loadError { lines.append(error) }
        if missing > 0 { lines.append("\(missing) point\(missing == 1 ? "s" : "") to a missing or trashed file and \(missing == 1 ? "is" : "are") left out of the XML. Put the file back, or forget the record.") }
        switch sorter.exportResult {
        case .success(let report):
            let failed = (report["failed"] as? [[String: Any]] ?? []).map { "\(($0["path"] as? String).map { URL(fileURLWithPath: $0).lastPathComponent } ?? "?"): \($0["error"] ?? "")" }
            lines.append("Last export: \(report["tracks"] ?? 0) track(s) written." + (failed.isEmpty ? "" : " Not exported: " + failed.joined(separator: "; ")))
        case .failure(let error): lines.append("Last export failed: \(error.localizedDescription)")
        case nil: break
        }
        if sorter.exporting { lines.append("Exporting…") }
        summary.stringValue = lines.joined(separator: "\n")
        updateButtons()
    }

    func updateButtons() {
        let selected = table.selectedRowIndexes.filter { keys.indices.contains($0) }
        revealButton.isEnabled = selected.count == 1 && sorter.gridStore.status(keys[selected.first!]) == .current
        forgetButton.isEnabled = !selected.isEmpty
    }

    func numberOfRows(in tableView: NSTableView) -> Int { keys.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard keys.indices.contains(row), let record = sorter.gridStore.contents.tracks[keys[row]] else { return nil }
        let text: String
        switch tableColumn?.identifier.rawValue {
        case "status": text = statusText(keys[row])
        case "bpm": text = record.bpm.map { String(format: "%.2f", $0) } ?? "—"
        case "cues": text = "\(record.cues.count)"
        default: text = keys[row]
        }
        let label = NSTextField(labelWithString: text)
        label.lineBreakMode = .byTruncatingMiddle
        if tableColumn?.identifier.rawValue == "status", text != "OK" { label.textColor = .systemRed }
        return label
    }

    func tableViewSelectionDidChange(_ notification: Notification) { updateButtons() }

    @objc func export() { sorter.exportRekordbox(); reload() }

    @objc func reveal() {
        guard let row = table.selectedRowIndexes.first, keys.indices.contains(row) else { return }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: keys[row])])
    }

    @objc func revealXML() {
        let xml = sorter.rekordboxXML
        if FileManager.default.fileExists(atPath: xml.path) { NSWorkspace.shared.activateFileViewerSelecting([xml]) }
        else { summary.stringValue += "\n\(xml.path) has not been written yet." }
    }

    @objc func confirmForget() {
        let selected = table.selectedRowIndexes.filter { keys.indices.contains($0) }.map { keys[$0] }
        guard !selected.isEmpty, let window else { return }
        let alert = NSAlert()
        alert.messageText = selected.count == 1 ? "Forget this recorded grid?" : "Forget \(selected.count) recorded grids?"
        alert.informativeText = selected.map { URL(fileURLWithPath: $0).lastPathComponent }.joined(separator: "\n")
            + "\n\nTheir grids and cues leave the store and the XML. Audio files are not touched, and tracks already imported into Rekordbox keep what they have."
        alert.addButton(withTitle: "Cancel").keyEquivalent = "\r"
        let confirm = alert.addButton(withTitle: "Forget")
        confirm.keyEquivalent = ""
        confirm.hasDestructiveAction = true
        alert.beginSheetModal(for: window) { [self] response in
            guard response == .alertSecondButtonReturn else { return }
            forget(selected)
        }
    }

    func forget(_ selected: [String]) {
        do {
            try sorter.gridStore.forget(selected)
            sorter.exportRekordbox()
        } catch {
            summary.stringValue += "\nNot forgotten: \(error.localizedDescription)"
            return
        }
        reload()
    }
}

final class SettingsController: NSWindowController {
    unowned let sorter: Sorter
    let sourceField = NSTextField(string: "")
    let rootField = NSTextField(string: "")
    let rows = NSStackView()
    let add = NSButton(title: "Add destination", target: nil, action: nil)
    let preview = NSButton(checkboxWithTitle: "Start newly loaded tracks at 30%", target: nil, action: nil)
    let suffixField = NSTextField(string: "")
    let xmlField = NSTextField(string: "")

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
        let hint = NSTextField(wrappingLabelWithString: "Sorting creates folders inside the destination parent. Leave Genre blank to preserve the existing tag. Changes affect future moves and explicitly confirmed Genre repairs, never existing tags automatically.")
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
        suffixField.placeholderString = "Edit"
        let suffixRow = NSStackView(views: [NSTextField(labelWithString: "Cut-bars file name:  <name> - "), suffixField, NSTextField(labelWithString: ".<ext>")])
        suffixRow.spacing = 4
        suffixField.widthAnchor.constraint(equalToConstant: 140).isActive = true
        content.addArrangedSubview(suffixRow)
        content.addArrangedSubview(NSTextField(labelWithString: "Rekordbox XML (recorded grids and memory cues)"))
        xmlField.placeholderString = "~/Music/Audio Sorter/rekordbox.xml"
        let xmlRow = NSStackView(views: [xmlField, NSButton(title: "Choose…", target: self, action: #selector(browseXML))])
        xmlRow.spacing = 8
        content.addArrangedSubview(xmlRow)
        xmlRow.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
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
            suffixField.stringValue = sorter.configuration.editSuffix
            xmlField.stringValue = sorter.configuration.rekordboxXML
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

    @objc func browseXML() {
        let panel = NSSavePanel()
        panel.canCreateDirectories = true
        panel.allowedContentTypes = [.xml]
        let current = URL(fileURLWithPath: (xmlField.stringValue as NSString).expandingTildeInPath)
        panel.directoryURL = current.deletingLastPathComponent()
        panel.nameFieldStringValue = current.lastPathComponent.isEmpty ? "rekordbox.xml" : current.lastPathComponent
        panel.beginSheetModal(for: window!) { [self] response in
            if response == .OK, let url = panel.url { xmlField.stringValue = url.path }
        }
    }
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
                "destinations": destinations, "startAtThirtyPercent": preview.state == .on,
                "editSuffix": trimmed(suffixField), "rekordboxXML": trimmed(xmlField)
            ])
            let updated = try JSONDecoder().decode(Configuration.self, from: data)
            for path in [updated.sourceFolder, updated.destinationRoot, updated.rekordboxXML] {
                let expanded = (path as NSString).expandingTildeInPath
                guard (expanded as NSString).isAbsolutePath else {
                    throw NSError(domain: "AudioSorter", code: 5, userInfo: [NSLocalizedDescriptionKey: "Choose an absolute path or a path starting with ~/."])
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

// MARK: - Rekordbox records

// One file in the rekordbox XML: every track sorted with 1–9, plus any file whose grid was
// recorded. Grids and memory cues are in seconds as AVFoundation decodes the file (the
// editor's timeline); a sorted track without a grid has none, and Rekordbox analyses it.
// rekordbox.py turns these into the XML.
struct GridRecord: Codable, Equatable {
    var bpm: Double?
    var anchor: Double?  // Any downbeat.
    var cues: [Double]
    var trackID: Int     // Stable, so Rekordbox sees the same TrackID on every export.
    var duration: Double?
    var sampleRate: Double?
    // The day (yyyy-MM-dd) it was sorted or its grid last recorded: the XML lists each
    // day's tracks as a playlist, so new tracks can be imported without the old ones.
    var day: String?
    // Set when the app moved the file to Trash; Finder's Put Back makes the record current again.
    var trashed: Bool?
    var hasGrid: Bool { bpm != nil && anchor != nil }
}

// The persistent store: a small JSON file keyed by absolute path, replaced atomically.
// Tag writes never change the audio timeline, so they leave records valid; moves, move
// undo and Trash update the keys. Records are never dropped silently: a file renamed or
// deleted outside the app shows as missing in the Records window until you forget it.
final class GridStore {
    struct Contents: Codable {
        var version = 1
        var nextTrackID = 1
        var tracks: [String: GridRecord] = [:]
    }
    enum Status { case current, missing, trashed }

    let url: URL
    private(set) var contents = Contents()
    // A store that cannot be read is never overwritten, so its records survive for repair.
    private(set) var loadError: String?

    static var defaultURL: URL {
        // Tests point this at a temporary folder; the app always uses Application Support.
        let support = ProcessInfo.processInfo.environment["AUDIO_SORTER_SUPPORT"].map { URL(fileURLWithPath: $0, isDirectory: true) }
            ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("Audio Sorter", isDirectory: true)
        return support.appendingPathComponent("grids.json")
    }

    init(url: URL) {
        self.url = url
        guard let data = try? Data(contentsOf: url) else { return }
        do { contents = try JSONDecoder().decode(Contents.self, from: data) }
        catch { loadError = "Cannot read \(url.path): \(error.localizedDescription)" }
    }

    // Folder listings can spell a path through a symlink (/var vs /private/var) or with accents
    // decomposed (é as e + ◌́); APFS treats those as the same file, and so do keys.
    static func key(_ url: URL) -> String { url.resolvingSymlinksInPath().path.precomposedStringWithCanonicalMapping }

    func record(for url: URL) -> GridRecord? { contents.tracks[Self.key(url)] }

    static func today() -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: Date())
    }

    func status(_ key: String) -> Status {
        if FileManager.default.fileExists(atPath: key) { return .current }
        return contents.tracks[key]?.trashed == true ? .trashed : .missing
    }

    private func commit(_ change: (inout Contents) -> Void) throws {
        if let loadError { throw EditError(loadError + " Fix or remove that file, then try again.") }
        var updated = contents
        change(&updated)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
        try encoder.encode(updated).write(to: url, options: .atomic)
        contents = updated
    }

    func set(_ url: URL, grid: BeatGrid, cues: [Double], duration: Double, sampleRate: Double) throws {
        let key = Self.key(url)
        try commit { contents in
            let id = contents.tracks[key]?.trackID ?? contents.nextTrackID
            contents.nextTrackID = max(contents.nextTrackID, id + 1)
            contents.tracks[key] = GridRecord(bpm: grid.bpm, anchor: grid.anchor, cues: cues.sorted(), trackID: id, duration: duration, sampleRate: sampleRate, day: Self.today())
        }
    }

    // A track sorted with 1–9: its record follows it, or a new one without a grid joins
    // today's playlist. Returns whether a record was created, so undo can remove it again.
    func sorted(from old: String, to new: URL) throws -> Bool {
        if try move(from: old, to: new) { return false }
        let key = Self.key(new)
        try commit { contents in
            contents.tracks[key] = GridRecord(cues: [], trackID: contents.nextTrackID, day: Self.today())
            contents.nextTrackID += 1
        }
        return true
    }

    // Returns whether a record changed, so callers only regenerate the XML when needed.
    @discardableResult func move(from old: String, to new: URL) throws -> Bool {
        guard contents.tracks[old] != nil else { return false }
        let key = Self.key(new)
        try commit { contents in
            var record = contents.tracks.removeValue(forKey: old)!
            record.trashed = nil
            contents.tracks[key] = record
        }
        return true
    }

    @discardableResult func markTrashed(_ key: String) throws -> Bool {
        guard contents.tracks[key] != nil else { return false }
        try commit { $0.tracks[key]?.trashed = true }
        return true
    }

    func forget(_ keys: [String]) throws {
        try commit { contents in for key in keys { contents.tracks[key] = nil } }
    }
}

// MARK: - Bar editor

// Constant-tempo 4/4 grid. Bar k's downbeat is at anchor + k bars (seconds); k may be negative.
struct BeatGrid: Equatable {
    var bpm: Double
    var anchor: Double
    var barDuration: Double { 240 / bpm }
    var beatDuration: Double { 60 / bpm }
    func barTime(_ bar: Int) -> Double { anchor + Double(bar) * barDuration }
    func nearestBar(to time: Double) -> Int { Int(((time - anchor) / barDuration).rounded()) }
    // Bars are numbered from 1 at the first downbeat at or after time zero.
    var firstBar: Int { Int(((-anchor) / barDuration - 1e-9).rounded(.up)) }
    func number(_ bar: Int) -> Int { bar - firstBar + 1 }
}

// The edited track is a sequence of source pieces. Bounds are bar numbers rather than
// frames, so tempo and downbeat adjustments carry existing edits along with the grid.
enum Bound: Equatable { case start, bar(Int), end }
struct Piece: Equatable { var from: Bound; var to: Bound }

struct EditSession {
    static let untouched = [Piece(from: .start, to: .end)]
    // Rekordbox's MEMORY CUE panel holds ten cues per track.
    static let cueLimit = 10
    var grid: BeatGrid
    var pieces = untouched
    var selection: Int?  // First selected bar of the edited track.
    var bars = 16
    // Clicks snap only once the grid has been placed; until then it is a guess.
    var downbeatSet = false
    // False for a file without a BPM tag until a BPM is typed or tapped: no grid is shown
    // or used until then, rather than a 120 BPM guess that looks like a real grid.
    var tempoSet = true
    // Memory cues on the edited track, in beats from its grid's anchor (outputGrid), sorted.
    // Edits are whole bars, so a cut or duplicate moves later cues by exactly its length in
    // beats. Tempo changes and nudges keep each cue on its bar and beat, the same way pieces
    // keep their bar numbers, so a cue placed on the grid stays on it. Moving the downbeat
    // (regrid) keeps every beat line's cues at the same time, so they stay on the audio.
    // Source times would not survive a tempo change: the splices move within the source,
    // so a cue near one could end up in audio that is no longer part of the edit.
    var cues: [Double] = []
    // Undo entries pair each pieces snapshot with the cues at that moment.
    var undo: [[Piece]] = []
    var redo: [[Piece]] = []
    var cueUndo: [[Double]] = []
    var cueRedo: [[Double]] = []
    // The pieces last written to disk; nil until the first save.
    var savedPieces: [Piece]?
    // The cues last recorded for Rekordbox, with an edit or with "Save grid & cues".
    var savedCues: [Double] = []
    var edited: Bool { pieces != Self.untouched }
    // Grid-only changes do not count: they never change what a save would lose.
    var unsaved: Bool { (edited && pieces != savedPieces) || cues != savedCues }

    func time(_ bound: Bound, duration: Double) -> Double {
        switch bound {
        case .start: return 0
        case .end: return duration
        case .bar(let bar): return min(max(0, grid.barTime(bar)), duration)
        }
    }

    func length(_ piece: Piece, duration: Double) -> Double { time(piece.to, duration: duration) - time(piece.from, duration: duration) }
    func outputDuration(_ duration: Double) -> Double { pieces.reduce(0) { $0 + length($1, duration: duration) } }

    // Every piece after the first is whole bars long, so the source grid continues
    // through the edit; only a track that now starts on a downbeat needs a new anchor.
    func outputGrid(_ duration: Double) -> BeatGrid {
        guard let first = pieces.first, first.from != .start else { return grid }
        return BeatGrid(bpm: grid.bpm, anchor: 0)
    }

    // Splits the piece containing an edited-track time (on a bar line) and returns the index
    // of the piece that starts there.
    func split(_ pieces: [Piece], at outputTime: Double, duration: Double) -> ([Piece], Int)? {
        var result = pieces
        var cursor = 0.0
        for (index, piece) in pieces.enumerated() {
            let pieceLength = length(piece, duration: duration)
            if abs(outputTime - cursor) < 1e-6 { return (result, index) }
            if outputTime < cursor + pieceLength - 1e-6 {
                let bar = grid.nearestBar(to: time(piece.from, duration: duration) + outputTime - cursor)
                result.replaceSubrange(index...index, with: [Piece(from: piece.from, to: .bar(bar)), Piece(from: .bar(bar), to: piece.to)])
                return (result, index + 1)
            }
            cursor += pieceLength
        }
        return abs(outputTime - cursor) < 1e-6 ? (result, result.count) : nil
    }

    func normalized(_ pieces: [Piece], duration: Double) -> [Piece] {
        var result: [Piece] = []
        for piece in pieces where length(piece, duration: duration) > 1e-9 {
            if let last = result.last, last.to == piece.from { result[result.count - 1].to = piece.to }
            else { result.append(piece) }
        }
        return result
    }

    func selectionDescription(_ duration: Double) -> String? {
        guard let selection else { return nil }
        let first = outputGrid(duration).number(selection)
        return "bars \(first)–\(first + bars - 1)"
    }

    // Removes or repeats the selected bars. Duplicates land right after the selection and
    // become the new selection, so repeating the command keeps extending.
    mutating func edit(duplicate: Bool, duration: Double) throws {
        guard let selection else { throw EditError("Set a selection first: I at its first bar, O at its end.") }
        let output = outputGrid(duration)
        let start = output.barTime(selection), end = output.barTime(selection + bars)
        guard selection >= output.firstBar else { throw EditError("Selections start at bar 1 or later.") }
        guard end <= outputDuration(duration) + 1e-6 else { throw EditError("The selection runs past the end of the track.") }
        guard let (first, from) = split(pieces, at: start, duration: duration),
              let (both, to) = split(first, at: end, duration: duration) else { throw EditError("The selection is not on the grid.") }
        var updated = both
        if duplicate { updated.insert(contentsOf: both[from..<to], at: to) }
        else { updated.removeSubrange(from..<to) }
        remember()
        pieces = normalized(updated, duration: duration)
        if duplicate { self.selection = selection + bars }
        // Cues in a cut go with it; a duplicate does not copy cues. Cutting the opening bars can
        // move the anchor to 0, so positions go through time rather than shifting the beat count.
        let after = outputGrid(duration), length = end - start
        cues = cues.compactMap { cue in
            var time = output.anchor + cue * output.beatDuration
            if time >= end - 1e-6 { time += duplicate ? length : -length }
            else if !duplicate && time >= start - 1e-6 { return nil }
            return (time - after.anchor) / after.beatDuration
        }
    }

    // Moves every bar bound to the bar nearest its old time under the new grid.
    mutating func regrid(_ updated: BeatGrid) {
        let old = grid
        func remap(_ bound: Bound) -> Bound {
            if case .bar(let bar) = bound { return .bar(updated.nearestBar(to: old.barTime(bar))) }
            return bound
        }
        func remapAll(_ pieces: [Piece]) -> [Piece] { pieces.map { Piece(from: remap($0.from), to: remap($0.to)) } }
        // Each cue's beat line becomes the new line nearest it; the offset from that line is kept.
        // A track that starts on a bar line has its grid anchored at 0, which does not move.
        func remapCues(_ cues: [Double], _ pieces: [Piece]) -> [Double] {
            guard pieces.first?.from ?? .start == .start else { return cues }
            return cues.map { cue in
                let line = cue.rounded()
                return ((old.anchor + line * old.beatDuration - updated.anchor) / updated.beatDuration).rounded() + cue - line
            }
        }
        cues = remapCues(cues, pieces)
        cueUndo = zip(cueUndo, undo).map(remapCues)
        cueRedo = zip(cueRedo, redo).map(remapCues)
        savedCues = remapCues(savedCues, savedPieces ?? pieces)
        pieces = remapAll(pieces)
        undo = undo.map(remapAll)
        redo = redo.map(remapAll)
        savedPieces = savedPieces.map(remapAll)
        grid = updated
    }

    // Every change to pieces or cues goes on one undo stack.
    mutating func remember() {
        undo.append(pieces)
        cueUndo.append(cues)
        redo.removeAll()
        cueRedo.removeAll()
    }

    mutating func undoLast() -> Bool {
        guard let previous = undo.popLast(), let previousCues = cueUndo.popLast() else { return false }
        redo.append(pieces)
        cueRedo.append(cues)
        pieces = previous
        cues = previousCues
        return true
    }

    mutating func redoLast() -> Bool {
        guard let next = redo.popLast(), let nextCues = cueRedo.popLast() else { return false }
        undo.append(pieces)
        cueUndo.append(cues)
        pieces = next
        cues = nextCues
        return true
    }

    // Cue positions in seconds on the edited track.
    func cueTimes(_ duration: Double) -> [Double] {
        let output = outputGrid(duration)
        return cues.map { output.anchor + $0 * output.beatDuration }
    }

    mutating func addCue(at time: Double, duration: Double) throws {
        let output = outputGrid(duration)
        guard cueTimes(duration).allSatisfy({ abs($0 - time) >= 0.001 }) else { throw EditError("There is already a cue here.") }
        guard cues.count < Self.cueLimit else { throw EditError("Rekordbox keeps at most \(Self.cueLimit) memory cues per track. Remove one first (⇧M).") }
        remember()
        cues.append((time - output.anchor) / output.beatDuration)
        cues.sort()
    }

    // Removes the cue nearest an edited-track time and returns where it was.
    mutating func removeCue(nearest time: Double, duration: Double) throws -> Double {
        let times = cueTimes(duration)
        guard let index = times.indices.min(by: { abs(times[$0] - time) < abs(times[$1] - time) }) else { throw EditError("There are no cues to remove.") }
        remember()
        cues.remove(at: index)
        return times[index]
    }
}

struct EditError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

// Frame-level view of an edit: where each output frame comes from in the source.
struct EditTimeline {
    struct Span: Equatable { var source: Int64; var output: Int64; var count: Int64 }
    let rate: Double
    let spans: [Span]
    let length: Int64
    let grid: BeatGrid

    init(session: EditSession, rate: Double, frames: Int64) {
        self.rate = rate
        let duration = Double(frames) / rate
        func frame(_ bound: Bound) -> Int64 {
            switch bound {
            case .start: return 0
            case .end: return frames
            case .bar(let bar): return min(max(0, Int64((session.grid.barTime(bar) * rate).rounded())), frames)
            }
        }
        var spans: [Span] = []
        var cursor: Int64 = 0
        for piece in session.pieces {
            let start = frame(piece.from), count = frame(piece.to) - start
            guard count > 0 else { continue }
            spans.append(Span(source: start, output: cursor, count: count))
            cursor += count
        }
        self.spans = spans
        length = cursor
        grid = session.outputGrid(duration)
    }

    var duration: Double { Double(length) / rate }

    func ranges(from output: Int64, count: Int64) -> [(source: Int64, count: Int64)] {
        var result: [(source: Int64, count: Int64)] = []
        var position = max(0, output)
        let end = min(length, output + count)
        for span in spans where span.output + span.count > position && span.output < end {
            let taken = min(span.output + span.count, end) - position
            result.append((span.source + position - span.output, taken))
            position += taken
        }
        return result
    }

    // The source time an edited-track time plays, if that audio appears exactly once in the
    // edit. Audio that was duplicated has no single place on the original.
    func uniqueSourceTime(_ time: Double) -> Double? {
        let frame = Int64((time * rate).rounded())
        guard let span = spans.first(where: { frame >= $0.output && frame < $0.output + $0.count }) else { return nil }
        let source = span.source + frame - span.output
        guard spans.filter({ source >= $0.source && source < $0.source + $0.count }).count == 1 else { return nil }
        return time + Double(span.source - span.output) / rate
    }

    // Output frames where playback jumps to a different part of the source.
    var junctions: [Int64] {
        zip(spans, spans.dropFirst()).compactMap { $0.source + $0.count == $1.source ? nil : $1.output }
    }

    // Source frame ranges to concatenate, with contiguous spans merged.
    var segments: [[Int64]] {
        var result: [[Int64]] = []
        for span in spans {
            if let last = result.last, last[1] == span.source { result[result.count - 1][1] = span.source + span.count }
            else { result.append([span.source, span.source + span.count]) }
        }
        return result
    }
}

// Min/max envelope at several resolutions so drawing stays cheap at any zoom.
struct WaveformPeaks {
    static let baseFrames = 32
    static let factor = 32
    var minimum: [[Float]]
    var maximum: [[Float]]
    var sampleRate: Double

    func binFrames(_ level: Int) -> Int64 { Int64(Self.baseFrames) * Int64(pow(Double(Self.factor), Double(level))) }

    // Accurate to the finest bin: coarse bins are only used where they fit inside the range.
    func extremes(from start: Int64, to end: Int64) -> (Float, Float)? {
        guard end > start, !minimum.isEmpty else { return nil }
        var low: Float = 0, high: Float = 0, found = false
        var position = start
        while position < end {
            var level = 0
            while level + 1 < minimum.count {
                let size = binFrames(level + 1)
                guard position % size == 0, position + size <= end else { break }
                level += 1
            }
            let size = binFrames(level), bin = Int(position / size)
            guard bin < minimum[level].count else { break }
            low = min(low, minimum[level][bin])
            high = max(high, maximum[level][bin])
            found = true
            position = (position / size + 1) * size
        }
        return found ? (low, high) : nil
    }
}

func waveformPeaks(_ url: URL, cancelled: () -> Bool = { false }) -> WaveformPeaks? {
    guard let file = try? AVAudioFile(forReading: url), file.length > 0,
          let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 65_536) else { return nil }
    let channels = Int(file.processingFormat.channelCount)
    let bin = WaveformPeaks.baseFrames
    var minimum: [Float] = [], maximum: [Float] = []
    minimum.reserveCapacity(Int(file.length) / bin + 1)
    maximum.reserveCapacity(Int(file.length) / bin + 1)
    var low: Float = 0, high: Float = 0, filled = 0, decoded = false
    while !cancelled() {
        do { try file.read(into: buffer) } catch { if !decoded { return nil }; break }
        let frames = Int(buffer.frameLength)
        guard frames > 0, let data = buffer.floatChannelData else { break }
        decoded = true
        var offset = 0
        // Bins carry across buffer boundaries so every bin covers exactly `bin` frames.
        while offset < frames {
            let count = min(bin - filled, frames - offset)
            for channel in 0..<channels {
                var value: Float = 0
                vDSP_minv(data[channel] + offset, 1, &value, vDSP_Length(count))
                low = min(low, value)
                vDSP_maxv(data[channel] + offset, 1, &value, vDSP_Length(count))
                high = max(high, value)
            }
            filled += count
            offset += count
            if filled == bin {
                minimum.append(low); maximum.append(high)
                low = 0; high = 0; filled = 0
            }
        }
    }
    if cancelled() || !decoded { return nil }
    if filled > 0 { minimum.append(low); maximum.append(high) }
    var mins = [minimum], maxs = [maximum]
    while let last = mins.last, last.count > WaveformPeaks.factor {
        let previousMax = maxs.last!
        let groups = stride(from: 0, to: last.count, by: WaveformPeaks.factor)
        mins.append(groups.map { last[$0..<min(last.count, $0 + WaveformPeaks.factor)].min()! })
        maxs.append(groups.map { previousMax[$0..<min(previousMax.count, $0 + WaveformPeaks.factor)].max()! })
    }
    return WaveformPeaks(minimum: mins, maximum: maxs, sampleRate: file.processingFormat.sampleRate)
}

// Decodes with AVFoundation, the decoder the editor draws and plays with, so MP3 frame
// numbers in an edit match what was on screen regardless of encoder-delay headers.
func decodeToWAV(_ source: URL, to destination: URL) throws {
    let input = try AVAudioFile(forReading: source)
    let format = input.processingFormat
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: format.sampleRate,
        AVNumberOfChannelsKey: format.channelCount, AVLinearPCMBitDepthKey: 32,
        AVLinearPCMIsFloatKey: true, AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false
    ]
    let output = try AVAudioFile(forWriting: destination, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
    guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 65_536) else { return }
    // Stop at the reported length: that is the timeline the editor used.
    while input.framePosition < input.length {
        try input.read(into: buffer, frameCount: AVAudioFrameCount(min(65_536, input.length - input.framePosition)))
        if buffer.frameLength == 0 { break }
        try output.write(from: buffer)
    }
    output.close()
}

// A click on every beat of the edited track, accented on downbeats, rendered live by the
// audio engine so tempo and grid changes are heard immediately without rescheduling.
final class Metronome: @unchecked Sendable {
    struct State {
        var enabled = false
        var grid = BeatGrid(bpm: 120, anchor: 0)
        var end = 0.0
        // Host time at which playback of `seconds` (edited-track time) is heard; nil when stopped.
        var sync: (host: UInt64, seconds: Double)?
    }

    static let secondsPerTick: Double = {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        return Double(info.numer) / Double(info.denom) / 1e9
    }()
    static let length = 0.03

    private let lock = NSLock()
    private var state = State()
    func update(_ change: (inout State) -> Void) { lock.withLock { change(&state) } }
    var snapshot: State { lock.withLock { state } }

    static func click(at time: Double, grid: BeatGrid, end: Double) -> Float {
        guard time >= 0, time < end else { return 0 }
        let beat = ((time - grid.anchor) / grid.beatDuration).rounded(.down)
        let phase = time - (grid.anchor + beat * grid.beatDuration)
        guard phase < length else { return 0 }
        let downbeat = Int(beat) % 4 == 0
        return Float((downbeat ? 0.6 : 0.35) * sin(2 * .pi * (downbeat ? 1500 : 1000) * phase) * exp(-phase * 150))
    }

    static func fill(_ samples: UnsafeMutablePointer<Float>, count: Int, from start: Double, rate: Double, state: State) {
        for frame in 0..<count {
            samples[frame] = click(at: start + Double(frame) / rate, grid: state.grid, end: state.end)
        }
    }

    func makeNode(rate: Double) -> AVAudioSourceNode {
        AVAudioSourceNode(format: AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!) { [self] _, timestamp, frameCount, bufferList in
            let state = snapshot
            for buffer in UnsafeMutableAudioBufferListPointer(bufferList) {
                guard let data = buffer.mData?.assumingMemoryBound(to: Float.self) else { continue }
                let count = Int(frameCount)
                guard state.enabled, let sync = state.sync, timestamp.pointee.mFlags.contains(.hostTimeValid) else {
                    data.update(repeating: 0, count: count)
                    continue
                }
                let start = sync.seconds + Double(Int64(bitPattern: timestamp.pointee.mHostTime &- sync.host)) * Self.secondsPerTick
                Self.fill(data, count: count, from: start, rate: rate, state: state)
            }
            return noErr
        }
    }
}

// Tap tempo: the average of the last taps, restarting after a pause.
struct TapTempo {
    var taps: [TimeInterval] = []
    static let needed = 4

    // Returns the tempo once enough taps have arrived, rounded to 0.1 BPM.
    mutating func tap(at time: TimeInterval) -> Double? {
        if let last = taps.last, time - last > 2 || time <= last { taps.removeAll() }
        taps.append(time)
        taps = Array(taps.suffix(16))
        guard taps.count >= Self.needed else { return nil }
        return (600 * Double(taps.count - 1) / (taps.last! - taps.first!)).rounded() / 10
    }
}

final class EditWaveformView: NSView {
    var peaks: WaveformPeaks? { didSet { needsDisplay = true } }
    var timeline: EditTimeline? { didSet { rawCache = nil; if fitted { fit() } else { clampView() } } }
    var sampleFile: AVAudioFile? { didSet { rawCache = nil } }
    var selection: (start: Double, end: Double)? { didSet { needsDisplay = true } }
    var cues: [Double] = [] { didSet { if cues != oldValue { needsDisplay = true } } }
    var playhead: Double = 0 { didSet { if playhead != oldValue { needsDisplay = true } } }
    var visibleStart = 0.0
    var secondsPerPixel = 0.01
    var fitted = true
    var snapping = false
    var showsGrid = true { didSet { if showsGrid != oldValue { needsDisplay = true } } }
    var onSeek: ((Double) -> Void)?
    var onGridDrag: ((Double?) -> Void)?
    var onViewChange: (() -> Void)?
    private var dragOrigin: CGFloat?
    private var rawCache: (start: Int64, samples: [Float])?

    var rate: Double { timeline?.rate ?? 44_100 }
    var duration: Double { timeline?.duration ?? 0 }
    var span: Double { Double(bounds.width) * secondsPerPixel }
    var minimumSecondsPerPixel: Double { 0.125 / rate }
    var maximumSecondsPerPixel: Double { max(minimumSecondsPerPixel, duration / max(1, Double(bounds.width))) }

    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: 260) }

    func time(at x: CGFloat) -> Double { visibleStart + Double(x) * secondsPerPixel }
    func x(for time: Double) -> CGFloat { CGFloat((time - visibleStart) / secondsPerPixel) }

    func fit() {
        fitted = true
        secondsPerPixel = maximumSecondsPerPixel
        visibleStart = 0
        changed()
    }

    func zoom(by factor: Double, around x: CGFloat) {
        let anchor = time(at: x)
        secondsPerPixel = min(max(secondsPerPixel / factor, minimumSecondsPerPixel), maximumSecondsPerPixel)
        fitted = secondsPerPixel >= maximumSecondsPerPixel
        visibleStart = anchor - Double(x) * secondsPerPixel
        clampView()
    }

    func clampView() {
        secondsPerPixel = min(max(secondsPerPixel, minimumSecondsPerPixel), maximumSecondsPerPixel)
        visibleStart = min(max(0, visibleStart), max(0, duration - span))
        changed()
    }

    func center(on time: Double) {
        visibleStart = time - span / 2
        clampView()
    }

    // Pages along with playback the way a DAW follows the playhead.
    func follow(_ time: Double) {
        if time < visibleStart || time > visibleStart + span { visibleStart = time - span * 0.05; clampView() }
    }

    private func changed() {
        needsDisplay = true
        onViewChange?()
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        if fitted { fit() } else { clampView() }
    }

    override func scrollWheel(with event: NSEvent) {
        let precise = event.hasPreciseScrollingDeltas
        if event.modifierFlags.contains(.command) || event.modifierFlags.contains(.option) {
            zoom(by: exp(Double(event.scrollingDeltaY) * (precise ? 0.01 : 0.1)), around: convert(event.locationInWindow, from: nil).x)
            return
        }
        let dx = event.scrollingDeltaX, dy = event.scrollingDeltaY
        let delta = abs(dx) > abs(dy) ? dx : dy
        visibleStart -= Double(delta) * (precise ? 1 : 10) * secondsPerPixel
        fitted = false
        clampView()
    }

    override func magnify(with event: NSEvent) {
        zoom(by: 1 + Double(event.magnification), around: convert(event.locationInWindow, from: nil).x)
    }

    // The finest grid division at least 12 px wide: sixteenths when zoomed in, then
    // eighths, beats, half bars, bars, 2 bars and so on as the view zooms out.
    func snapUnit(_ grid: BeatGrid) -> Double {
        var unit = grid.beatDuration / 4
        while unit / secondsPerPixel < 12 { unit *= 2 }
        return unit
    }

    func snapped(_ time: Double, to grid: BeatGrid) -> Double {
        let unit = snapUnit(grid)
        // Measured from bar 1 so multi-bar units land on musically even bars.
        let origin = grid.barTime(grid.firstBar)
        return min(max(0, origin + ((time - origin) / unit).rounded() * unit), duration)
    }

    func seek(_ event: NSEvent) {
        let time = min(max(0, time(at: convert(event.locationInWindow, from: nil).x)), duration)
        if snapping, !event.modifierFlags.contains(.command), let grid = timeline?.grid { onSeek?(snapped(time, to: grid)) }
        else { onSeek?(time) }
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        let x = convert(event.locationInWindow, from: nil).x
        if event.modifierFlags.contains(.option) { dragOrigin = x; onGridDrag?(0) }
        else { seek(event) }
    }

    override func mouseDragged(with event: NSEvent) {
        let x = convert(event.locationInWindow, from: nil).x
        if let dragOrigin { onGridDrag?(Double(x - dragOrigin) * secondsPerPixel) }
        else { seek(event) }
    }

    override func mouseUp(with event: NSEvent) {
        if dragOrigin != nil { onGridDrag?(nil) }
        dragOrigin = nil
    }

    // Channel-averaged samples for sample-level zoom, read straight from the file.
    func samples(from start: Int64, count: Int) -> [Float] {
        if let rawCache, start >= rawCache.start, start + Int64(count) <= rawCache.start + Int64(rawCache.samples.count) {
            let offset = Int(start - rawCache.start)
            return Array(rawCache.samples[offset..<offset + count])
        }
        guard let timeline, let file = sampleFile else { return [] }
        // Read three screens' worth so scrolling and playhead redraws reuse it.
        let first = max(0, start - Int64(count)), total = Int64(count) * 3
        var result: [Float] = []
        let channels = Int(file.processingFormat.channelCount)
        for range in timeline.ranges(from: first, count: total) {
            guard let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(range.count)) else { return [] }
            file.framePosition = range.source
            do { try file.read(into: buffer, frameCount: AVAudioFrameCount(range.count)) } catch { return [] }
            guard let data = buffer.floatChannelData else { return [] }
            for frame in 0..<Int(buffer.frameLength) {
                var sum: Float = 0
                for channel in 0..<channels { sum += data[channel][frame] }
                result.append(sum / Float(channels))
            }
            result += [Float](repeating: 0, count: Int(range.count) - Int(buffer.frameLength))
        }
        rawCache = (first, result)
        let offset = Int(start - first)
        guard offset >= 0, offset < result.count else { return [] }
        return Array(result[offset..<min(result.count, offset + count)])
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.textBackgroundColor.setFill()
        bounds.fill()
        guard let timeline, timeline.length > 0 else { return }
        let labelHeight: CGFloat = 16
        let middle = (bounds.height - labelHeight) / 2
        let half = middle - 4
        if let selection {
            NSColor.controlAccentColor.withAlphaComponent(0.18).setFill()
            NSRect(x: x(for: selection.start), y: 0, width: x(for: selection.end) - x(for: selection.start), height: bounds.height).fill()
        }
        drawWaveform(timeline, middle: middle, half: half)
        if showsGrid { drawGrid(timeline.grid, height: bounds.height - labelHeight) }
        NSColor.systemRed.setFill()
        for junction in timeline.junctions {
            NSRect(x: x(for: Double(junction) / rate) - 1, y: 0, width: 2, height: bounds.height - labelHeight).fill()
        }
        // Memory cues: a line with a triangle hanging from the top, like Rekordbox's markers.
        NSColor.systemGreen.setFill()
        for cue in cues {
            let position = x(for: cue)
            guard position > -8, position < bounds.width + 8 else { continue }
            NSRect(x: position, y: 0, width: 1, height: bounds.height - labelHeight).fill()
            let marker = NSBezierPath()
            let top = bounds.height - labelHeight
            marker.move(to: NSPoint(x: position - 6, y: top))
            marker.line(to: NSPoint(x: position + 7, y: top))
            marker.line(to: NSPoint(x: position + 0.5, y: top - 9))
            marker.close()
            marker.fill()
        }
        NSColor.labelColor.setFill()
        NSRect(x: x(for: playhead), y: 0, width: 1, height: bounds.height).fill()
    }

    private func drawWaveform(_ timeline: EditTimeline, middle: CGFloat, half: CGFloat) {
        let framesPerPixel = secondsPerPixel * rate
        let columns = Int(bounds.width.rounded(.up))
        NSColor.secondaryLabelColor.setFill()
        if framesPerPixel >= Double(WaveformPeaks.baseFrames) {
            guard let peaks else { return }
            for column in 0..<columns {
                let start = Int64(time(at: CGFloat(column)) * rate), end = Int64(time(at: CGFloat(column + 1)) * rate)
                var low: Float = 0, high: Float = 0
                for range in timeline.ranges(from: start, count: max(1, end - start)) {
                    guard let (l, h) = peaks.extremes(from: range.source, to: range.source + range.count) else { continue }
                    low = min(low, l); high = max(high, h)
                }
                let top = middle + CGFloat(high) * half, bottom = middle + CGFloat(low) * half
                NSRect(x: CGFloat(column), y: bottom, width: 1, height: max(1, top - bottom)).fill()
            }
            return
        }
        let first = Int64(max(0, visibleStart * rate).rounded(.down))
        let count = Int((Double(columns) * framesPerPixel).rounded(.up)) + 2
        let values = samples(from: first, count: count)
        guard !values.isEmpty else { return }
        if framesPerPixel >= 1 {
            for column in 0..<columns {
                let a = Int(Double(column) * framesPerPixel), b = min(values.count, max(a + 1, Int(Double(column + 1) * framesPerPixel)))
                guard a < b else { break }
                let slice = values[a..<b]
                let top = middle + CGFloat(slice.max()!) * half, bottom = middle + CGFloat(slice.min()!) * half
                NSRect(x: CGFloat(column), y: bottom, width: 1, height: max(1, top - bottom)).fill()
            }
            return
        }
        // Individual samples: a connected line, with dots once they are far enough apart.
        let path = NSBezierPath()
        let pixelsPerFrame = 1 / framesPerPixel
        for (i, value) in values.enumerated() {
            let point = NSPoint(x: x(for: Double(first + Int64(i)) / rate), y: middle + CGFloat(value) * half)
            if i == 0 { path.move(to: point) } else { path.line(to: point) }
            if pixelsPerFrame >= 6 { NSBezierPath(ovalIn: NSRect(x: point.x - 2, y: point.y - 2, width: 4, height: 4)).fill() }
        }
        NSColor.secondaryLabelColor.setStroke()
        path.lineWidth = 1
        path.stroke()
    }

    private func drawGrid(_ grid: BeatGrid, height: CGFloat) {
        let barPixels = grid.barDuration / secondsPerPixel
        let end = visibleStart + span
        // Eighths or sixteenths, when zoomed in far enough to snap to them.
        let unit = snapUnit(grid)
        if unit < grid.beatDuration {
            NSColor.tertiaryLabelColor.withAlphaComponent(0.15).setFill()
            let perBeat = Int((grid.beatDuration / unit).rounded())
            let first = Int(((visibleStart - grid.anchor) / unit).rounded(.down))
            let last = Int(((end - grid.anchor) / unit).rounded(.up))
            for line in first...max(first, last) where line % perBeat != 0 {
                NSRect(x: x(for: grid.anchor + Double(line) * unit), y: 0, width: 1, height: height).fill()
            }
        }
        if barPixels / 4 >= 6 {
            NSColor.tertiaryLabelColor.withAlphaComponent(0.35).setFill()
            let firstBeat = Int(((visibleStart - grid.anchor) / grid.beatDuration).rounded(.down))
            let lastBeat = Int(((end - grid.anchor) / grid.beatDuration).rounded(.up))
            for beat in firstBeat...max(firstBeat, lastBeat) where beat % 4 != 0 {
                NSRect(x: x(for: grid.anchor + Double(beat) * grid.beatDuration), y: 0, width: 1, height: height).fill()
            }
        }
        // Same 12 px threshold as snapping, so every bar line drawn is one a click can land on.
        var step = 1
        while Double(step) * barPixels < 12 { step *= 2 }
        var labelStep = step
        while Double(labelStep) * barPixels < 44 { labelStep *= 2 }
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .regular), .foregroundColor: NSColor.secondaryLabelColor]
        let firstBar = Int(((visibleStart - grid.anchor) / grid.barDuration).rounded(.down))
        let lastBar = Int(((end - grid.anchor) / grid.barDuration).rounded(.up))
        for bar in firstBar...max(firstBar, lastBar) {
            let number = grid.number(bar)
            // Bar 1 is always on the lattice, so thinned-out lines stay on musically even bars.
            let index = number - 1
            guard ((index % step) + step) % step == 0 else { continue }
            let position = x(for: grid.barTime(bar))
            (((index % 16) + 16) % 16 == 0 ? NSColor.systemOrange : NSColor.systemOrange.withAlphaComponent(0.55)).setFill()
            NSRect(x: position, y: 0, width: 1, height: height).fill()
            if ((index % labelStep) + labelStep) % labelStep == 0 {
                ("\(number)" as NSString).draw(at: NSPoint(x: position + 3, y: height + 1), withAttributes: attributes)
            }
        }
    }

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .image }
    override func accessibilityLabel() -> String? { "Edited waveform with bar grid" }
}

// Whole-track strip: shows where the zoomed view is; click or drag to jump there.
final class EditOverviewView: NSView {
    var levels: [Float] = [] { didSet { needsDisplay = true } }
    var duration = 0.0
    var visible: (start: Double, end: Double) = (0, 0) { didSet { needsDisplay = true } }
    var selection: (start: Double, end: Double)? { didSet { needsDisplay = true } }
    var cues: [Double] = [] { didSet { if cues != oldValue { needsDisplay = true } } }
    var playhead = 0.0 { didSet { if playhead != oldValue { needsDisplay = true } } }
    var onSelect: ((Double) -> Void)?

    override var intrinsicContentSize: NSSize { NSSize(width: NSView.noIntrinsicMetric, height: 44) }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func mouseDown(with event: NSEvent) { select(event) }
    override func mouseDragged(with event: NSEvent) { select(event) }

    func x(for time: Double) -> CGFloat { duration > 0 ? CGFloat(time / duration) * bounds.width : 0 }

    func select(_ event: NSEvent) {
        guard duration > 0, bounds.width > 0 else { return }
        let x = convert(event.locationInWindow, from: nil).x
        onSelect?(Double(min(max(0, x / bounds.width), 1)) * duration)
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.controlBackgroundColor.setFill()
        bounds.fill()
        if let selection {
            NSColor.controlAccentColor.withAlphaComponent(0.3).setFill()
            NSRect(x: x(for: selection.start), y: 0, width: max(1, x(for: selection.end) - x(for: selection.start)), height: bounds.height).fill()
        }
        NSColor.secondaryLabelColor.setFill()
        for (i, level) in levels.enumerated() {
            let height = max(1, CGFloat(level) * (bounds.height - 4))
            let x = CGFloat(i) * bounds.width / CGFloat(max(1, levels.count))
            NSRect(x: x, y: bounds.midY - height / 2, width: max(1, bounds.width / CGFloat(max(1, levels.count))), height: height).fill()
        }
        NSColor.controlAccentColor.setStroke()
        let window = NSBezierPath(rect: NSRect(x: x(for: visible.start), y: 0.5, width: max(2, x(for: visible.end) - x(for: visible.start)), height: bounds.height - 1))
        window.lineWidth = 1.5
        window.stroke()
        NSColor.systemGreen.setFill()
        for cue in cues {
            NSRect(x: x(for: cue) - 1, y: bounds.height - 8, width: 3, height: 8).fill()
        }
        NSColor.labelColor.setFill()
        NSRect(x: x(for: playhead), y: 0, width: 1, height: bounds.height).fill()
    }

    override func isAccessibilityElement() -> Bool { true }
    override func accessibilityRole() -> NSAccessibility.Role? { .image }
    override func accessibilityLabel() -> String? { "Track overview" }
}

final class EditorController: NSWindowController, NSWindowDelegate {
    unowned let sorter: Sorter
    var url: URL?
    var session = EditSession(grid: BeatGrid(bpm: 120, anchor: 0))
    var rate = 44_100.0
    var frames: Int64 = 0
    var duration: Double { Double(frames) / rate }
    var timeline: EditTimeline { EditTimeline(session: session, rate: rate, frames: frames) }
    var playFile: AVAudioFile?
    var playhead = 0.0
    let engine = AVAudioEngine()
    let node = AVAudioPlayerNode()
    var playing = false
    var playStart: Int64 = 0
    var playEnd: Int64 = 0
    var peaksJob: CancellationFlag?
    let peaksQueue = DispatchQueue(label: "AudioSorter.peaks", qos: .userInitiated)
    var dragAnchor: Double?
    var saving = false
    var monitor: Any?
    var timer: Timer?

    let waveform = EditWaveformView()
    let overview = EditOverviewView()
    let titleLabel = NSTextField(labelWithString: "")
    let positionLabel = NSTextField(labelWithString: "")
    let bpmField = NSTextField(string: "")
    let barsField = NSTextField(string: "16")
    let selectionLabel = NSTextField(labelWithString: "")
    let editLabel = NSTextField(labelWithString: "")
    let status = NSTextField(wrappingLabelWithString: "")
    let playButton = NSButton(title: "Play  Space", target: nil, action: nil)
    let saveButton = NSButton(title: "Save  ⌘S", target: nil, action: nil)
    let undoButton = NSButton(title: "Undo  ⌘Z", target: nil, action: nil)
    let redoButton = NSButton(title: "Redo  ⌘⇧Z", target: nil, action: nil)
    let cutButton = NSButton(title: "Cut  X", target: nil, action: nil)
    let duplicateButton = NSButton(title: "Duplicate  ⌘D", target: nil, action: nil)
    let snapButton = NSButton(checkboxWithTitle: "Snap (⌘-click: free)", target: nil, action: nil)
    let clickButton = NSButton(checkboxWithTitle: "Click  K", target: nil, action: nil)
    let writeBPMButton = NSButton(title: "Write BPM to tag", target: nil, action: nil)
    let cueLabel = NSTextField(labelWithString: "")
    let recordButton = NSButton(title: "Save grid & cues  ⌘⇧S", target: nil, action: nil)
    let detectButton = NSButton(title: "Detect", target: nil, action: nil)
    var detecting = false
    var detectJob: CancellationFlag?
    // The session from before the last detection, while ⌘Z can still return to it: until the
    // grid is changed again, and only when the undo stack is back where detection left it.
    var detectUndo: (session: EditSession, undoCount: Int)?
    // The original file's BPM tag, so writing it is only offered when it would change.
    var taggedBPM: Double?
    let metronome = Metronome()
    var tapTempo = TapTempo()

    init(sorter: Sorter) {
        self.sorter = sorter
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 640), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        super.init(window: window)
        window.title = "Cut Bars"
        window.minSize = NSSize(width: 900, height: 520)
        window.isReleasedWhenClosed = false
        window.delegate = self
        engine.attach(node)
        let outputRate = engine.mainMixerNode.outputFormat(forBus: 0).sampleRate
        let click = metronome.makeNode(rate: outputRate > 0 ? outputRate : 44_100)
        engine.attach(click)
        engine.connect(click, to: engine.mainMixerNode, format: click.outputFormat(forBus: 0))
        // Views no longer clip by default (macOS 14); lines past the edges would spill out.
        waveform.clipsToBounds = true
        overview.clipsToBounds = true
        let content = NSStackView()
        content.orientation = .vertical
        content.alignment = .leading
        content.spacing = 10
        content.translatesAutoresizingMaskIntoConstraints = false
        window.contentView!.addSubview(content)
        NSLayoutConstraint.activate([
            content.leadingAnchor.constraint(equalTo: window.contentView!.leadingAnchor, constant: 20),
            content.trailingAnchor.constraint(equalTo: window.contentView!.trailingAnchor, constant: -20),
            content.topAnchor.constraint(equalTo: window.contentView!.topAnchor, constant: 20),
            content.bottomAnchor.constraint(equalTo: window.contentView!.bottomAnchor, constant: -20)
        ])
        titleLabel.font = .systemFont(ofSize: 16, weight: .semibold)
        titleLabel.lineBreakMode = .byTruncatingMiddle
        content.addArrangedSubview(titleLabel)
        content.addArrangedSubview(overview)
        content.addArrangedSubview(waveform)
        waveform.setContentHuggingPriority(.defaultLow - 1, for: .vertical)
        positionLabel.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
        clickButton.state = UserDefaults.standard.bool(forKey: "editorMetronome") ? .on : .off
        clickButton.target = self
        clickButton.action = #selector(clickChanged)
        clickButton.toolTip = "Metronome on every beat (accented downbeats) to check the grid by ear"
        let transport = NSStackView(views: [playButton, button("Play into selection  P", #selector(previewSelection)), clickButton, positionLabel])
        transport.spacing = 12
        content.addArrangedSubview(transport)
        bpmField.placeholderString = "BPM"
        bpmField.target = self
        bpmField.action = #selector(bpmEntered)
        bpmField.widthAnchor.constraint(equalToConstant: 80).isActive = true
        let gridRow = NSStackView(views: [
            NSTextField(labelWithString: "BPM"), bpmField, detectButton, button("Tap  T", #selector(tapPressed)), button("÷2", #selector(halveTempo)), button("×2", #selector(doubleTempo)), writeBPMButton,
            button("Downbeat at playhead  ⌘\\", #selector(setDownbeat)),
            button("◀ 1 ms  ,", #selector(nudgeEarlier)), button("1 ms ▶  .", #selector(nudgeLater)), snapButton
        ])
        writeBPMButton.target = self
        writeBPMButton.action = #selector(writeBPM)
        detectButton.target = self
        detectButton.action = #selector(detectGrid)
        snapButton.state = UserDefaults.standard.object(forKey: "editorSnap") as? Bool ?? true ? .on : .off
        snapButton.target = self
        snapButton.action = #selector(snapChanged)
        gridRow.spacing = 8
        content.addArrangedSubview(gridRow)
        barsField.target = self
        barsField.action = #selector(barsEntered)
        barsField.widthAnchor.constraint(equalToConstant: 50).isActive = true
        let presets = NSSegmentedControl(labels: ["8", "16", "32"], trackingMode: .momentary, target: self, action: #selector(presetBars(_:)))
        let selectionRow = NSStackView(views: [
            button("Start at playhead  I", #selector(setSelectionStart)), button("End at playhead  O", #selector(setSelectionEnd)),
            NSTextField(labelWithString: "Bars"), barsField, presets, selectionLabel
        ])
        selectionRow.spacing = 8
        content.addArrangedSubview(selectionRow)
        for (control, action) in [(playButton, #selector(togglePlayback)), (saveButton, #selector(save)), (undoButton, #selector(undoEdit)),
                                  (redoButton, #selector(redoEdit)), (cutButton, #selector(cutSelection)), (duplicateButton, #selector(duplicateSelection))] {
            control.target = self
            control.action = action
        }
        let editRow = NSStackView(views: [cutButton, duplicateButton, undoButton, redoButton, editLabel, saveButton])
        editRow.spacing = 8
        content.addArrangedSubview(editRow)
        recordButton.target = self
        recordButton.action = #selector(saveGridAndCues)
        let cueRow = NSStackView(views: [
            button("To nearest beat  C", #selector(cueToNearestBeat)), button("Memory cue  M", #selector(addCue)), button("Remove nearest  ⇧M", #selector(removeCue)),
            button("◀ Cue  [", #selector(previousCue)), button("Cue ▶  ]", #selector(nextCue)), cueLabel, recordButton
        ])
        cueRow.spacing = 8
        content.addArrangedSubview(cueRow)
        status.textColor = .secondaryLabelColor
        content.addArrangedSubview(status)
        let help = NSTextField(wrappingLabelWithString: "Click to place the playhead • Scroll to move • Pinch, ⌘/⌥-scroll or ⌘= / ⌘− to zoom, ⌘0 fits • ⌥-drag slides the grid • ← / → beat, ⇧ bar • C nearest beat • Shift+, / . nudges 10 ms • T taps tempo • K click • Esc stops")
        help.textColor = .tertiaryLabelColor
        help.font = .systemFont(ofSize: 11)
        content.addArrangedSubview(help)
        for view in [titleLabel, overview, waveform, status, help] {
            view.widthAnchor.constraint(equalTo: content.widthAnchor).isActive = true
        }
        waveform.onSeek = { [weak self] time in self?.seek(to: time) }
        waveform.onViewChange = { [weak self] in self?.refreshOverview() }
        waveform.onGridDrag = { [weak self] offset in self?.dragGrid(offset) }
        overview.onSelect = { [weak self] time in
            self?.waveform.center(on: time)
            self?.seek(to: time)
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func button(_ title: String, _ action: Selector) -> NSButton { NSButton(title: title, target: self, action: action) }

    func open(_ url: URL) {
        if let window, window.isVisible, self.url == url {
            window.makeKeyAndOrderFront(nil)
            return
        }
        storeSession()
        stopPlayback()
        peaksJob?.cancel()
        detectJob?.cancel()
        detecting = false
        detectUndo = nil
        do {
            let file = try AVAudioFile(forReading: url)
            guard file.length > 0 else { throw EditError("This file has no audio.") }
            playFile = file
            waveform.sampleFile = try AVAudioFile(forReading: url)
            rate = file.processingFormat.sampleRate
            frames = file.length
            engine.stop()
            engine.disconnectNodeOutput(node)
            engine.connect(node, to: engine.mainMixerNode, format: file.processingFormat)
        } catch {
            sorter.report(error)
            return
        }
        self.url = url
        let tagged = (try? sorter.helper("read-bpm", file: url))?["bpm"] as? Double
        taggedBPM = tagged
        if let remembered = sorter.editSessions[url] {
            session = remembered
            status.stringValue = remembered.edited ? "Restored this track's grid and edits." : "Restored this track's grid."
        }
        else if let record = sorter.gridStore.record(for: url), let bpm = record.bpm, let anchor = record.anchor {
            session = EditSession(grid: BeatGrid(bpm: bpm, anchor: anchor))
            session.downbeatSet = true
            session.cues = record.cues.map { ($0 - anchor) / session.grid.beatDuration }
            session.savedCues = session.cues
            status.stringValue = "Restored the grid and \(record.cues.count) memory cue\(record.cues.count == 1 ? "" : "s") recorded for Rekordbox."
        }
        else {
            session = EditSession(grid: BeatGrid(bpm: tagged ?? 120, anchor: 0))
            session.tempoSet = tagged != nil
            status.stringValue = tagged.map { "BPM \(format($0)) from the file's tag. Set a downbeat (⌘\\) and check the grid late in the track." }
                ?? "No BPM tag. Type the BPM or tap it (T), then set a downbeat (⌘\\)."
        }
        playhead = 0
        titleLabel.stringValue = url.lastPathComponent
        window?.title = "Cut Bars — \(url.lastPathComponent)"
        waveform.peaks = nil
        waveform.fitted = true
        refresh()
        loadPeaks(url)
        if monitor == nil {
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in self?.handle(event) == true ? nil : event }
        }
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30, repeats: true) { [weak self] _ in self?.tick() }
        }
        if window?.isVisible != true { window?.center() }
        showWindow(nil)
        window?.makeKeyAndOrderFront(nil)
        window?.makeFirstResponder(waveform)
    }

    func loadPeaks(_ url: URL) {
        let job = CancellationFlag()
        peaksJob = job
        let previous = status.stringValue
        let loading = "Reading waveform… " + previous
        status.stringValue = loading
        peaksQueue.async { [weak self] in
            let peaks = waveformPeaks(url, cancelled: { job.isCancelled })
            DispatchQueue.main.async {
                guard let self, !job.isCancelled, self.url == url else { return }
                // Only replace our own message; anything newer stays.
                if peaks == nil { self.status.stringValue = "Cannot draw this file's waveform." }
                else if self.status.stringValue == loading { self.status.stringValue = previous }
                self.waveform.peaks = peaks
                self.refreshOverview()
            }
        }
    }

    func storeSession() {
        if let url { sorter.editSessions[url] = session }
    }

    // Up to two decimals without trailing zeros: 124, 124.5, 123.25.
    func format(_ bpm: Double) -> String {
        var text = String(format: "%.2f", bpm)
        while text.hasSuffix("0") { text.removeLast() }
        if text.hasSuffix(".") { text.removeLast() }
        return text
    }

    func stamp(_ seconds: Double) -> String {
        String(format: "%d:%06.3f", Int(seconds) / 60, seconds.truncatingRemainder(dividingBy: 60))
    }

    // Recomputes everything derived from the session and pushes it to the controls.
    func refresh() {
        let timeline = timeline
        waveform.timeline = timeline
        waveform.snapping = snapButton.state == .on && session.downbeatSet
        let clicking = clickButton.state == .on
        let tempoSet = session.tempoSet
        metronome.update { $0.grid = timeline.grid; $0.end = timeline.duration; $0.enabled = clicking && tempoSet }
        waveform.showsGrid = tempoSet
        snapButton.toolTip = session.downbeatSet ? "Clicks snap to the finest grid lines shown; zoom in for beats and sixteenths." : "Snapping starts once you set a downbeat (D)."
        playhead = min(playhead, timeline.duration)
        waveform.playhead = playhead
        if bpmField.currentEditor() == nil { bpmField.stringValue = tempoSet ? format(session.grid.bpm) : "" }
        if barsField.currentEditor() == nil { barsField.stringValue = "\(session.bars)" }
        if let selection = session.selection {
            let range = (timeline.grid.barTime(selection), timeline.grid.barTime(selection + session.bars))
            waveform.selection = range
            overview.selection = range
            selectionLabel.stringValue = "Selected \(session.selectionDescription(duration) ?? "")"
        } else {
            waveform.selection = nil
            overview.selection = nil
            selectionLabel.stringValue = "No selection"
        }
        let junctions = timeline.junctions.count
        editLabel.stringValue = session.edited
            ? "\(stamp(duration)) → \(stamp(timeline.duration)) • \(junctions) splice\(junctions == 1 ? "" : "s")\(session.unsaved ? " • unsaved" : "")"
            : "Unedited"
        let cues = session.cueTimes(duration)
        waveform.cues = cues
        overview.cues = cues
        cueLabel.stringValue = "\(cues.count)/\(EditSession.cueLimit) memory cues"
        recordButton.isEnabled = session.downbeatSet && !saving
        recordButton.toolTip = session.downbeatSet
            ? "Record this file's grid and memory cues for the Rekordbox XML. The audio file is not changed."
            : "Set the downbeat (D) first: Rekordbox's bar 1 comes from it."
        window?.isDocumentEdited = session.unsaved
        saveButton.isEnabled = (session.edited || session.downbeatSet) && !saving
        saveButton.toolTip = session.edited
            ? "Save the edit as a new file beside the original, and record its grid and cues for Rekordbox."
            : "No audio edits: record this file's grid and cues for Rekordbox (same as ⌘⇧S)."
        writeBPMButton.isEnabled = tempoSet && taggedBPM != tagBPM && !saving
        writeBPMButton.toolTip = "Write \(format(tagBPM)) to the original's BPM tag (currently \(taggedBPM.map(format) ?? "none")). Saved edits get it automatically."
        undoButton.isEnabled = !session.undo.isEmpty || detectUndo?.undoCount == session.undo.count
        detectButton.isEnabled = !detecting && !saving
        detectButton.title = detecting ? "Detecting…" : "Detect"
        detectButton.toolTip = sorter.detectModel == nil
            ? "Grid detection isn't installed: run zsh build.sh --detect."
            : "Detect the BPM and downbeat with beat_this (about 10 s for a 6-minute track). ⌘Z right after undoes it."
        redoButton.isEnabled = !session.redo.isEmpty
        cutButton.isEnabled = session.selection != nil
        duplicateButton.isEnabled = session.selection != nil
        updatePosition()
        refreshOverview()
    }

    func refreshOverview() {
        let timeline = timeline
        overview.duration = timeline.duration
        overview.visible = (waveform.visibleStart, waveform.visibleStart + waveform.span)
        overview.playhead = playhead
        guard let peaks = waveform.peaks, timeline.length > 0 else { overview.levels = []; return }
        let bins = 600
        var levels: [Float] = []
        for bin in 0..<bins {
            let start = timeline.length * Int64(bin) / Int64(bins), end = timeline.length * Int64(bin + 1) / Int64(bins)
            var level: Float = 0
            for range in timeline.ranges(from: start, count: end - start) {
                if let (low, high) = peaks.extremes(from: range.source, to: range.source + range.count) { level = max(level, -low, high) }
            }
            levels.append(level)
        }
        let loudest = levels.max() ?? 0
        let normalized = loudest > 0 ? levels.map { $0 / loudest } : levels
        if normalized != overview.levels { overview.levels = normalized }
    }

    // Bar and beat on the edited track, as in "5.1".
    func barBeat(_ time: Double) -> String {
        let grid = timeline.grid
        let beats = Int(((time - grid.anchor) / grid.beatDuration + 1e-6).rounded(.down))
        let bar = Int((Double(beats) / 4).rounded(.down))
        return "\(grid.number(bar)).\(beats - bar * 4 + 1)"
    }

    func position(_ time: Double) -> String { "bar \(barBeat(time)) (\(stamp(time)))" }

    func updatePosition() {
        positionLabel.stringValue = "Bar \(barBeat(playhead))   \(stamp(playhead)) / \(stamp(timeline.duration))"
        playButton.title = playing ? "Pause  Space" : "Play  Space"
        waveform.playhead = playhead
        overview.playhead = playhead
    }

    func handle(_ event: NSEvent) -> Bool {
        guard let window, event.window === window, NSApp.modalWindow == nil else { return false }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        let key = event.charactersIgnoringModifiers?.lowercased() ?? ""
        if flags.contains(.command) {
            switch key {
            case "s": flags.contains(.shift) ? saveGridAndCues() : save(); return true
            case "d": duplicateSelection(); return true
            case "z": flags.contains(.shift) ? redoEdit() : undoEdit(); return true
            case "\\": setDownbeat(); return true  // Rekordbox's key for it; D works too.
            // The usual Mac zoom keys, alongside plain = / − / 0.
            case "=", "+": waveform.zoom(by: 2, around: waveform.x(for: playhead)); return true
            case "-", "_": waveform.zoom(by: 0.5, around: waveform.x(for: playhead)); return true
            case "0": waveform.fit(); return true
            default: return false
            }
        }
        if window.firstResponder is NSText {
            // Typing a BPM or bar count; Esc leaves the field without applying.
            if event.keyCode == 53 { refresh(); window.makeFirstResponder(waveform); return true }
            return false
        }
        guard !flags.contains(.control), !flags.contains(.option) else { return false }
        let shift = flags.contains(.shift)
        switch event.keyCode {
        case 49: togglePlayback(); return true
        case 53: stopAndKeepPosition(); return true
        case 51, 117: cutSelection(); return true
        case 123: step(-1, bars: shift); return true
        case 124: step(1, bars: shift); return true
        default: break
        }
        switch key {
        case "=", "+": waveform.zoom(by: 2, around: waveform.x(for: playhead)); return true
        case "-", "_": waveform.zoom(by: 0.5, around: waveform.x(for: playhead)); return true
        case "0", ")": waveform.fit(); return true
        case "d": setDownbeat(); return true
        case "t": tapPressed(); return true
        case "k": clickButton.state = clickButton.state == .on ? .off : .on; clickChanged(); return true
        case "i": setSelectionStart(); return true
        case "o": setSelectionEnd(); return true
        case "p": previewSelection(); return true
        case "x": cutSelection(); return true
        // Rekordbox's keys: C is CUE with quantize, M stores a memory cue.
        case "c": cueToNearestBeat(); return true
        case "m": shift ? removeCue() : addCue(); return true
        case "[", "{": previousCue(); return true
        case "]", "}": nextCue(); return true
        case ",", "<": nudge(shift ? -0.01 : -0.001); return true
        case ".", ">": nudge(shift ? 0.01 : 0.001); return true
        default: return false
        }
    }

    // MARK: Playback

    func seek(to time: Double) {
        playhead = min(max(0, time), timeline.duration)
        if playing { play(from: playhead) } else { updatePosition() }
    }

    // Moves to the next beat (or bar) line, so a playhead between lines snaps onto the grid.
    func step(_ direction: Int, bars: Bool) {
        let grid = timeline.grid
        let unit = bars ? grid.barDuration : grid.beatDuration
        let position = (playhead - grid.anchor) / unit
        let target = direction > 0 ? (position + 1e-6).rounded(.down) + 1 : (position - 1e-6).rounded(.up) - 1
        seek(to: grid.anchor + target * unit)
        waveform.follow(playhead)
    }

    @objc func togglePlayback() {
        if playing { stopAndKeepPosition() }
        else { play(from: playhead >= timeline.duration - 0.05 ? 0 : playhead) }
    }

    func play(from time: Double) {
        stopPlayback()
        let timeline = timeline
        guard let file = playFile, timeline.length > 0 else { return }
        let start = min(max(0, Int64((time * rate).rounded())), timeline.length - 1)
        do {
            if !engine.isRunning { engine.prepare(); try engine.start() }
        } catch {
            status.stringValue = "Cannot start audio output: \(error.localizedDescription)"
            return
        }
        for range in timeline.ranges(from: start, count: timeline.length - start) {
            node.scheduleSegment(file, startingFrame: range.source, frameCount: AVAudioFrameCount(range.count), at: nil)
        }
        playStart = start
        playEnd = timeline.length
        playhead = Double(start) / rate
        // Start slightly ahead so the click can be lined up with the first sample.
        let startHost = mach_absolute_time() + AVAudioTime.hostTime(forSeconds: 0.05)
        node.play(at: AVAudioTime(hostTime: startHost))
        metronome.update { $0.sync = (startHost, Double(start) / rate) }
        playing = true
        updatePosition()
    }

    func renderedPosition() -> Int64? {
        guard let nodeTime = node.lastRenderTime, let playerTime = node.playerTime(forNodeTime: nodeTime) else { return nil }
        return playStart + max(0, Int64(Double(playerTime.sampleTime) * rate / playerTime.sampleRate))
    }

    func tick() {
        guard playing, let position = renderedPosition() else { return }
        if position >= playEnd {
            stopPlayback()
            playhead = Double(playEnd) / rate
        } else {
            playhead = Double(position) / rate
        }
        waveform.follow(playhead)
        updatePosition()
    }

    func stopPlayback() {
        node.stop()
        metronome.update { $0.sync = nil }
        playing = false
    }

    func stopAndKeepPosition() {
        if playing, let position = renderedPosition() { playhead = min(Double(position), Double(playEnd)) / rate }
        stopPlayback()
        updatePosition()
    }

    // Plays from two bars before the selection, which is also where the last edit's splice sits.
    @objc func previewSelection() {
        guard let selection = session.selection else { status.stringValue = "Set a selection first (I)."; return }
        let grid = timeline.grid
        let start = max(0, grid.barTime(selection - 2))
        waveform.follow(start)
        play(from: start)
    }

    // MARK: Grid

    @objc func snapChanged() {
        UserDefaults.standard.set(snapButton.state == .on, forKey: "editorSnap")
        refresh()
        window?.makeFirstResponder(waveform)
    }

    // Grid changes keep edits on the same bar numbers, which then sit on the adjusted lines.
    // Placing the grid (rather than only changing tempo) turns on snapping.
    func changeGrid(_ grid: BeatGrid, remap: Bool = false, placed: Bool = false) {
        guard grid.bpm.isFinite, (40...300).contains(grid.bpm), grid.anchor.isFinite else { return }
        // Typing or tapping a BPM sets the tempo; anything that places the grid needs one.
        if placed || remap { guard tempoKnown() else { return } } else { session.tempoSet = true }
        detectUndo = nil
        // Unedited playback does not depend on the grid, so tapping and nudging can continue live.
        if playing && session.edited { stopAndKeepPosition() }
        if remap { session.regrid(grid) } else { session.grid = grid }
        if placed { session.downbeatSet = true }
        refresh()
    }

    @objc func bpmEntered() {
        let text = bpmField.stringValue.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")
        guard let bpm = Double(text), (40...300).contains(bpm) else {
            status.stringValue = "Enter a BPM between 40 and 300."
            bpmField.stringValue = session.tempoSet ? format(session.grid.bpm) : ""
            return
        }
        changeGrid(BeatGrid(bpm: bpm, anchor: session.grid.anchor))
        window?.makeFirstResponder(waveform)
    }

    @objc func tapPressed() { tap(at: ProcessInfo.processInfo.systemUptime) }

    func tap(at time: TimeInterval) {
        guard let bpm = tapTempo.tap(at: time) else {
            status.stringValue = "Tap along on each beat… \(TapTempo.needed - tapTempo.taps.count) more."
            return
        }
        guard (40...300).contains(bpm) else { status.stringValue = "Tapped \(format(bpm)) BPM, outside 40–300. Pause, then tap again."; return }
        changeGrid(BeatGrid(bpm: bpm, anchor: session.grid.anchor))
        status.stringValue = "Tapped \(format(bpm)) BPM from \(tapTempo.taps.count) taps. Keep tapping to refine; pause 2 s to start over. Then set the downbeat (D) and fine-tune."
    }

    @objc func clickChanged() {
        UserDefaults.standard.set(clickButton.state == .on, forKey: "editorMetronome")
        refresh()
        window?.makeFirstResponder(waveform)
    }

    // The grid's tempo as written to tags: two decimals at most.
    var tagBPM: Double { (session.grid.bpm * 100).rounded() / 100 }

    // Changes only the original's BPM tag, with the same lossless writer as comments and Genre.
    @objc func writeBPM() {
        guard let url, !saving else { return }
        guard !sorter.repairingGenres else { status.stringValue = "Genre repair is running; tag writes are unavailable until it finishes."; return }
        let bpm = tagBPM
        do {
            let json = String(decoding: try JSONSerialization.data(withJSONObject: ["bpm": bpm]), as: UTF8.self)
            _ = try sorter.helper("write-bpm", file: url, text: json)
            taggedBPM = bpm
            status.stringValue = "Wrote BPM \(format(bpm)) to \(url.lastPathComponent). Its audio and other tags are unchanged."
        } catch {
            status.stringValue = "BPM not written: \(error.localizedDescription)"
        }
        refresh()
    }

    @objc func halveTempo() { if tempoKnown() { changeGrid(BeatGrid(bpm: session.grid.bpm / 2, anchor: session.grid.anchor)) } }
    @objc func doubleTempo() { if tempoKnown() { changeGrid(BeatGrid(bpm: session.grid.bpm * 2, anchor: session.grid.anchor)) } }

    // Grid-based actions wait for a real BPM on files without a tag.
    func tempoKnown() -> Bool {
        guard !session.tempoSet else { return true }
        status.stringValue = "This file has no BPM tag: type the BPM or tap it (T) first."
        NSSound.beep()
        return false
    }

    // The playhead is on the edited track; the downbeat is stored on the original.
    func sourceTime(_ output: Double) -> Double? {
        let frame = Int64((output * rate).rounded())
        guard let range = timeline.ranges(from: frame, count: 1).first else { return output >= timeline.duration ? duration : nil }
        return Double(range.source) / rate
    }

    @objc func setDownbeat() {
        guard tempoKnown(), let source = sourceTime(playhead) else { return }
        let selectionTime = session.selection.map { timeline.grid.barTime($0) }
        changeGrid(BeatGrid(bpm: session.grid.bpm, anchor: source), remap: true, placed: true)
        // Keep the selection on whichever bar line is now nearest to where it was.
        if let selectionTime { session.selection = timeline.grid.nearestBar(to: selectionTime) }
        refresh()
        status.stringValue = "Downbeat set at \(stamp(playhead)). Check that later bar lines still sit on the kicks; adjust BPM if they drift."
    }

    func nudge(_ seconds: Double) { changeGrid(BeatGrid(bpm: session.grid.bpm, anchor: session.grid.anchor + seconds), placed: true) }
    @objc func nudgeEarlier() { nudge(-0.001) }
    @objc func nudgeLater() { nudge(0.001) }

    func dragGrid(_ offset: Double?) {
        guard let offset else { dragAnchor = nil; return }
        if dragAnchor == nil { dragAnchor = session.grid.anchor }
        changeGrid(BeatGrid(bpm: session.grid.bpm, anchor: dragAnchor! + offset), placed: true)
    }

    // MARK: Selection and edits

    @objc func setSelectionStart() {
        guard tempoKnown() else { return }
        let grid = timeline.grid
        session.selection = max(grid.firstBar, grid.nearestBar(to: playhead))
        refresh()
    }

    @objc func setSelectionEnd() {
        guard let selection = session.selection else { status.stringValue = "Set the selection start first (I)."; return }
        let end = timeline.grid.nearestBar(to: playhead)
        guard end > selection else { status.stringValue = "The end must be after the selection start."; return }
        session.bars = end - selection
        refresh()
    }

    @objc func barsEntered() {
        guard let bars = Int(barsField.stringValue.trimmingCharacters(in: .whitespaces)), (1...1024).contains(bars) else {
            status.stringValue = "Enter a whole number of bars."
            barsField.stringValue = "\(session.bars)"
            return
        }
        session.bars = bars
        refresh()
        window?.makeFirstResponder(waveform)
    }

    @objc func presetBars(_ sender: NSSegmentedControl) {
        session.bars = [8, 16, 32][sender.selectedSegment]
        refresh()
    }

    func applyEdit(duplicate: Bool) {
        guard tempoKnown() else { return }
        if playing { stopAndKeepPosition() }
        let before = session.selectionDescription(duration)
        do {
            try session.edit(duplicate: duplicate, duration: duration)
            if !duplicate, let selection = session.selection { playhead = timeline.grid.barTime(selection) }
            refresh()
            status.stringValue = duplicate
                ? "Duplicated \(before ?? ""); the copy is selected, so ⌘D again extends further. P plays into it."
                : "Cut \(before ?? ""). The red line marks the splice; P plays across it."
        } catch {
            status.stringValue = error.localizedDescription
            NSSound.beep()
        }
    }

    @objc func cutSelection() { applyEdit(duplicate: false) }
    @objc func duplicateSelection() { applyEdit(duplicate: true) }

    @objc func undoEdit() {
        if let before = detectUndo, before.undoCount == session.undo.count {
            if playing { stopAndKeepPosition() }
            session = before.session
            detectUndo = nil
            refresh()
            status.stringValue = "Back to the grid from before Detect."
            return
        }
        if playing && !session.undo.isEmpty { stopAndKeepPosition() }
        if session.undoLast() { refresh() }
    }

    @objc func redoEdit() {
        if playing && !session.redo.isEmpty { stopAndKeepPosition() }
        if session.redoLast() { refresh() }
    }

    // MARK: Memory cues

    // A stopped playhead is already where a click or arrow key put it, snapped or not. While
    // playing, a cue snaps to the nearest beat when snapping is on.
    @objc func addCue() {
        // Cues are kept in beats, so they need the real tempo first.
        guard tempoKnown() else { return }
        let grid = timeline.grid
        var time = playing ? (renderedPosition().map { Double($0) / rate } ?? playhead) : playhead
        if playing && waveform.snapping { time = grid.anchor + ((time - grid.anchor) / grid.beatDuration).rounded() * grid.beatDuration }
        time = min(max(0, time), timeline.duration)
        do {
            try session.addCue(at: time, duration: duration)
            refresh()
            status.stringValue = "Memory cue at \(position(time)). ⇧M removes the nearest cue; [ and ] jump between cues."
        } catch {
            status.stringValue = error.localizedDescription
            NSSound.beep()
        }
    }

    @objc func removeCue() {
        do {
            let time = try session.removeCue(nearest: playhead, duration: duration)
            refresh()
            status.stringValue = "Removed the memory cue at \(position(time)). ⌘Z brings it back."
        } catch {
            status.stringValue = error.localizedDescription
            NSSound.beep()
        }
    }

    func jumpToCue(_ direction: Int) {
        let times = session.cueTimes(duration)
        guard let target = direction > 0 ? times.first(where: { $0 > playhead + 1e-4 }) : times.last(where: { $0 < playhead - 1e-4 }) else { NSSound.beep(); return }
        seek(to: target)
        waveform.follow(playhead)
    }

    // Like Rekordbox's CUE with quantize on: stop, and put the playhead on the nearest beat.
    @objc func cueToNearestBeat() {
        guard tempoKnown() else { return }
        if playing { stopAndKeepPosition() }
        let grid = timeline.grid
        seek(to: grid.anchor + ((playhead - grid.anchor) / grid.beatDuration).rounded() * grid.beatDuration)
        waveform.follow(playhead)
    }

    @objc func previousCue() { jumpToCue(-1) }
    @objc func nextCue() { jumpToCue(1) }

    // MARK: Saving

    @objc func save() {
        guard !saving, let url else { return }
        // Without audio edits there is no new file to write; ⌘S records the grid and cues instead.
        guard session.edited else {
            if session.downbeatSet { saveGridAndCues() }
            else { status.stringValue = "Nothing to save yet: set the downbeat (⌘\\) to record the grid and cues, or cut or duplicate some bars." }
            return
        }
        let output = sorter.editURL(for: url)
        guard !FileManager.default.fileExists(atPath: output.path) else {
            status.stringValue = "\(output.lastPathComponent) already exists. Move it to Trash or rename it, then save again."
            NSSound.beep()
            return
        }
        let timeline = timeline
        let bpm = tagBPM
        let request: [String: Any] = ["segments": timeline.segments, "fade": Int((rate * 0.005).rounded()), "output": output.path, "bpm": bpm]
        let mp3 = url.pathExtension.lowercased() == "mp3"
        let pieces = session.pieces
        // The edit's own grid and cues, recorded once the file is verified.
        let placed = session.downbeatSet, cueBeats = session.cues, cueTimes = session.cueTimes(duration)
        saving = true
        refresh()
        status.stringValue = mp3 ? "Saving… decoding and re-encoding the MP3." : "Saving…"
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let result = Result { () throws -> [String: Any] in
                var request = request
                let work = FileManager.default.temporaryDirectory.appendingPathComponent("AudioSorter-\(UUID().uuidString)", isDirectory: true)
                defer { try? FileManager.default.removeItem(at: work) }
                if mp3 {
                    try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
                    let decoded = work.appendingPathComponent("decoded.wav")
                    try decodeToWAV(url, to: decoded)
                    request["decoded"] = decoded.path
                }
                let json = String(decoding: try JSONSerialization.data(withJSONObject: request), as: UTF8.self)
                let report = try sorter.helper("cut", file: url, text: json)
                // Read the new file back the way a player would: it must decode to exactly the edited length.
                let length = (try? AVAudioFile(forReading: output))?.length
                guard length == timeline.length else {
                    try? sorter.sendToTrash(output)
                    let found = length.map { "\($0) frames instead of \(timeline.length)" } ?? "could not be read"
                    throw EditError("The saved file failed verification (\(found)), so it was moved to Trash. The original is unchanged.")
                }
                return report
            }
            DispatchQueue.main.async { [self] in
                saving = false
                refresh()
                switch result {
                case .success(let report):
                    if self.url == url { session.savedPieces = pieces } else { sorter.editSessions[url]?.savedPieces = pieces }
                    let dropped = report["dropped"] as? [String] ?? []
                    let note = dropped.isEmpty ? "" : " Removed timeline-specific metadata that no longer lines up: \(dropped.joined(separator: ", "))."
                    var recorded = " Not recorded for Rekordbox: the downbeat was not set. Open the edit, set it (D), then ⌘⇧S."
                    if placed {
                        do {
                            try sorter.recordGrid(output, grid: timeline.grid, cues: cueTimes, duration: timeline.duration, sampleRate: timeline.rate)
                            if self.url == url { session.savedCues = cueBeats } else { sorter.editSessions[url]?.savedCues = cueBeats }
                            recorded = " Recorded its grid and \(cueTimes.count) memory cue\(cueTimes.count == 1 ? "" : "s") for Rekordbox."
                        } catch {
                            recorded = " Not recorded for Rekordbox: \(error.localizedDescription)"
                        }
                    }
                    status.stringValue = "Saved \(output.lastPathComponent) (\(stamp(timeline.duration)), BPM \(format(bpm)) tagged). The original is unchanged.\(note)\(recorded)"
                    sorter.editSaved(output)
                case .failure(let error):
                    status.stringValue = "Not saved: \(error.localizedDescription)"
                    if let window { NSAlert(error: error).beginSheetModal(for: window) }
                }
            }
        }
    }

    // MARK: Detection

    // Runs beat_this on the track as AVFoundation decodes it, so detected times are on the
    // editor's timeline, then applies the grid as if typed and placed (see detect.py).
    @objc func detectGrid() {
        guard let url, !detecting, !saving else { return }
        guard let model = sorter.detectModel else {
            status.stringValue = "Grid detection isn't installed: run zsh build.sh --detect, then reopen the app."
            NSSound.beep()
            return
        }
        let job = CancellationFlag()
        detectJob = job
        detecting = true
        refresh()
        status.stringValue = "Detecting beats… (about 10 s for a 6-minute track)"
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let result = Result { () throws -> [String: Any] in
                let work = FileManager.default.temporaryDirectory.appendingPathComponent("AudioSorter-\(UUID().uuidString)", isDirectory: true)
                try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
                defer { try? FileManager.default.removeItem(at: work) }
                let decoded = work.appendingPathComponent("decoded.wav")
                try decodeToWAV(url, to: decoded)
                let request = String(decoding: try JSONSerialization.data(withJSONObject: ["model": model.path]), as: UTF8.self)
                return try sorter.helper("detect", file: decoded, text: request)
            }
            DispatchQueue.main.async { [self] in
                // Opening another track or closing the editor abandons the result.
                guard !job.isCancelled, self.url == url else { return }
                detecting = false
                switch result {
                case .success(let report): applyDetected(report)
                case .failure(let error):
                    refresh()
                    status.stringValue = "Detection failed: \(error.localizedDescription)"
                    NSSound.beep()
                }
            }
        }
    }

    func applyDetected(_ report: [String: Any]) {
        guard let bpm = report["bpm"] as? Double, let anchor = report["anchor"] as? Double, (40...300).contains(bpm) else {
            refresh()
            status.stringValue = "Detection returned no usable grid."
            return
        }
        if playing && session.edited { stopAndKeepPosition() }
        let before = session
        let selectionTime = session.selection.map { timeline.grid.barTime($0) }
        session.regrid(BeatGrid(bpm: bpm, anchor: anchor))
        session.tempoSet = true
        session.downbeatSet = true
        if let selectionTime { session.selection = timeline.grid.nearestBar(to: selectionTime) }
        detectUndo = (before, session.undo.count)
        refresh()
        let grid = timeline.grid
        let onGrid = Int(((report["confidence"] as? Double ?? 0) * 100).rounded())
        var message = "Detected \(format(bpm)) BPM, bar 1 at \(stamp(grid.barTime(grid.firstBar))), \(onGrid)% of beats on the grid."
        if let share = report["downbeatConfidence"] as? Double, share < 0.8 {
            message += " The downbeat is uncertain (\(Int((share * 100).rounded()))% of bars agree): check bar 1, and press ⌘\\ on the first kick if it's off."
        }
        if onGrid < 80 { message += " Few beats sit on one grid: try ÷2 or ×2, or set the BPM by hand." }
        if let drift = report["drift"] as? Double, drift > 0.03 {
            message += " The tempo drifts by up to \(Int((drift * 1000).rounded())) ms, so no constant grid fits everywhere."
        }
        status.stringValue = message + " Check a late bar with the click (K); ⌘Z undoes."
    }

    // Records the original's grid, with cues mapped back to where they play in it. The audio
    // file is not touched: the record lives in the app's store and the rekordbox XML.
    @objc func saveGridAndCues() {
        guard let url, !saving else { return }
        guard session.downbeatSet else {
            status.stringValue = "Set the downbeat (D) first: Rekordbox's bar 1 comes from it."
            NSSound.beep()
            return
        }
        let recorded: (cues: Int, dropped: Int)
        do {
            recorded = try sorter.recordOriginal(url, session: session, rate: rate, frames: frames)
        } catch {
            status.stringValue = "Not recorded: \(error.localizedDescription)"
            NSSound.beep()
            return
        }
        session.savedCues = session.cues
        refresh()
        let grid = session.grid
        status.stringValue = "Recorded \(url.lastPathComponent) for Rekordbox: BPM \(format(grid.bpm)), bar 1 at \(stamp(grid.barTime(grid.firstBar))), \(recorded.cues) memory cue\(recorded.cues == 1 ? "" : "s"). The audio file is unchanged." + Sorter.droppedNote(recorded.dropped)
    }

    func windowWillClose(_ notification: Notification) {
        stopPlayback()
        engine.stop()
        peaksJob?.cancel()
        detectJob?.cancel()
        detecting = false
        storeSession()
        timer?.invalidate()
        timer = nil
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = Sorter()
app.delegate = delegate
app.run()
