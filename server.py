import Foundation
import AVFoundation
import Combine

@MainActor
final class DispatchConversationManager: NSObject, ObservableObject {

    // MARK: - UI state
    @Published var isRecording = false
    @Published var lastTranscript: String = ""
    @Published var lastReply: String = ""
    @Published var status: String = "Ready"

    // MARK: - Server-side TTS knobs
    @Published var useServerTTS: Bool = true
    @Published var selectedVoice: String = "nova"   // server default; change anytime

    // Delay server audio playback to avoid VoiceOver overlap
    @Published var replyPlaybackDelaySeconds: Double = 3.0

    // MARK: - Audio
    private var recorder: AVAudioRecorder?
    private let audioSession = AVAudioSession.sharedInstance()

    private var audioPlayer: AVAudioPlayer?
    private var lastServerAudioData: Data? = nil
    private var lastServerAudioMime: String? = nil

    // MARK: - Pilot controls (local knobs)
    // These drive the actual PathLight audio mix (beeps + any local speech that uses the mixer).
    // If you want, we can also persist this to AppStorage later.
    @Published var pilotMasterVolume: Float = 0.5 {
        didSet { pilotMasterVolume = max(0.0, min(1.0, pilotMasterVolume)) }
    }

    // Point this at YOUR backend (Render)
    var dispatchEndpoint: URL =
        URL(string: "https://pathlight-dispatch-v1.onrender.com/dispatch")!

    // MARK: - Recording

    func startRecording() {
        do {
            status = "Starting mic…"

            try audioSession.setCategory(
                .playAndRecord,
                mode: .spokenAudio,
                options: [.defaultToSpeaker, .allowBluetooth]
            )
            try audioSession.setActive(true)

            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("dispatch-\(UUID().uuidString).m4a")

            let settings: [String: Any] = [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 44100,
                AVNumberOfChannelsKey: 1,
                AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue
            ]

            recorder = try AVAudioRecorder(url: url, settings: settings)
            recorder?.prepareToRecord()
            recorder?.record()

            isRecording = true
            status = "Recording…"

        } catch {
            status = "Mic error: \(error.localizedDescription)"
        }
    }

    func stopRecordingAndSend() async {
        guard let recorder else { return }

        recorder.stop()
        isRecording = false
        status = "Uploading…"

        let audioURL = recorder.url
        self.recorder = nil

        do {
            let result = try await sendToBackend(audioURL: audioURL)
            lastTranscript = result.transcript
            lastReply = result.reply
            status = "Done"

            // Execute pilot action after we have a reply (safe ordering)
            if let action = result.action {
                await applyPilotAction(action, replyText: result.reply)
            }

            // Play server audio (delayed to prevent VoiceOver overlap)
            if let audioData = result.audioData {
                lastServerAudioData = audioData
                lastServerAudioMime = result.audioMime
                await playServerAudioDelayed(audioData, delaySeconds: replyPlaybackDelaySeconds)
            } else {
                print("ℹ️ No server audio returned (tts disabled or server text-only).")
            }

        } catch {
            status = "Dispatch error: \(error.localizedDescription)"
        }
    }

    // MARK: - Pilot Action Model

    struct PilotAction: Decodable {
        let name: String
        let args: [String: AnyDecodable]?

        func argString(_ key: String) -> String? { args?[key]?.stringValue }
        func argBool(_ key: String) -> Bool? { args?[key]?.boolValue }
        func argDouble(_ key: String) -> Double? { args?[key]?.doubleValue }
        func argFloat(_ key: String) -> Float? {
            if let d = args?[key]?.doubleValue { return Float(d) }
            return nil
        }
    }

    // Tiny helper to decode "args" as heterogenous JSON
    struct AnyDecodable: Decodable {
        let value: Any

        var stringValue: String? { value as? String }
        var boolValue: Bool? { value as? Bool }
        var doubleValue: Double? {
            if let d = value as? Double { return d }
            if let i = value as? Int { return Double(i) }
            return nil
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if let b = try? c.decode(Bool.self) { value = b; return }
            if let i = try? c.decode(Int.self) { value = i; return }
            if let d = try? c.decode(Double.self) { value = d; return }
            if let s = try? c.decode(String.self) { value = s; return }
            if let dict = try? c.decode([String: AnyDecodable].self) { value = dict; return }
            if let arr = try? c.decode([AnyDecodable].self) { value = arr; return }
            value = NSNull()
        }
    }

    // MARK: - Apply pilot controls

    private func applyPilotAction(_ action: PilotAction, replyText: String) async {
        print("🧭 PilotAction name=\(action.name)")

        switch action.name {

        case "repeat_last":
            // Prefer repeating the last server audio for maximum "ChatGPT-like" feel.
            if let data = lastServerAudioData {
                await playServerAudioDelayed(data, delaySeconds: 0.2)
            } else {
                // If no audio cached, just re-hit the delay (text is already on-screen)
                print("ℹ️ repeat_last: no cached server audio")
            }

        case "help":
            // No local change needed; server reply will explain commands.
            break

        case "set_tts":
            if let enabled = action.argBool("enabled") {
                useServerTTS = enabled
                print("🔧 set_tts enabled=\(enabled)")
            }

        case "set_voice":
            if let v = action.argString("voice"), !v.isEmpty {
                selectedVoice = v
                print("🎙 set_voice \(v)")
            }

        case "adjust_volume":
            let delta = action.argFloat("delta") ?? 0.0
            setMasterVolume(pilotMasterVolume + delta)

        case "set_volume":
            if let v = action.argFloat("value") {
                setMasterVolume(v)
            }

        case "save_feedback":
            let note = action.argString("note") ?? replyText
            saveFeedbackNote(note)

        default:
            print("ℹ️ Unknown action: \(action.name)")
        }
    }

    private func setMasterVolume(_ v: Float) {
        let clamped = max(0.0, min(1.0, v))
        pilotMasterVolume = clamped

        // Drive your shared audio engine if you want volume to affect PathLight beeps too:
        AudioCueEngine.shared.masterVolume = clamped

        print("🔊 pilotMasterVolume=\(clamped)")
    }

    private func saveFeedbackNote(_ note: String) {
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        do {
            let dir = try ensureFeedbackDir()
            let file = dir.appendingPathComponent("feedback.jsonl")

            let payload: [String: Any] = [
                "ts": ISO8601DateFormatter().string(from: Date()),
                "note": trimmed
            ]
            let data = try JSONSerialization.data(withJSONObject: payload, options: [])
            let line = String(data: data, encoding: .utf8)! + "\n"

            if FileManager.default.fileExists(atPath: file.path) {
                let handle = try FileHandle(forWritingTo: file)
                try handle.seekToEnd()
                try handle.write(contentsOf: line.data(using: .utf8)!)
                try handle.close()
            } else {
                try line.data(using: .utf8)!.write(to: file)
            }

            print("📝 Saved feedback: \(trimmed) -> \(file.lastPathComponent)")
        } catch {
            print("❌ Failed to save feedback: \(error.localizedDescription)")
        }
    }

    private func ensureFeedbackDir() throws -> URL {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        let dir = docs.appendingPathComponent("PathLightFeedback", isDirectory: true)
        if !FileManager.default.fileExists(atPath: dir.path) {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        }
        return dir
    }

    // MARK: - Playback helpers

    private func playServerAudioDelayed(_ data: Data, delaySeconds: Double) async {
        let ns = UInt64(max(0.0, delaySeconds) * 1_000_000_000.0)
        if ns > 0 {
            try? await Task.sleep(nanoseconds: ns)
        }
        playServerAudio(data)
    }

    private func playServerAudio(_ data: Data) {
        do {
            audioPlayer?.stop()
            audioPlayer = try AVAudioPlayer(data: data)
            audioPlayer?.prepareToPlay()
            audioPlayer?.play()
            print("🔊 Playing server TTS audio_bytes=\(data.count)")
        } catch {
            print("❌ Audio playback error: \(error.localizedDescription)")
        }
    }

    // MARK: - Networking

    private struct BackendResponse: Decodable {
        let transcript: String
        let reply: String
        let audio_b64: String?
        let audio_mime: String?
        let action: PilotAction?
    }

    private struct BackendResult {
        let transcript: String
        let reply: String
        let audioData: Data?
        let audioMime: String?
        let action: PilotAction?
    }

    private func sendToBackend(audioURL: URL) async throws -> BackendResult {

        var request = URLRequest(url: dispatchEndpoint)
        request.httpMethod = "POST"
        request.timeoutInterval = 45   // Render can be slow on wake

        let boundary = "Boundary-\(UUID().uuidString)"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")

        print("➡️ Dispatch request: \(request.httpMethod ?? "nil") \(request.url?.absoluteString ?? "nil")")

        var body = Data()

        func addField(name: String, value: String) {
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".data(using: .utf8)!)
            body.append("\(value)\r\n".data(using: .utf8)!)
        }

        func addFile(name: String, filename: String, mime: String, data: Data) {
            body.append("--\(boundary)\r\n".data(using: .utf8)!)
            body.append("Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
            body.append("Content-Type: \(mime)\r\n\r\n".data(using: .utf8)!)
            body.append(data)
            body.append("\r\n".data(using: .utf8)!)
        }

        // Fields
        addField(name: "mode", value: "pathlight_dispatch_v1")
        addField(name: "voice", value: selectedVoice)
        addField(name: "tts", value: useServerTTS ? "1" : "0")

        let audioData = try Data(contentsOf: audioURL)
        addFile(name: "audio", filename: "speech.m4a", mime: "audio/mp4", data: audioData)

        body.append("--\(boundary)--\r\n".data(using: .utf8)!)
        request.httpBody = body

        func doRequest() async throws -> (Data, HTTPURLResponse) {
            let (data, resp) = try await URLSession.shared.data(for: request)
            guard let http = resp as? HTTPURLResponse else {
                throw NSError(
                    domain: "DispatchHTTP",
                    code: 0,
                    userInfo: [NSLocalizedDescriptionKey: "No HTTPURLResponse"]
                )
            }
            return (data, http)
        }

        var (data, http) = try await doRequest()

        if [502, 503, 504].contains(http.statusCode) {
            let bodyText = String(data: data, encoding: .utf8) ?? "(no body)"
            print("⏳ Dispatch gateway \(http.statusCode) (cold start?) body=\(bodyText)")
            status = "Waking server…"
            try await Task.sleep(nanoseconds: 1_500_000_000)
            (data, http) = try await doRequest()
        }

        if http.statusCode == 429 {
            let bodyText = String(data: data, encoding: .utf8) ?? "(no body)"
            print("⚠️ Dispatch HTTP 429 rate_limited body=\(bodyText)")
            throw NSError(
                domain: "DispatchHTTP",
                code: 429,
                userInfo: [NSLocalizedDescriptionKey: "Dispatch is busy. Try again in a moment."]
            )
        }

        if http.statusCode != 200 {
            let bodyText = String(data: data, encoding: .utf8) ?? "(no body)"
            print("❌ Dispatch HTTP \(http.statusCode) URL=\(request.url?.absoluteString ?? "nil") body=\(bodyText)")
            throw NSError(
                domain: "DispatchHTTP",
                code: http.statusCode,
                userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(bodyText)"]
            )
        }

        let decoded = try JSONDecoder().decode(BackendResponse.self, from: data)
        print("✅ Dispatch OK transcript_len=\(decoded.transcript.count) reply_len=\(decoded.reply.count) action=\(decoded.action?.name ?? "none")")

        var audioBytes: Data? = nil
        if let b64 = decoded.audio_b64 {
            audioBytes = Data(base64Encoded: b64)
        }

        return BackendResult(
            transcript: decoded.transcript,
            reply: decoded.reply,
            audioData: audioBytes,
            audioMime: decoded.audio_mime,
            action: decoded.action
        )
    }
}
