import ARKit
import SceneKit
import SwiftUI

final class ARTracker: NSObject, ObservableObject, ARSessionDelegate {
    let session = ARSession()
    @MainActor var onPose: (@MainActor (SIMD3<Float>, simd_quatf) -> Void)?
    @Published var tracking = "Starting"

    override init() {
        super.init()
        session.delegate = self
    }

    func run() {
        let configuration = ARWorldTrackingConfiguration()
        configuration.worldAlignment = .gravity
        session.run(configuration, options: [.resetTracking])
    }

    func pause() {
        session.pause()
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let camera = frame.camera
        let transform = camera.transform
        let state: String
        switch camera.trackingState {
        case .normal:
            state = "Tracking"
        case .notAvailable:
            state = "Tracking unavailable"
        case .limited(let reason):
            switch reason {
            case .excessiveMotion: state = "Move slower"
            case .insufficientFeatures: state = "Point the camera at something with detail"
            case .initializing, .relocalizing: state = "Hold steady, starting tracking"
            @unknown default: state = "Tracking limited"
            }
        @unknown default:
            state = "Tracking limited"
        }
        let position = SIMD3<Float>(transform.columns.3.x, transform.columns.3.y, transform.columns.3.z)
        let rotation = simd_quatf(transform)
        let normal: Bool
        if case .normal = camera.trackingState { normal = true } else { normal = false }
        Task { @MainActor in
            if self.tracking != state { self.tracking = state }
            if normal { self.onPose?(position, rotation) }
        }
    }
}

struct CameraView: UIViewRepresentable {
    let tracker: ARTracker

    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView()
        view.session = tracker.session
        view.automaticallyUpdatesLighting = false
        tracker.run()
        return view
    }

    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}
