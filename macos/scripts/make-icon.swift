// make-icon.swift <mark.svg> <out.iconset>
//
// The app icon IS the product mark (observatory/engine/dashboard/brand/observatory-mark.svg,
// pinned with its SHA-256 in that folder's manifest.json) — rasterized, never redrawn, so
// the Dock, the dashboard's rail and its favicon show one glyph. This is vector
// rasterization of a reviewed asset, not generated imagery.
//
// Geometry follows the macOS icon grid: on a 1024 canvas the tile is 824 × 824, centred,
// with a soft shadow in the margin, so the icon sits at the same visual size as the
// system's own. Every size is drawn from the vector at its own resolution.
import AppKit

let args = CommandLine.arguments
guard args.count == 3, let mark = NSImage(contentsOfFile: args[1]) else {
    FileHandle.standardError.write("usage: make-icon.swift <mark.svg> <out.iconset>\n".data(using: .utf8)!); exit(2)
}
let out = URL(fileURLWithPath: args[2])
try? FileManager.default.removeItem(at: out)
try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

/// The SVG draws its tile at 8…248 of a 256 box; the tile, not the box, gets 824/1024.
let tileInBox = 240.0 / 256.0
func render(_ px: Int) -> Data {
    let s = CGFloat(px)
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0),
          let ctx = NSGraphicsContext(bitmapImageRep: rep) else { fatalError("bitmap \(px)") }
    rep.size = NSSize(width: px, height: px)
    NSGraphicsContext.saveGraphicsState(); NSGraphicsContext.current = ctx
    ctx.imageInterpolation = .high
    NSColor.clear.set(); NSRect(x: 0, y: 0, width: s, height: s).fill()
    let tile = s * 824 / 1024
    let box = tile / tileInBox
    let rect = NSRect(x: (s - box) / 2, y: (s - box) / 2 + s * 0.006, width: box, height: box)
    // The shadow belongs to the large sizes; at 16–32 px it only muddies the edge.
    if px >= 64 {
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.32)
        shadow.shadowOffset = NSSize(width: 0, height: -s * 0.010)
        shadow.shadowBlurRadius = s * 0.022
        shadow.set()
    }
    mark.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    guard let png = rep.representation(using: .png, properties: [:]) else { fatalError("png \(px)") }
    return png
}

// The iconset names iconutil requires: each point size at 1x and 2x.
for pt in [16, 32, 128, 256, 512] {
    try render(pt).write(to: out.appendingPathComponent("icon_\(pt)x\(pt).png"))
    try render(pt * 2).write(to: out.appendingPathComponent("icon_\(pt)x\(pt)@2x.png"))
}
print(out.path)
