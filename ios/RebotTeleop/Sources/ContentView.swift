import SwiftUI

struct StandOption: Identifiable {
    let id: String
    let title: String
}

struct ContentView: View {
    @EnvironmentObject var link: ArmLink
    @EnvironmentObject var tracker: ARTracker
    @Environment(\.scenePhase) private var scenePhase
    @State private var code = ""
    @State private var showSettings = false
    @State private var gripAtTouch: Double = 1.0
    @State private var stripAtTouch: Double = 1.0
    @State private var touching = false
    @State private var stripTouching = false
    @AppStorage("separateGrip") private var separateGrip = true

    private let views = [StandOption(id: "behind", title: "Behind arm"), StandOption(id: "front", title: "Facing arm"), StandOption(id: "left", title: "Arm's left"), StandOption(id: "right", title: "Arm's right")]

    var body: some View {
        ZStack {
            CameraView(tracker: tracker).ignoresSafeArea()
            if link.paired {
                controls
            } else {
                pairing
            }
        }
        .sheet(isPresented: $showSettings) { settings }
        .onAppear { if link.paired { link.connect() } }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { link.resume() } else if phase == .background { link.pause() }
        }
    }

    private var pairing: some View {
        VStack(spacing: 14) {
            Text("reBot Teleop").font(.largeTitle.bold())
            Text("On the arm computer open the control page and press Show pairing code.").multilineTextAlignment(.center)
            TextField("Computer address, for example http://192.168.1.20:8080", text: $link.serverURL)
                .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                .textFieldStyle(.roundedBorder)
            TextField("Pairing code", text: $code)
                .keyboardType(.numberPad).textFieldStyle(.roundedBorder)
            Button("Pair") { Task { await link.pair(code: code) } }
                .buttonStyle(.borderedProminent).controlSize(.large)
            if let message = link.message { Text(message).font(.footnote) }
        }
        .padding(24)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 20))
        .padding()
    }

    private var statusLine: String {
        if !link.connected { return "Not connected" }
        if !link.status.torque { return "Motors off. Power on at the computer." }
        if !link.status.remote { return "Allow phone motion at the computer." }
        if !link.status.owner { return "Hold the phone upright, camera facing the way you face, then touch the pad" }
        return link.status.note ?? (link.moving ? "Moving" : "Ready. Thumb on the pad to move.")
    }

    private var controls: some View {
        VStack(spacing: 10) {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(statusLine).font(.headline)
                    Text(tracker.tracking + (link.status.joints.isEmpty ? "" : " · " + link.status.joints.map { String(format: "%.0f°", $0) }.joined(separator: " ")))
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button { showSettings = true } label: { Image(systemName: "gearshape").font(.title2) }
            }
            .padding(12)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))

            if !link.connected {
                VStack(spacing: 8) {
                    if let message = link.message { Text(message).font(.footnote).multilineTextAlignment(.center) }
                    Button { link.connect() } label: { Label("Reconnect", systemImage: "arrow.clockwise") }
                        .buttonStyle(.borderedProminent)
                }
                .frame(maxWidth: .infinity)
                .padding(12)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
            }

            HStack(spacing: 8) {
                Button(link.status.owner ? "Stop" : "Start") {
                    if link.status.owner { link.stop() } else { link.start() }
                }
                .buttonStyle(.borderedProminent)
                Button("Align") { link.align() }.buttonStyle(.bordered)
                Button("Home") { link.home() }.buttonStyle(.bordered)
                Menu {
                    ForEach(views) { item in
                        Button(item.title) { link.setView(item.id) }
                    }
                } label: {
                    Label(views.first { $0.id == link.view }?.title ?? "View", systemImage: "person.fill.viewfinder")
                }
                .buttonStyle(.bordered)
            }

            if let arms = link.status.arms, arms.count > 1 {
                Picker("Arm", selection: Binding(get: { link.status.arm ?? arms[0] }, set: { link.selectArm($0) })) {
                    ForEach(arms, id: \.self) { name in
                        Text(name.capitalized).tag(name)
                    }
                }
                .pickerStyle(.segmented)
                .padding(6)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
            }

            VStack(spacing: 4) {
                Toggle("Rotation follows phone", isOn: Binding(get: { link.tilt }, set: { link.setTilt($0) }))
                Toggle("Separate gripper strip", isOn: $separateGrip)
            }
            .font(.footnote)
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))

            Spacer(minLength: 0)
            HStack(spacing: 10) {
                if separateGrip { gripperStrip }
                movePad
            }
            .frame(height: 290)
        }
        .padding()
    }

    private var gripperStrip: some View {
        GeometryReader { geometry in
            ZStack(alignment: .bottom) {
                RoundedRectangle(cornerRadius: 22).fill(Color.black.opacity(0.45))
                RoundedRectangle(cornerRadius: 22)
                    .fill(Color.white.opacity(0.75))
                    .frame(height: max(16, geometry.size.height * link.grip))
                VStack {
                    Text("OPEN").font(.caption2.bold())
                    Spacer()
                    Text("\(Int(link.grip * 100))%").font(.caption.monospacedDigit().bold())
                    Spacer()
                    Text("CLOSE").font(.caption2.bold())
                }
                .foregroundStyle(.white)
                .padding(.vertical, 10)
                .blendMode(.difference)
            }
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        if !stripTouching {
                            stripTouching = true
                            stripAtTouch = link.grip
                        }
                        let delta = -Double(value.translation.height) / Double(max(geometry.size.height, 1))
                        link.grip = min(1, max(0, stripAtTouch + delta))
                    }
                    .onEnded { _ in stripTouching = false }
            )
        }
        .frame(width: 78)
    }

    private var movePad: some View {
        GeometryReader { geometry in
            ZStack {
                RoundedRectangle(cornerRadius: 28)
                    .fill(touching ? Color.green.opacity(0.55) : Color.black.opacity(0.45))
                RoundedRectangle(cornerRadius: 28).stroke(Color.green, lineWidth: 3)
                VStack(spacing: 6) {
                    Text(touching ? "MOVING" : "THUMB HERE TO MOVE").font(.title3.bold())
                    Text(separateGrip ? "Gripper strip on the left" : "Slide up to open, down to close").font(.footnote)
                }
                .foregroundStyle(.white)
                .multilineTextAlignment(.center)
            }
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        if !touching {
                            touching = true
                            gripAtTouch = link.grip
                            link.moving = true
                            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
                        }
                        if !separateGrip {
                            let delta = -Double(value.translation.height) / Double(max(geometry.size.height, 1))
                            link.grip = min(1, max(0, gripAtTouch + delta * 1.5))
                        }
                    }
                    .onEnded { _ in
                        touching = false
                        link.moving = false
                        UIImpactFeedbackGenerator(style: .light).impactOccurred()
                    }
            )
        }
    }

    private var settings: some View {
        NavigationStack {
            Form {
                Section("Computer") {
                    TextField("Address", text: $link.serverURL)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                    Text("Use the address shown under Show pairing code, for example http://192.168.1.20:8080 on the same WiFi or the computer's Tailscale address.").font(.caption)
                    Button("Save and reconnect") { UserDefaults.standard.set(link.serverURL, forKey: "serverURL"); link.connect(); showSettings = false }
                }
                Section("How it works") {
                    Text("Hold the phone upright with the camera looking the way you face and pick where you stand. Touching the pad takes control and aligns forward. Keep your thumb on the pad and move the phone: the gripper moves and turns by the same amount. Lift your thumb to stop. The strip on the left opens and closes the gripper without moving the arm. Tap Align any time forward feels off.")
                }
                Section {
                    Button("Forget this computer", role: .destructive) { link.forget(); showSettings = false }
                }
            }
            .navigationTitle("Settings")
            .toolbar { Button("Done") { showSettings = false } }
        }
    }
}
