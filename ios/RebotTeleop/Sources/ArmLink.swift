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
    private var lastSent = Date.distantPast
    private var lastNote: String?
    private let haptic = UINotificationFeedbackGenerator()

    func pair(code: String) async {
        guard let url = URL(string: serverURL.trimmingCharacters(in: .whitespaces) + "/api/session") else {
            message = "Check the computer address"
            return
        }
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
        Keychain.delete("session")
        paired = false
    }

    func connect() {
        guard let session = Keychain.read("session") else { return }
        var text = serverURL.trimmingCharacters(in: .whitespaces)
        text = text.replacingOccurrences(of: "https://", with: "wss://").replacingOccurrences(of: "http://", with: "ws://")
        guard let url = URL(string: text + "/ws/app") else {
            message = "Check the computer address"
            return
        }
        disconnect()
        var request = URLRequest(url: url)
        request.setValue("Bearer " + session, forHTTPHeaderField: "Authorization")
        let task = URLSession.shared.webSocketTask(with: request)
        self.task = task
        task.resume()
        connected = true
        receive(task)
    }

    func disconnect() {
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        connected = false
        moving = false
    }

    private func receive(_ task: URLSessionWebSocketTask) {
        task.receive { [weak self] result in
            Task { @MainActor in
                guard let self, self.task === task else { return }
                switch result {
                case .failure(let error):
                    self.connected = false
                    self.moving = false
                    self.message = "Disconnected: \(error.localizedDescription)"
                case .success(let frame):
                    if case .string(let text) = frame, let data = text.data(using: .utf8), let status = try? JSONDecoder().decode(ArmStatus.self, from: data) {
                        self.status = status
                        if status.note != self.lastNote, status.note != nil, self.moving {
                            self.haptic.notificationOccurred(.warning)
                        }
                        self.lastNote = status.note
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
