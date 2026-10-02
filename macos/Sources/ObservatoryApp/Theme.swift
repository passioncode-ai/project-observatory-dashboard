import SwiftUI
import AppKit
import ObservatoryCore

/// The app's one visual layer: PassionCode's roles (`Palette`, the dashboard's own
/// values) as SwiftUI colours, the radii and spacing, and the few atoms every
/// window shares. No view names a colour; `PaletteTests` refuses one that does.
///
/// Workbench register: borders as elevation, one accent (gold) for the primary
/// action, selection and focus; semantic colours carry state only — warning for
/// "a person is needed", negative for a failure, positive for ready, info for
/// running. Motion: colour and border on hover and press, 120 ms, and none at
/// all under Reduce Motion.
extension Color {
    init(_ c: Palette.RGB) { self.init(.sRGB, red: c.red, green: c.green, blue: c.blue, opacity: 1) }
}
extension NSColor {
    convenience init(_ c: Palette.RGB) { self.init(srgbRed: c.red, green: c.green, blue: c.blue, alpha: 1) }
}

enum Theme {
    static let bg = Color(Palette.bg)
    static let panel = Color(Palette.panel)
    static let raised = Color(Palette.panelRaised)
    static let text = Color(Palette.text)
    static let muted = Color(Palette.textMuted)
    static let border = Color(Palette.border)
    static let borderStrong = Color(Palette.borderStrong)
    static let accent = Color(Palette.accent)
    static let accentHover = Color(Palette.accentHover)
    static let onAccent = Color(Palette.onAccent)
    static let accentSoft = Color(Palette.accentSoft)

    static let radiusControl = CGFloat(Palette.radiusControl)
    static let radiusPanel = CGFloat(Palette.radiusPanel)
    static let gap = CGFloat(Palette.space[3])          // 16
    static let transcript: CGFloat = 760                // one reading column

    static let hover = Animation.easeOut(duration: 0.12)
    /// The window background behind AppKit's own chrome (title bar, sheets).
    static let windowBackground = NSColor(Palette.bg)
}

/// A state, in the design system's semantic colours.
enum Tone {
    case positive, warning, negative, info, neutral
    var color: Color {
        switch self {
        case .positive: return Color(Palette.positive)
        case .warning: return Color(Palette.warning)
        case .negative: return Color(Palette.negative)
        case .info: return Color(Palette.info)
        case .neutral: return Theme.muted
        }
    }
    var fill: Color {
        switch self {
        case .positive: return Color(Palette.positiveSoft)
        case .warning: return Color(Palette.warningSoft)
        case .negative: return Color(Palette.negativeSoft)
        case .info: return Color(Palette.infoSoft)
        case .neutral: return Theme.raised
        }
    }
}

/// Primary: the gold fill, at most one per view. Text is `on-accent` (11:1), never white.
struct PrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View { Face(configuration: configuration) }
    private struct Face: View {
        let configuration: Configuration
        @Environment(\.isEnabled) private var enabled
        @Environment(\.accessibilityReduceMotion) private var still
        @State private var hovering = false
        var body: some View {
            configuration.label
                .font(.body.weight(.semibold))
                .foregroundStyle(enabled ? Theme.onAccent : Theme.muted)
                .padding(.horizontal, 14).padding(.vertical, 6)
                .background(RoundedRectangle(cornerRadius: Theme.radiusControl)
                    .fill(!enabled ? Theme.raised : (hovering || configuration.isPressed) ? Theme.accentHover : Theme.accent))
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusControl).strokeBorder(enabled ? Color.clear : Theme.border))
                .contentShape(Rectangle())
                .onHover { hovering = $0 }
                .animation(still ? nil : Theme.hover, value: hovering)
        }
    }
}

/// Secondary: a 1 px ghost; `destructive` draws its edge and text in the negative role.
struct SecondaryButtonStyle: ButtonStyle {
    var destructive = false
    func makeBody(configuration: Configuration) -> some View { Face(configuration: configuration, destructive: destructive) }
    private struct Face: View {
        let configuration: Configuration
        let destructive: Bool
        @Environment(\.isEnabled) private var enabled
        @Environment(\.accessibilityReduceMotion) private var still
        @State private var hovering = false
        var body: some View {
            configuration.label
                .foregroundStyle(!enabled ? Theme.muted : destructive ? Tone.negative.color : Theme.text)
                .padding(.horizontal, 12).padding(.vertical, 5)
                .background(RoundedRectangle(cornerRadius: Theme.radiusControl)
                    .fill((hovering || configuration.isPressed) && enabled ? Theme.raised : Theme.panel))
                .overlay(RoundedRectangle(cornerRadius: Theme.radiusControl)
                    .strokeBorder(!enabled ? Theme.border : destructive ? Tone.negative.color : Theme.borderStrong))
                .opacity(enabled ? 1 : 0.55)
                .contentShape(Rectangle())
                .onHover { hovering = $0 }
                .animation(still ? nil : Theme.hover, value: hovering)
        }
    }
}

/// A text field on the panel: the design system's control edge at rest, the 2 px
/// accent ring when focused (the system ring would be the user's own accent colour).
struct FieldChrome: ViewModifier {
    var focused: Bool
    func body(content: Content) -> some View {
        content
            .textFieldStyle(.plain)
            .foregroundStyle(Theme.text)
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: Theme.radiusControl).fill(Theme.panel))
            .overlay(RoundedRectangle(cornerRadius: Theme.radiusControl)
                .strokeBorder(focused ? Theme.accent : Theme.borderStrong, lineWidth: focused ? 2 : 1))
    }
}
extension View {
    func fieldChrome(focused: Bool) -> some View { modifier(FieldChrome(focused: focused)) }
    /// One window of the product: dark, gold-tinted, on the PassionCode ground.
    func passionCodeWindow() -> some View {
        self.preferredColorScheme(.dark).tint(Theme.accent).foregroundStyle(Theme.text)
            .background(Theme.bg)
            .toolbarBackground(Theme.panel, for: .windowToolbar)
            .toolbarBackground(.visible, for: .windowToolbar)
    }
}

/// A state banner across the top of a pane: icon and text in the tone, actions on the right.
struct Banner<Actions: View>: View {
    let tone: Tone
    let symbol: String
    let title: String
    var detail: String? = nil
    @ViewBuilder var actions: () -> Actions
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol).foregroundStyle(tone.color).accessibilityHidden(true).padding(.top, 2)
            VStack(alignment: .leading, spacing: 2) {
                Text(rendered(title)).font(.callout.weight(.semibold)).foregroundStyle(Theme.text).textSelection(.enabled)
                if let detail { Text(rendered(detail)).font(.caption).foregroundStyle(Theme.muted).textSelection(.enabled) }
            }
            Spacer(minLength: 8)
            actions()
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(tone.fill)
        .overlay(alignment: .bottom) { Rectangle().fill(Theme.border).frame(height: 1) }
        .accessibilityElement(children: .contain)
    }
}

/// A two-way segmented choice, the selected side in gold (no system blue).
struct Segmented: View {
    let options: [(id: String, label: String)]
    @Binding var selection: String
    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(options.enumerated()), id: \.offset) { i, o in
                let on = o.id == selection
                Button { selection = o.id } label: {
                    Text(o.label).font(.callout.weight(on ? .semibold : .regular))
                        .foregroundStyle(on ? Theme.onAccent : Theme.text)
                        .padding(.horizontal, 14).padding(.vertical, 5)
                        .background(on ? Theme.accent : Theme.panel)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(on ? .isSelected : [])
                if i < options.count - 1 { Rectangle().fill(Theme.borderStrong).frame(width: 1) }
            }
        }
        .fixedSize()
        .clipShape(RoundedRectangle(cornerRadius: Theme.radiusControl))
        .overlay(RoundedRectangle(cornerRadius: Theme.radiusControl).strokeBorder(Theme.borderStrong))
    }
}
