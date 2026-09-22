import AppKit
import Foundation
import Vision

guard CommandLine.arguments.count >= 2 else {
    fputs("usage: vision_ocr_408 IMAGE [LANG]\n", stderr)
    exit(2)
}

let path = CommandLine.arguments[1]
let language = CommandLine.arguments.count >= 3 ? CommandLine.arguments[2] : "zh-Hans"
guard let image = NSImage(contentsOfFile: path),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("cannot load image: \(path)\n", stderr)
    exit(1)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = [language, "en-US"]
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
try handler.perform([request])

let observations = (request.results ?? []).sorted {
    let ay = $0.boundingBox.maxY
    let by = $1.boundingBox.maxY
    if abs(ay - by) > 0.008 { return ay > by }
    return $0.boundingBox.minX < $1.boundingBox.minX
}

for observation in observations {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let box = observation.boundingBox
    let escaped = candidate.string
        .replacingOccurrences(of: "\t", with: " ")
        .replacingOccurrences(of: "\n", with: " ")
    print(String(
        format: "%.6f\t%.6f\t%.6f\t%.6f\t%@",
        box.minX,
        box.minY,
        box.width,
        box.height,
        escaped
    ))
}
