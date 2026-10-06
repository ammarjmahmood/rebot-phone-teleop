import Foundation
import simd
import UIKit

struct ArmStatus: Decodable {
    var owner: Bool = false
    var active: Bool = false
    var mode: String?
    var note: String?
    var calibrated: Bool = false
    var tilt: Bool = true
    var torque: Bool = false
    var fault: String?
    var remote: Bool = false
    var joints: [Double] = []
    var gripper: Double?
    var phase: String = ""
    var view: String?
    var arm: String?
    var arms: [String]?
}

@MainActor
final class ArmLink: ObservableObject {
    @Published var serverURL: String = UserDefaults.standard.string(forKey: "serverURL") ?? ""
    @Published var connected = false
    @Published var paired = Keychain.read("session") != nil
    @Published var status = ArmStatus()
    @Published var message: String?
    @Published var moving = false
    @Published var grip: Double = 1.0
    @Published var tilt = true
    @Published var view: String = UserDefaults.standard.string(forKey: "view") ?? "behind"

    private var task: URLSessionWebSocketTask?
    private var retry: Task<Void, Never>?
    private var wantConnected = false
    private var lastSent = Date.distantPast
    private var lastNote: String?
    private let haptic = UINotificationFeedbackGenerator()

    func open(_ url: URL) async {
        guard url.scheme == "rebotteleop", let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else { return }
        let address = items.first { $0.name == "url" }?.value
        let code = items.first { $0.name == "code" }?.value
        if let address, !address.isEmpty {
            serverURL = address
            UserDefaults.standard.set(address, forKey: "serverURL")
        }
        if let code, !code.isEmpty {
            disconnect()
            await pair(code: code)
        } else if paired {
            connect()
        }
    }

    private var baseURL: String {
        var text = serverURL.trimmingCharacters(in: .whitespaces)
        while text.hasSuffix("/") { text.removeLast() }
        if !text.contains("://") { text = "http://" + text }
        return text
    }

    func pair(code: String) async {
        guard let url = URL(string: baseURL + "/api/session"), url.host != nil else {
            message = "Check the computer address"
            return
        }
        serverURL = baseURL
        UserDefaults.standard.set(serverURL, forKey: "serverURL")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["token": code.trimmingCharacters(in: .whitespaces)])
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            guard (response as? HTTPURLResponse)?.statusCode == 200, let session = body["session"] as? String else {
                message = body["detail"] as? String ?? "Pairing failed"
                return
            }
            Keychain.save("session", session)
            paired = true
            message = "Paired"
            connect()
        } catch {
            message = "Cannot reach the computer: \(error.localizedDescription)"
        }
    }

    func forget() {
        disconnect()
        wantConnected = false
        Keychain.delete("session")
        paired = false
    }

    func connect() {
        retry?.cancel()
        retry = nil
        guard let session = Keychain.read("session") else { return }
        if Self.sessionExpired(session) {
            expire("Pairing expired. Show a new pairing code on the computer and pair again.")
            return
        }
        let text = baseURL.replacingOccurrences(of: "https://", with: "wss://").replacingOccurrences(of: "http://", with: "ws://")
        guard let url = URL(string: text + "/ws/app") else {
            message = "Check the computer address"
            return
        }
        disconnect()
        wantConnected = true
        message = "Connecting…"
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        request.setValue("Bearer " + session, forHTTPHeaderField: "Authorization")
        let task = URLSession.shared.webSocketTask(with: request)
        self.task = task
        task.resume()
        receive(task)
    }

    func disconnect() {
        retry?.cancel()
        retry = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        connected = false
        moving = false
    }

    func pause() {
        wantConnected = false
        disconnect()
    }

    func resume() {
        if paired && !connected { connect() }
    }

    private func expire(_ text: String) {
        disconnect()
        wantConnected = false
        Keychain.delete("session")
        paired = false
        message = text
    }

    private func scheduleRetry() {
        guard wantConnected, paired, retry == nil else { return }
        retry = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            guard !Task.isCancelled else { return }
            await MainActor.run {
                guard let self, self.wantConnected, !self.connected else { return }
                self.retry = nil
                self.connect()
            }
        }
    }

    private static func sessionExpired(_ session: String) -> Bool {
        guard var value = session.split(separator: ".").first.map(String.init) else { return false }
        value = value.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        while value.count % 4 != 0 { value += "=" }
        guard let data = Data(base64Encoded: value),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let exp = object["exp"] as? Double else { return false }
        return Date().timeIntervalSince1970 >= exp - 30
    }

    private func receive(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.task === task else { return }
                switch result {
                case .failure(let error):
                    let wasConnected = self.connected
                    let code = task.closeCode.rawValue
                    let http = (task.response as? HTTPURLResponse)?.statusCode
                    self.task = nil
                    self.connected = false
                    self.moving = false
                    if code == 4401 {
                        self.expire("The computer no longer accepts this phone. Show a new pairing code and pair again.")
                        return
                    }
                    if !wasConnected && (http == 401 || http == 403) {
                        self.wantConnected = false
                        self.message = "The computer refused this phone. Check the address, or Forget this computer and pair again."
                        return
                    }
                    self.message = (wasConnected ? "Connection lost" : "Cannot reach the computer") + ". Retrying… (\(error.localizedDescription))"
                    self.scheduleRetry()
                case .success(let frame):
                    if case .string(let text) = frame, let data = text.data(using: .utf8) {
                        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
                        if object?["type"] as? String == "error" {
                            self.message = object?["message"] as? String ?? "The computer reported an error"
                        } else if let status = try? JSONDecoder().decode(ArmStatus.self, from: data) {
                            if !self.connected {
                                self.connected = true
                                self.message = nil
                            }
                            self.status = status
                            if status.note != self.lastNote, status.note != nil, self.moving {
                                self.haptic.notificationOccurred(.warning)
                            }
                            self.lastNote = status.note
                        }
                    }
                    self.receive(task)
                }
            }
        }
    }

    private func send(_ object: [String: Any]) {
        guard let task, let data = try? JSONSerialization.data(withJSONObject: object), let text = String(data: data, encoding: .utf8) else { return }
        task.send(.string(text)) { _ in }
    }

    func start() { send(["type": "start", "tilt": tilt, "view": view]) }
    func setView(_ value: String) { view = value; UserDefaults.standard.set(value, forKey: "view"); send(["type": "view", "view": value]) }
    func align() { send(["type": "align"]) }
    func selectArm(_ name: String) { moving = false; send(["type": "arm", "arm": name]) }
    func stop() { moving = false; send(["type": "stop"]) }
    func home() { moving = false; send(["type": "home"]) }
    func setTilt(_ on: Bool) { tilt = on; send(["type": "tilt", "on": on]) }

    func sendPose(position: SIMD3<Float>, rotation: simd_quatf) {
        let now = Date()
        guard connected, now.timeIntervalSince(lastSent) >= 1.0 / 60.0 else { return }
        lastSent = now
        send([
            "type": "pose",
            "p": [position.x, position.y, position.z],
            "q": [rotation.real, rotation.imag.x, rotation.imag.y, rotation.imag.z],
            "move": moving,
            "grip": grip,
        ])
    }
}

enum Keychain {
    static func save(_ key: String, _ value: String) {
        delete(key)
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: key, kSecValueData as String: Data(value.utf8), kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        SecItemAdd(query as CFDictionary, nil)
    }

    static func read(_ key: String) -> String? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: key, kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func delete(_ key: String) {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: key]
        SecItemDelete(query as CFDictionary)
    }
}
