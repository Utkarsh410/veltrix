"""Veltrix Data app icon generator.

Writes the vector master (veltrix-data.svg), the 1024 px PNG, the macOS iconset and the
Windows .ico into the folder given as the first argument (default: this folder). Then:
    iconutil -c icns veltrix-data.iconset -o veltrix-data.icns
Colours are the Veltrix brand tokens (carbon #0B0B0D, white, lavender #B9A7E8).
The mark is a speed trace (straight, braking, apex, acceleration) over a throttle trace."""
import math
import os
import sys
from PIL import Image, ImageDraw, ImageFilter

CARBON = (11, 11, 13); CARBON_TOP = (26, 26, 31); WHITE = (255, 255, 255); LAV = (185, 167, 232)

# Speed trace (body-unit coords, y down): straight, braking to the apex, then acceleration back to speed.
X0, XB, XA, X1 = 0.12, 0.36, 0.47, 0.88
Y_TOP, Y_APEX, Y_END = 0.29, 0.64, 0.31
APEX = (XA, Y_APEX)

def speed_trace(n=400):
    pts = []
    for i in range(n + 1):
        x = X0 + (X1 - X0) * i / n
        if x <= XB:
            y = 0.33 + (Y_TOP - 0.33) * (x - X0) / (XB - X0)
        elif x <= XA:
            y = Y_TOP + (Y_APEX - Y_TOP) * ((x - XB) / (XA - XB)) ** 1.3
        else:
            k = 2.8
            f = (1 - math.exp(-k * (x - XA) / (X1 - XA))) / (1 - math.exp(-k))
            y = Y_APEX + (Y_END - Y_APEX) * f
        pts.append((x, y))
    return pts

def throttle_trace():
    hi, lo = 0.775, 0.855
    return [(X0, hi), (XB - 0.005, hi), (XB + 0.012, lo), (XA, lo), (XA + 0.10, hi), (X1, hi)]

def densify(pts, n=60):
    out = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        out += [(ax + (bx - ax) * k / n, ay + (by - ay) * k / n) for k in range(n)]
    return out + [pts[-1]]

def stroke(draw, pts, width, fill):
    """Round-capped, round-joined stroke drawn as overlapping discs — smooth at any curvature."""
    r = width / 2
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        steps = max(1, int(math.hypot(bx - ax, by - ay) / (r * 0.25)))
        for k in range(steps + 1):
            x = ax + (bx - ax) * k / steps; y = ay + (by - ay) * k / steps
            draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)

def render(size, margin, radius_frac, detail=True, shadow=True):
    S = 4  # supersample
    W = size * S
    img = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    m = round(W * margin); body = W - 2 * m; r = round(body * radius_frac)
    if shadow:
        sh = Image.new("RGBA", (W, W), (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([m, m + body*0.015, m + body, m + body*1.015], r, fill=(0, 0, 0, 110))
        img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(body * 0.022)))
    # Tile: carbon with a very gentle top lift (no heavy gradient).
    tile = Image.new("RGBA", (W, W), (0, 0, 0, 0)); td = ImageDraw.Draw(tile)
    for yy in range(body):
        f = yy / body
        c = tuple(round(CARBON_TOP[i] * (1 - f) + CARBON[i] * f) for i in range(3))
        td.line([(m, m + yy), (m + body, m + yy)], fill=c + (255,))
    mask = Image.new("L", (W, W), 0); ImageDraw.Draw(mask).rounded_rectangle([m, m, m + body, m + body], r, fill=255)
    layer = Image.new("RGBA", (W, W), (0, 0, 0, 0)); ld = ImageDraw.Draw(layer)
    U = lambda x, y: (m + x * body, m + y * body)
    if detail:
        # Engineering grid.
        for i in range(1, 6):
            g = i / 6
            ld.line([U(0, g), U(1, g)], fill=(255, 255, 255, 16), width=max(1, round(body * 0.004)))
            ld.line([U(g, 0), U(g, 1)], fill=(255, 255, 255, 16), width=max(1, round(body * 0.004)))
        # Apex marker line.
        ld.line([U(APEX[0], 0.16), U(APEX[0], 0.88)], fill=LAV + (80,), width=max(1, round(body * 0.006)))
    if detail:
        stroke(ld, [U(*p) for p in densify(throttle_trace())], body * 0.02, (255, 255, 255, 105))
    stroke(ld, [U(*p) for p in speed_trace()], body * (0.052 if detail else 0.085), WHITE + (255,))
    # Apex dot (lavender, ringed in carbon so it separates from the trace).
    ax, ay = U(*APEX); R = body * (0.058 if detail else 0.085)
    ld.ellipse([ax - R*1.35, ay - R*1.35, ax + R*1.35, ay + R*1.35], fill=CARBON + (255,))
    ld.ellipse([ax - R, ay - R, ax + R, ay + R], fill=LAV + (255,))
    if detail:
        # Corner ticks — the website's frame motif.
        t = body * 0.11; th = max(2, round(body * 0.014)); inset = body * 0.085
        x0, y0 = m + inset, m + inset; x1, y1 = m + body - inset, m + body - inset
        ld.line([(x0, y0 + t), (x0, y0), (x0 + t, y0)], fill=LAV + (230,), width=th, joint="curve")
        ld.line([(x1 - t, y1), (x1, y1), (x1, y1 - t)], fill=LAV + (230,), width=th, joint="curve")
    tile = Image.alpha_composite(tile, layer)
    # Hairline edge for definition on dark docks/taskbars.
    edge = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle([m, m, m + body, m + body], r, outline=(255, 255, 255, 34), width=max(1, round(body * 0.004)))
    tile = Image.alpha_composite(tile, edge)
    img.paste(tile, (0, 0), Image.composite(tile, Image.new("RGBA", (W, W)), mask).getchannel("A"))
    return img.resize((size, size), Image.LANCZOS)

def svg():
    """Vector master with the same geometry (macOS grid: 824px body on a 1024 canvas)."""
    m, body, r = 100, 824, 185
    U = lambda x, y: (m + x * body, m + y * body)
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in (U(*p) for p in speed_trace(160)))
    th = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in (U(*p) for p in throttle_trace()))
    grid = "".join(f'<path d="M{m},{m+body*i/6:.1f}H{m+body} M{m+body*i/6:.1f},{m}V{m+body}"/>' for i in range(1, 6))
    ax, ay = U(*APEX); R = body * 0.058; t = body * 0.11; inset = body * 0.085
    x0 = y0 = m + inset; x1 = y1 = m + body - inset
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  <title>Veltrix Data</title>
  <defs>
    <linearGradient id="tile" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1A1A1F"/><stop offset="1" stop-color="#0B0B0D"/></linearGradient>
    <clipPath id="clip"><rect x="{m}" y="{m}" width="{body}" height="{body}" rx="{r}"/></clipPath>
  </defs>
  <rect x="{m}" y="{m}" width="{body}" height="{body}" rx="{r}" fill="url(#tile)"/>
  <g clip-path="url(#clip)">
    <g stroke="#FFFFFF" stroke-opacity="0.063" stroke-width="3.3" fill="none">{grid}</g>
    <path d="M{ax:.1f},{U(0,0.16)[1]:.1f}V{U(0,0.88)[1]:.1f}" stroke="#B9A7E8" stroke-opacity="0.31" stroke-width="5"/>
    <path d="{th}" fill="none" stroke="#FFFFFF" stroke-opacity="0.41" stroke-width="{body*0.02:.1f}" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="{d}" fill="none" stroke="#FFFFFF" stroke-width="{body*0.052:.1f}" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="{ax:.1f}" cy="{ay:.1f}" r="{R*1.35:.1f}" fill="#0B0B0D"/>
    <circle cx="{ax:.1f}" cy="{ay:.1f}" r="{R:.1f}" fill="#B9A7E8"/>
    <path d="M{x0:.1f},{y0+t:.1f}V{y0:.1f}H{x0+t:.1f} M{x1-t:.1f},{y1:.1f}H{x1:.1f}V{y1-t:.1f}" fill="none" stroke="#B9A7E8" stroke-opacity="0.9" stroke-width="{body*0.014:.1f}"/>
  </g>
  <rect x="{m}" y="{m}" width="{body}" height="{body}" rx="{r}" fill="none" stroke="#FFFFFF" stroke-opacity="0.13" stroke-width="3.3"/>
</svg>
'''

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    os.makedirs(out, exist_ok=True)
    open(f"{out}/veltrix-data.svg", "w").write(svg())
    # macOS: Apple grid (824/1024 body, ~22.5% continuous-corner radius), soft shadow.
    mac = {s: render(s, 100/1024, 0.225, detail=s >= 64, shadow=s >= 64) for s in (16, 32, 64, 128, 256, 512, 1024)}
    mac[1024].save(f"{out}/veltrix-data-1024.png")
    iconset = f"{out}/veltrix-data.iconset"; os.makedirs(iconset, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        mac[base].save(f"{iconset}/icon_{base}x{base}.png")
        mac[base * 2].save(f"{iconset}/icon_{base}x{base}@2x.png")
    # Windows: near full-bleed tile, no shadow; simplified mark at small sizes.
    win = [render(s, 0.03, 0.18, detail=s >= 48, shadow=False) for s in (16, 24, 32, 48, 64, 128, 256)]
    win[-1].save(f"{out}/veltrix-data.ico", sizes=[(i.width, i.height) for i in win], append_images=win[:-1])
