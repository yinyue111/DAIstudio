"""Local gateway placeholders used when mock mode is enabled."""

from __future__ import annotations

import base64
import io

# A deterministic 1-second, 640x360 H.264 MP4 used by offline/mock video flows.
# Keeping the fixture embedded avoids making mock mode depend on ffmpeg at runtime.
_MOCK_VIDEO_B64 = """
AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAM2bW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAA
A+gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAA
AAAAAAAAAAAAAAAAAAAAAgAAAmB0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAA+gAAAAAAAAAAAAA
AAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAoAAAAFoAAAAAAAkZWR0cwAAABxlbHN0
AAAAAAAAAAEAAAPoAAAAAAABAAAAAAHYbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAABAAAAAQABVxAAAAAAALWhk
bHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABg21pbmYAAAAUdm1oZAAAAAEAAAAAAAAA
AAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAUNzdGJsAAAAu3N0c2QAAAAAAAAAAQAAAKth
dmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAoABaABIAAAASAAAAAAAAAABFUxhdmM2Mi4yOC4xMDAgbGlieDI2
NAAAAAAAAAAAAAAAGP//AAAAMWF2Y0MBQsAe/+EAGWdCwB7ZAKAv+XARAAADAAEAAAMACA8WLkgBAAVoy4CUsgAA
ABBwYXNwAAAAAQAAAAEAAAAUYnRydAAAAAAAADWYAAAAAAAAABhzdHRzAAAAAAAAAAEAAAAEAAAQAAAAABRzdHNz
AAAAAAAAAAEAAAABAAAAHHN0c2MAAAAAAAAAAQAAAAEAAAAEAAAAAQAAACRzdHN6AAAAAAAAAAAAAAAEAAAGMgAA
ACsAAAArAAAAKwAAABRzdGNvAAAAAAAAAAEAAANmAAAAYnVkdGEAAABabWV0YQAAAAAAAAAhaGRscgAAAAAAAAAA
bWRpcmFwcGwAAAAAAAAAAAAAAAAtaWxzdAAAACWpdG9vAAAAHWRhdGEAAAABAAAAAExhdmY2Mi4xMi4xMDAAAAAI
ZnJlZQAABrttZGF0AAACcQYF//9t3EXpvebZSLeWLNgg2SPu73gyNjQgLSBjb3JlIDE2NSByMzIyMiBiMzU2MDVh
IC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyNSAtIGh0dHA6Ly93d3cudmlkZW9s
YW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvbnM6IGNhYmFjPTAgcmVmPTMgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4
MToweDExMSBtZT1oZXggc3VibWU9NyBwc3k9MSBwc3lfcmQ9MS4wMDowLjAwIG1peGVkX3JlZj0xIG1lX3Jhbmdl
PTE2IGNocm9tYV9tZT0xIHRyZWxsaXM9MSA4eDhkY3Q9MCBjcW09MCBkZWFkem9uZT0yMSwxMSBmYXN0X3Bza2lw
PTEgY2hyb21hX3FwX29mZnNldD0tMiB0aHJlYWRzPTExIGxvb2thaGVhZF90aHJlYWRzPTEgc2xpY2VkX3RocmVh
ZHM9MCBucj0wIGRlY2ltYXRlPTEgaW50ZXJsYWNlZD0wIGJsdXJheV9jb21wYXQ9MCBjb25zdHJhaW5lZF9pbnRy
YT0wIGJmcmFtZXM9MCB3ZWlnaHRwPTAga2V5aW50PTI1MCBrZXlpbnRfbWluPTQgc2NlbmVjdXQ9NDAgaW50cmFf
cmVmcmVzaD0wIHJjX2xvb2thaGVhZD00MCByYz1jcmYgbWJ0cmVlPTEgY3JmPTM1LjAgcWNvbXA9MC42MCBxcG1p
bj0wIHFwbWF4PTY5IHFwc3RlcD00IGlwX3JhdGlvPTEuNDAgYXE9MToxLjAwAIAAAAO5ZYiEBLxGKAAJHccAAQCo
4ABWTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5OTk5Ouuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu
uuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu
uuuuaCQHAAEAYAAnEEAFEABAe0CW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbWnrrrrrrrp66666666666
666666666666666666666euuuuuuunrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrp66666666euuuuuuuuuuuuuuuuu
uuuuuuuuuuuuuunrrrrrrrp664qCYBwADYHFgpLCBA6EQEhITIkJnEJkQmdra2tra2tra2tra2tra2tra2tra2tr
a2trTBPXXT111111109deH8AwDhEEwgC7EKbW1tbW1tbW1tbW1tbW1tbW1tbW1tbW1tbWmCeuunrrrrrrrp66///
4LQRYHgzMsqMs2m3Lpda2tra2tra2tra2tra2tra//jwwVX5t5dmYI6O0tLS0tLS0tdddPXXXXXXXT114fxDAOC0
EgOhEeC2KOfT7k0mtEdra2tra2tra2tra2tra2v//8FRTtz7ybTBLXXXXXXXXXXT111111109ddra2tra2tra2tr
a2tra2tra2tdddddddddddPXXXXXXXT118Q+IYBw2CIGAsy+JCR4iNCAjomk0+v0GSllJSyqcsqnLa2tra2tra2t
ra2tra2tra2tra2trjw//BVOwOvJs+wZKXlU5emCOuuuuunrrrrrrrp666WlpaWlpaWlpaWlpaWlpaWlpaWlpaWl
rrrrrrp66666666euuuuuuuuuuuuuuuuuuuuuuuuuuuuuuunrrrrrrrp66666666666666666666666666666666
euuuuuuunrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrrp66666668P4BgHCIJAOSwkb9ra2tra2tra2tra2tra2tra2t
ra2tra2tra2tra2trTBLXXXXXXXS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tLS0tdddddddddddddddddd
dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
dddddddddddddddddeAAAAAnQZo4CXgEgP5/P5/P5/P5/P5/P5/P5/P5/P5/P5/P5/P5/P5+AJggAAAAJ0GaVAI+
ASA/n8/n8/n8/n8/n8/n8/n8/n8/n8/n8/n8/n8/n4AmCAAAACdBmmAQ8AkB/P5/P5/P5/P5/P5/P5/P5/P5/P5/
P5/P5/P5/PwBMEA=
"""


def mock_image(prompt: str, size: str, idx: int) -> bytes:
    from PIL import Image, ImageDraw

    try:
        w, h = (int(x) for x in size.lower().split("x"))
    except Exception:
        w, h = 1024, 1024
    img = Image.new("RGB", (w, h))
    px = img.load()
    seed = (hash(prompt) + idx * 97) & 0xFFFFFF
    r0, g0, b0 = (seed >> 16) & 255, (seed >> 8) & 255, seed & 255
    for y in range(h):
        for x in range(0, w, 4):  # step for speed
            r = (r0 + x * 255 // w) % 256
            g = (g0 + y * 255 // h) % 256
            b = (b0 + (x + y) * 255 // (w + h)) % 256
            for dx in range(4):
                if x + dx < w:
                    px[x + dx, y] = (r, g, b)
    d = ImageDraw.Draw(img)
    d.text((24, 24), f"MOCK #{idx + 1}", fill=(255, 255, 255))
    d.text((24, 44), prompt[:60], fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def mock_video_preview_image() -> bytes:
    """Return a poster image for mock video results."""
    return mock_image("video preview", "640x360", 0)


def mock_video() -> bytes:
    """Return a real, browser-playable MP4 for mock video results."""
    return base64.b64decode(_MOCK_VIDEO_B64)
