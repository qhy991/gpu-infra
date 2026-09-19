import Foundation
import Metal

func evaluate() throws -> Bool {
    guard ProcessInfo.processInfo.environment["GPUQ_BACKEND"] == "metal",
          ProcessInfo.processInfo.environment["GPUQ_DEVICE_IDS"] == "0" else {
        throw NSError(domain: "broker allocation required", code: 1)
    }
    let devices = MTLCopyAllDevices()
    guard devices.count == 1, let device = devices.first,
          device.name.hasPrefix("Apple "), let queue = device.makeCommandQueue() else {
        throw NSError(domain: "single Apple Metal device required", code: 1)
    }
    let source = try String(contentsOfFile: CommandLine.arguments[1], encoding: .utf8)
    let library = try device.makeLibrary(source: source, options: nil)
    guard let function = library.makeFunction(name: "vector_double") else {
        throw NSError(domain: "missing vector_double", code: 1)
    }
    let pipeline = try device.makeComputePipelineState(function: function)
    let count = 65536
    let bytes = count * MemoryLayout<Float>.stride
    guard let input = device.makeBuffer(length: bytes, options: .storageModeShared),
          let output = device.makeBuffer(length: bytes, options: .storageModeShared),
          let command = queue.makeCommandBuffer(), let encoder = command.makeComputeCommandEncoder() else {
        throw NSError(domain: "Metal allocation failed", code: 1)
    }
    let x = input.contents().bindMemory(to: Float.self, capacity: count)
    let y = output.contents().bindMemory(to: Float.self, capacity: count)
    for i in 0..<count { x[i] = Float(i % 257) - 128; y[i] = .nan }
    encoder.setComputePipelineState(pipeline)
    encoder.setBuffer(input, offset: 0, index: 0)
    encoder.setBuffer(output, offset: 0, index: 1)
    encoder.dispatchThreads(MTLSize(width: count, height: 1, depth: 1), threadsPerThreadgroup: MTLSize(width: min(256, pipeline.maxTotalThreadsPerThreadgroup), height: 1, depth: 1))
    encoder.endEncoding()
    command.commit()
    command.waitUntilCompleted()
    guard command.status == .completed else {
        throw command.error ?? NSError(domain: "Metal execution failed", code: 1)
    }
    return (0..<count).allSatisfy { y[$0] == x[$0] * 2 }
}
do {
    print(try evaluate() ? "correct" : "incorrect")
} catch {
    fputs("\(error)\n", stderr)
    exit(2)
}
