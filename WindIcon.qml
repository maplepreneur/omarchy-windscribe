import QtQuick
import QtQuick.Shapes
import qs.Commons
import qs.Commons as Commons

// The Windscribe "W" mark, drawn natively from Windscribe's own public
// logo (windscribe.com/images/windscribe-logo-full.svg) so it takes the
// theme foreground instead of the brand's white-on-dark, the same reason
// the OmaProton widget redraws Proton's mark rather than scaling a bitmap.
//
// Connected/disconnected is carried by the caller's color and opacity,
// which is how the first-party widgets (dropbox, tailscale) signal state.
Item {
  id: root

  property real iconSize: Style.font.icon
  property color color: Commons.Color.foreground

  readonly property real viewBoxW: 16.49
  readonly property real viewBoxH: 20

  width: iconSize
  height: iconSize
  implicitWidth: iconSize
  implicitHeight: iconSize

  Item {
    anchors.centerIn: parent
    width: root.viewBoxW
    height: root.viewBoxH
    // Scaled down from the source viewBox to the bar's icon size, so the
    // shape geometry stays crisp rather than being rasterized then stretched.
    scale: Math.min(root.width / root.viewBoxW, root.height / root.viewBoxH)

    Shape {
      anchors.fill: parent
      antialiasing: true
      preferredRendererType: Shape.CurveRenderer

      ShapePath {
        fillColor: root.color
        strokeWidth: 0
        strokeColor: "transparent"
        fillRule: ShapePath.WindingFill

        PathSvg {
          // The W of the wordmark, lifted verbatim: two outer stems, the
          // zigzag valley between them reads as the wind it's named for.
          path: "M3.32021 16.0805L8.24604 10.5545L13.1719 16.081V5.16809H16.4916V19.8788H12.597L12.5249 19.7973L8.24556 14.9536L3.96673 19.7973L3.8946 19.8788H0V5.16809H3.32021V16.0805Z"
        }
      }
    }
  }
}
