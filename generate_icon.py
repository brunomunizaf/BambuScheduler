#!/usr/bin/env python3
"""Generate a modern app icon for BambuMenu - 3D printing scheduler."""

from PIL import Image, ImageDraw
import math
import subprocess
import os

SIZES = [16, 32, 64, 128, 256, 512, 1024]


def draw_icon(size):
    """Draw a stylized isometric 3D cube with printing layers + clock badge."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = size * 0.08
    inner = size - 2 * pad

    # Background rounded rect - Bambu teal
    bg_top = (0, 166, 147)      # Lighter teal
    bg_bottom = (0, 115, 100)   # Darker teal
    radius = size * 0.22

    # Draw gradient background
    x0, y0 = pad, pad
    x1, y1 = size - pad, size - pad

    # Base fill
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=bg_top)

    # Gradient overlay from top to bottom
    for y in range(int(y0), int(y1)):
        t = (y - y0) / (y1 - y0)
        r = int(bg_top[0] + t * (bg_bottom[0] - bg_top[0]))
        g = int(bg_top[1] + t * (bg_bottom[1] - bg_top[1]))
        b = int(bg_top[2] + t * (bg_bottom[2] - bg_top[2]))
        # Only draw within the rounded rect bounds
        # Simple approach: draw full line, mask will handle it
        draw.line([(x0, y), (x1, y)], fill=(r, g, b, 255))

    # Re-create the rounded rect mask
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=255)
    img.putalpha(mask)

    # --- Isometric 3D cube ---
    cx = size * 0.46
    cy = size * 0.48
    edge = inner * 0.32  # cube edge length

    # Isometric projection: 30 degrees
    cos30 = math.cos(math.radians(30))
    sin30 = math.sin(math.radians(30))

    # Define the 6 key points of visible isometric cube
    # Top vertex
    top = (cx, cy - edge)
    # Bottom vertex
    bottom = (cx, cy + edge)
    # Left vertex
    left = (cx - edge * cos30, cy + edge * sin30)
    # Right vertex
    right = (cx + edge * cos30, cy + edge * sin30)
    # Back-left vertex
    back_left = (cx - edge * cos30, cy - edge * sin30)
    # Back-right vertex
    back_right = (cx + edge * cos30, cy - edge * sin30)
    # Hidden back vertex
    back_top = (cx, cy - edge)  # same as top for this projection

    # Three visible faces
    # Top face
    top_center = (cx, cy - edge + edge * sin30 - edge * sin30)
    top_face = [
        top,
        back_left,
        (cx, cy),  # center
        back_right,
    ]

    # Left face
    left_face = [
        (cx, cy),  # center
        back_left,
        left,
        bottom,
    ]

    # Right face
    right_face = [
        (cx, cy),  # center
        back_right,
        right,
        bottom,
    ]

    # Top face - brightest white
    draw.polygon(top_face, fill=(255, 255, 255, 220))
    # Left face - medium
    draw.polygon(left_face, fill=(255, 255, 255, 150))
    # Right face - darkest
    draw.polygon(right_face, fill=(255, 255, 255, 90))

    # Layer lines on left face (horizontal stripes = 3D printing layers)
    num_layers = 7
    line_color = (0, 130, 115, 160)
    lw = max(1, int(size * 0.006))

    for i in range(1, num_layers):
        t = i / num_layers
        # Left edge: from center (cx, cy) to bottom (cx, cy+edge)
        ly1 = cy + t * edge
        lx1 = cx
        # Right edge: from back_left to left
        ly2 = back_left[1] + t * (left[1] - back_left[1])
        lx2 = back_left[0] + t * (left[0] - back_left[0])
        draw.line([(lx1, ly1), (lx2, ly2)], fill=line_color, width=lw)

    # Layer lines on right face
    for i in range(1, num_layers):
        t = i / num_layers
        ly1 = cy + t * edge
        lx1 = cx
        ly2 = back_right[1] + t * (right[1] - back_right[1])
        lx2 = back_right[0] + t * (right[0] - back_right[0])
        draw.line([(lx1, ly1), (lx2, ly2)], fill=line_color, width=lw)

    # Cube edges
    edge_color = (255, 255, 255, 255)
    ew = max(1, int(size * 0.012))

    # Top face edges
    draw.line([top, back_left], fill=edge_color, width=ew)
    draw.line([top, back_right], fill=edge_color, width=ew)
    draw.line([back_left, (cx, cy)], fill=edge_color, width=ew)
    draw.line([back_right, (cx, cy)], fill=edge_color, width=ew)

    # Left face edges
    draw.line([back_left, left], fill=edge_color, width=ew)
    draw.line([left, bottom], fill=edge_color, width=ew)

    # Right face edges
    draw.line([back_right, right], fill=edge_color, width=ew)
    draw.line([right, bottom], fill=edge_color, width=ew)

    # Center vertical edge
    draw.line([(cx, cy), bottom], fill=edge_color, width=ew)

    # --- Clock badge in bottom-right ---
    clock_cx = size * 0.76
    clock_cy = size * 0.76
    clock_r = size * 0.13

    # White circle with teal border
    # Shadow
    draw.ellipse(
        [clock_cx - clock_r + 2, clock_cy - clock_r + 2,
         clock_cx + clock_r + 2, clock_cy + clock_r + 2],
        fill=(0, 0, 0, 40),
    )
    draw.ellipse(
        [clock_cx - clock_r, clock_cy - clock_r,
         clock_cx + clock_r, clock_cy + clock_r],
        fill=(255, 255, 255, 245),
        outline=(0, 100, 88, 255),
        width=max(1, int(size * 0.012)),
    )

    # Clock hands
    hand_color = (0, 100, 88, 255)
    hw = max(1, int(size * 0.014))

    # Hour hand (10 o'clock)
    ha = math.radians(-60)
    hx = clock_cx + clock_r * 0.45 * math.sin(ha)
    hy = clock_cy - clock_r * 0.45 * math.cos(ha)
    draw.line([(clock_cx, clock_cy), (hx, hy)], fill=hand_color, width=hw)

    # Minute hand (2 o'clock)
    ma = math.radians(60)
    mx = clock_cx + clock_r * 0.65 * math.sin(ma)
    my = clock_cy - clock_r * 0.65 * math.cos(ma)
    draw.line([(clock_cx, clock_cy), (mx, my)], fill=hand_color, width=hw)

    # Center dot
    dr = max(1, size * 0.01)
    draw.ellipse(
        [clock_cx - dr, clock_cy - dr, clock_cx + dr, clock_cy + dr],
        fill=hand_color,
    )

    return img


def main():
    out_dir = "/tmp/bambu_icon.iconset"
    os.makedirs(out_dir, exist_ok=True)

    for sz in SIZES:
        img = draw_icon(sz)
        img.save(f"{out_dir}/icon_{sz}x{sz}.png")
        if sz <= 512:
            img2x = draw_icon(sz * 2)
            img2x.save(f"{out_dir}/icon_{sz}x{sz}@2x.png")

    icns_path = "/tmp/AppIcon.icns"
    subprocess.run(["iconutil", "-c", "icns", out_dir, "-o", icns_path], check=True)
    print(f"Icon created: {icns_path}")


if __name__ == "__main__":
    main()
