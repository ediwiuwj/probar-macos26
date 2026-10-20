import Foundation
import Metal

guard let device = MTLCreateSystemDefaultDevice() else {
    fatalError("No hay dispositivo Metal disponible")
}

print("== Metal / GPU ==")
print("Dispositivo:      \(device.name)")
print("Unified memory:   \(device.hasUnifiedMemory)")
print("Low power:        \(device.isLowPower)")
print("Headless:         \(device.isHeadless)")
print("Removable:        \(device.isRemovable)")
print("Registry ID:      \(device.registryID)")
print("maxThreadgroupMemoryLength: \(device.maxThreadgroupMemoryLength)")
print("recommendedMaxWorkingSetSize: \(device.recommendedMaxWorkingSetSize)")
print("supportsRaytracing:        \(device.supportsRaytracing)")
print("supportsFunctionPointers:  \(device.supportsFunctionPointers)")
print("supportsFamily(.common3):  \(device.supportsFamily(.common3))")
print("supportsFamily(.apple9):   \(device.supportsFamily(.apple9))")
print("supportsFamily(.apple8):   \(device.supportsFamily(.apple8))")
