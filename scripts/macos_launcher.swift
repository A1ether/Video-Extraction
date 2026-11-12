// vextract 的原生 macOS 外壳：启动内置 Python 服务，用 WKWebView 显示界面。
// 由 scripts/build_standalone_app.py 用 swiftc 编译进 .app；不要在终端直接运行。

import Cocoa
import Darwin
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var stoppingServer = false
    private var uiLoaded = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        startServer()
        // 从 Finder/终端启动时，系统有时不把新窗口带到前台；延迟再抢一次焦点。
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            self?.bringToFront()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            bringToFront()
        }
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        stoppingServer = true
        guard let process = server, process.isRunning else { return }
        process.terminate()
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline {
            usleep(100_000)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
    }

    // MARK: - 菜单

    private func buildMenu() {
        let mainMenu = NSMenu()

        let appItem = NSMenuItem()
        mainMenu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "关于 vextract",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""
        )
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(
            withTitle: "退出 vextract",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        mainMenu.addItem(editItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(NSMenuItem.separator())
        editMenu.addItem(withTitle: "剪切", action: Selector(("cut:")), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: Selector(("copy:")), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: Selector(("paste:")), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: Selector(("selectAll:")), keyEquivalent: "a")
        editItem.submenu = editMenu

        let viewItem = NSMenuItem()
        mainMenu.addItem(viewItem)
        let viewMenu = NSMenu(title: "查看")
        let backItem = NSMenuItem(title: "后退", action: #selector(goBack), keyEquivalent: "[")
        backItem.target = self
        viewMenu.addItem(backItem)
        let forwardItem = NSMenuItem(title: "前进", action: #selector(goForward), keyEquivalent: "]")
        forwardItem.target = self
        viewMenu.addItem(forwardItem)
        viewMenu.addItem(NSMenuItem.separator())
        let reloadItem = NSMenuItem(title: "重新载入", action: #selector(reloadPage), keyEquivalent: "r")
        reloadItem.target = self
        viewMenu.addItem(reloadItem)
        let logItem = NSMenuItem(title: "打开日志", action: #selector(openLog), keyEquivalent: "")
        logItem.target = self
        viewMenu.addItem(logItem)
        viewItem.submenu = viewMenu

        NSApp.mainMenu = mainMenu
    }

    @objc private func goBack() {
        webView?.goBack()
    }

    @objc private func goForward() {
        webView?.goForward()
    }

    @objc private func reloadPage() {
        if let url = webView?.url {
            webView.load(URLRequest(url: url))
        }
    }

    @objc private func openLog() {
        let log = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/vextract.log")
        NSWorkspace.shared.activateFileViewerSelecting([log])
    }

    // MARK: - 窗口

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsMagnification = true

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1120, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "vextract"
        window.minSize = NSSize(width: 900, height: 620)
        window.collectionBehavior = [.moveToActiveSpace]
        window.contentView = webView
        window.delegate = self
        window.center()
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        bringToFront()
        showLoading("正在启动本地服务…")
    }

    private func bringToFront() {
        guard let window = window else { return }
        if #available(macOS 14.0, *) {
            NSApp.activate()
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
        _ = NSRunningApplication.current.activate(options: [.activateAllWindows])
        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
    }

    private func showLoading(_ text: String) {
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        *{box-sizing:border-box}body{margin:0;height:100vh;display:flex;align-items:center;
        justify-content:center;background:#0b1220;color:#cbd5e1;
        font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
        .box{text-align:center}.spin{width:34px;height:34px;border:3px solid #334155;
        border-top-color:#38bdf8;border-radius:50%;margin:0 auto 18px;
        animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
        p{font-size:14px;opacity:.8;margin:0}
        </style></head><body><div class="box"><div class="spin"></div><p>\(text)</p></div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - 服务进程

    private func startServer() {
        guard let resource = Bundle.main.resourceURL else {
            fail("找不到 app 的 Resources 目录。")
            return
        }
        let binary = resource.appendingPathComponent("server/vextract")
        guard FileManager.default.isExecutableFile(atPath: binary.path) else {
            fail("找不到内置服务：\(binary.path)")
            return
        }

        let process = Process()
        process.executableURL = binary
        var env = ProcessInfo.processInfo.environment
        env["VEXTRACT_NATIVE_SERVER"] = "1"
        env["VEXTRACT_NO_BROWSER"] = "1"
        env["VEXTRACT_PORT"] = "8787"
        process.environment = env

        let logURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/vextract.log")
        try? FileManager.default.createDirectory(
            at: logURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            process.standardOutput = handle
            process.standardError = handle
        }

        process.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                self?.serverExited(proc)
            }
        }

        do {
            try process.run()
        } catch {
            fail("启动内置服务失败：\(error.localizedDescription)")
            return
        }
        server = process
        pollHealth(attempt: 0)
    }

    private func pollHealth(attempt: Int) {
        if attempt >= 60 {
            fail("本地服务没有在 30 秒内就绪。\n日志：~/Library/Logs/vextract.log")
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            for port in 8787...8799 {
                guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { continue }
                if self.isHealthy(url) {
                    DispatchQueue.main.async {
                        self.loadUI(port: port)
                    }
                    return
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                self.pollHealth(attempt: attempt + 1)
            }
        }
    }

    private func isHealthy(_ url: URL) -> Bool {
        var request = URLRequest(url: url)
        request.timeoutInterval = 0.4
        let semaphore = DispatchSemaphore(value: 0)
        var healthy = false
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data = data,
               let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               object["yt_dlp"] != nil {
                healthy = true
            }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 1.0)
        return healthy
    }

    private func loadUI(port: Int) {
        guard let url = URL(string: "http://127.0.0.1:\(port)/") else { return }
        uiLoaded = true
        webView.load(URLRequest(url: url))
        bringToFront()
    }

    private func serverExited(_ process: Process) {
        guard !stoppingServer else { return }
        if !uiLoaded {
            fail("本地服务启动失败（退出码 \(process.terminationStatus)）。\n日志：~/Library/Logs/vextract.log")
            return
        }
        let cleanExit = (process.terminationReason == .exit && process.terminationStatus == 0)
            || (process.terminationReason == .uncaughtSignal && process.terminationStatus == SIGTERM)
        if cleanExit {
            NSApp.terminate(nil)
        } else {
            alert(
                "vextract 服务已停止",
                "退出码 \(process.terminationStatus)。\n日志：~/Library/Logs/vextract.log"
            )
            NSApp.terminate(nil)
        }
    }

    private func fail(_ message: String) {
        showLoading("启动失败")
        alert("vextract 启动失败", message)
        NSApp.terminate(nil)
    }

    private func alert(_ title: String, _ message: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.addButton(withTitle: "好")
        alert.runModal()
    }

    // MARK: - 导航与下载

    private func isLocal(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return host == "127.0.0.1" || host == "localhost" || host == "::1"
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow)
            return
        }
        if isLocal(url) {
            if url.query?.contains("attachment=1") == true {
                decisionHandler(.cancel)
                download(url)
                return
            }
            decisionHandler(.allow)
            return
        }
        if url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isLocal(url) {
                if url.query?.contains("attachment=1") == true {
                    download(url)
                } else {
                    webView.load(URLRequest(url: url))
                }
            } else if url.scheme == "http" || url.scheme == "https" {
                NSWorkspace.shared.open(url)
            }
        }
        return nil
    }

    private func download(_ url: URL) {
        let task = URLSession.shared.downloadTask(with: url) { [weak self] tempURL, response, error in
            DispatchQueue.main.async {
                guard let self = self else { return }
                if let error = error {
                    self.alert("保存失败", error.localizedDescription)
                    return
                }
                guard let tempURL = tempURL else {
                    self.alert("保存失败", "下载没有返回文件。")
                    return
                }
                let downloads = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
                    ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Downloads")
                try? FileManager.default.createDirectory(at: downloads, withIntermediateDirectories: true)

                var name = response?.suggestedFilename ?? url.lastPathComponent
                if let http = response as? HTTPURLResponse,
                   let disposition = http.value(forHTTPHeaderField: "Content-Disposition"),
                   let range = disposition.range(of: "filename*=UTF-8''") {
                    let encoded = String(disposition[range.upperBound...])
                    if let decoded = encoded.removingPercentEncoding, !decoded.isEmpty {
                        name = decoded
                    }
                }
                if name.isEmpty {
                    name = "vextract-download"
                }

                var dest = downloads.appendingPathComponent(name)
                var index = 1
                while FileManager.default.fileExists(atPath: dest.path) {
                    let base = (name as NSString).deletingPathExtension
                    let ext = (name as NSString).pathExtension
                    let suffix = ext.isEmpty ? "" : ".\(ext)"
                    dest = downloads.appendingPathComponent("\(base)-\(index)\(suffix)")
                    index += 1
                }
                do {
                    try FileManager.default.moveItem(at: tempURL, to: dest)
                    NSWorkspace.shared.activateFileViewerSelecting([dest])
                } catch {
                    self.alert("保存失败", "\(error.localizedDescription)\n目标：\(dest.path)")
                }
            }
        }
        task.resume()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
