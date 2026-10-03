import SwiftUI

@main
struct RebotTeleopApp: App {
    @StateObject private var link = ArmLink()
    @StateObject private var tracker = ARTracker()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(link)
                .environmentObject(tracker)
                .onAppear {
                    tracker.onPose = { position, rotation in
                        link.sendPose(position: position, rotation: rotation)
                    }
                }
        }
    }
}
