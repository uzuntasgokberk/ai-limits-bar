"""The menu bar strip: the gauge and its percentage, then one ring per provider (right arc: 5-hour usage, left arc:
weekly usage; one continuous arc for a provider with a single limit), drawn as one PNG image per menu bar appearance
and passed to SwiftBar as `image=<light>,<dark>`.

Drawing: a fixed JavaScript-for-Automation script (`SCRIPT`) run by /usr/bin/osascript without a shell and with
the fixed environment, like the panels in panel.py. Nothing is ever put into the script text: every value is an
osascript argument, checked against a strict pattern before the run (`arguments`). The answer must be exactly two
lines of base64, each a PNG of the expected height within the size caps (`parse`); anything else, a non-zero exit or
the timeout means no strip, and the menu bar shows the gauge and percentage as text instead (render.menu).
The images are cached in the cache folder by their input values: a run with the same values starts no process.

Logos: the middle of a ring is a mark from two folders, by fixed file names (never from provider data or config):
the user's own files in LOGO_DIR (`claude.svg`, `codex-light.png` ...) win over the project's own drawings shipped in
SHIPPED_DIR (`claude.pdf`, `codex-light.pdf` ...). For each menu bar variant (light, dark) the names tried are
`<provider>-<variant>.*`, then `<provider>.*`. A file must be a regular file (not a link) in a folder that is not a link,
owned by the user (a shipped one also by root), not writable by group or others, of 1 byte to LOGO_MAX_BYTES; anything
else is skipped. The provider's initials are the last fallback.
"""
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import struct
import subprocess

from ai_limits import render
from ai_limits.common import HOME, run, write_private_json

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT = 5  # seconds; a drawing takes about 0.15 s
CACHE_FILE = "menu-bar-image.json"
LOGO_DIR = os.path.join(HOME, "Library", "Application Support", "ai-limits-bar", "logos")
LOGO_TYPES = ("svg", "pdf", "png")
# The project's own drawings of the marks, as vector PDFs (NSImage reads PDF on every macOS; their SVG sources and the
# converter are in logos/source/). Drawn for a dark menu bar; `-light` files are the light-bar versions of marks that
# would vanish there (white or light grey).
SHIPPED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos")
SHIPPED_TYPES = ("pdf",)
VARIANTS = ("light", "dark")
LOGO_MAX_BYTES = 512 * 1024
# Shown in the middle of a ring without a usable logo file: fixed, never from provider data.
INITIALS = {"claude": "Cl", "codex": "Cx", "gemini": "Ge", "copilot": "Cp"}
SINGLE_LIMIT = {"copilot"}  # one limit: one continuous arc
# Providers whose ring follows one of several quota groups: the choices (first = default) and their fixed labels.
GROUPS = {"gemini": ("gemini", "3p")}
GROUP_LABELS = {"gemini": {"gemini": "Gemini models", "3p": "Claude and GPT models"}}
RING_NAMES = {("gemini", "3p"): "Gemini (Claude GPT)"}  # in the tooltip; otherwise render.NAMES
MAX_RINGS = 8
HEIGHT = 22  # points; the PNGs are drawn at 2x, so they are HEIGHT * SCALE pixels high
SCALE = 2
MAX_WIDTH = 400  # points
MAX_PNG_BYTES = 96 * 1024
LEVELS = {None: "n", render.ORANGE: "o", render.RED: "r"}  # render.color(percent) -> argument
GAUGE_SYMBOLS = {render.icon(p) for p in (None, 0, 50, 70, 90)}
_GAUGE_TEXT = re.compile(r"\d{1,3}%|–")
_VALUE = re.compile(r"|\d{1,3}")
_B64 = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"

# Geometry: the design's arcs of 164 degrees with a 16-degree gap at the top and the bottom and butt ends; sized for
# the optical weight of the gauge next to them (a thin ring with a mark inside reads smaller than a solid glyph of the
# same diameter): the gauge's visible circle is GAUGE_D (20 points), a ring fills the image height (RING_D, 22) with
# a 1.5-point stroke, and its mark is as large as the ring allows with MARK_GAP (1.25 points) clear of the arc (the
# longer side of the mark; a wide mark keeps its aspect). A single-limit ring is one arc of 344 degrees with its
# 16-degree gap at the top, running clockwise from the gap.
# Numbers instead of AppKit constant names: the JavaScript bridge does not export all of them.
SCRIPT = r"""
ObjC.import('AppKit');

var H = 22, SCALE = 2, GAUGE_D = 20, RING_D = 22, SPAN = 164, HALF_GAP = 8, STROKE = 1.5, MARK_GAP = 1.25;
var R = (RING_D - STROKE) / 2, MARK = 2 * (R - STROKE / 2 - MARK_GAP);  // 16.5: the mark's longer side
var RING = RING_D, RING_SPACE = 6, GAUGE_SPACE = 8, PAD = 1;
var GAUGE_INK = 16.75 / 20;  // the gauge symbol's visible circle in its image (13 pt, large scale: 16.75 of 20)
var VARIANTS = [{fg: [0.12, 0.12, 0.12], track: 0.28}, {fg: [0.949, 0.949, 0.949], track: 0.3}];  // light, dark bar
var LEVEL = {o: [0.910, 0.639, 0.239], r: [0.898, 0.282, 0.302]};  // render.ORANGE, render.RED

function rgb(c, a) { return $.NSColor.colorWithSRGBRedGreenBlueAlpha(c[0], c[1], c[2], a); }

function arc(cx, cy, start, end, clockwise, color) {
  var p = $.NSBezierPath.bezierPath;
  p.appendBezierPathWithArcWithCenterRadiusStartAngleEndAngleClockwise($.NSMakePoint(cx, cy), R, start, end, clockwise);
  p.lineWidth = STROKE;
  p.lineCapStyle = 0;  // butt
  color.setStroke;
  p.stroke;
}

function used(value, level, fg, span) {  // -> [degrees, colour] or null
  if (value === '') return null;
  var v = Math.max(0, Math.min(100, Number(value)));
  return [span * v / 100, level === 'n' ? rgb(fg, 1) : rgb(LEVEL[level], 1)];
}

function loadLogo(path) {
  if (path === '') return null;
  try {
    var img = $.NSImage.alloc.initWithContentsOfFile(path);
    if (img.isNil() || !img.isValid || img.size.width <= 0 || img.size.height <= 0) return null;
    return img;
  } catch (e) { return null; }
}

function drawRing(x, ring, variant) {
  var cx = x + RING / 2, cy = H / 2, fg = variant.fg;
  var track = rgb(fg, variant.track), right, left = null;
  if (ring.shape === '1') {  // one limit: one arc, clockwise from the gap at the top
    var span = 360 - 2 * HALF_GAP;
    arc(cx, cy, 90 - HALF_GAP, 90 + HALF_GAP - 360, true, track);
    right = used(ring.right, ring.rightLevel, fg, span);
    if (right && right[0] > 0) arc(cx, cy, 90 - HALF_GAP, 90 - HALF_GAP - right[0], true, right[1]);
  } else {
    arc(cx, cy, -90 + HALF_GAP, 90 - HALF_GAP, false, track);   // right: 5-hour, from the bottom up
    arc(cx, cy, 270 - HALF_GAP, 90 + HALF_GAP, true, track);    // left: weekly, from the bottom up
    right = used(ring.right, ring.rightLevel, fg, SPAN);
    left = used(ring.left, ring.leftLevel, fg, SPAN);
    if (right && right[0] > 0) arc(cx, cy, -90 + HALF_GAP, -90 + HALF_GAP + right[0], false, right[1]);
    if (left && left[0] > 0) arc(cx, cy, 270 - HALF_GAP, 270 - HALF_GAP - left[0], true, left[1]);
  }
  var alpha = (right || left) ? 1 : 0.4;  // no current value: track only, the middle dimmed
  var logo = loadLogo(variant.dark ? ring.logoDark : ring.logoLight);
  if (logo) {
    var aspect = logo.size.width / logo.size.height;
    var w = aspect >= 1 ? MARK : MARK * aspect, h = aspect >= 1 ? MARK / aspect : MARK;
    logo.drawInRectFromRectOperationFraction($.NSMakeRect(cx - w / 2, cy - h / 2, w, h), $.NSMakeRect(0, 0, 0, 0),
                                             2, alpha);  // source over
  } else {
    var font = $.NSFont.boldSystemFontOfSize(MARK * 0.6);
    var attrs = $.NSDictionary.dictionaryWithObjectsForKeys($([font, rgb(fg, alpha)]), $(['NSFont', 'NSColor']));
    var text = $(ring.initials), size = text.sizeWithAttributes(attrs);
    text.drawAtPointWithAttributes($.NSMakePoint(cx - size.width / 2, cy - size.height / 2), attrs);
  }
}

function gaugeRect(symbol, x) {  // the symbol drawn so that its visible circle is GAUGE_D high, centred
  var side = GAUGE_D / GAUGE_INK, w = side * symbol.size.width / symbol.size.height;
  return $.NSMakeRect(x - (w - GAUGE_D) / 2, (H - side) / 2, w, side);
}

function gaugeSymbol(name, color) {
  try {
    var img = $.NSImage.imageWithSystemSymbolNameAccessibilityDescription(name, $());
    if (img.isNil()) return null;
    var config = $.NSImageSymbolConfiguration.configurationWithPointSizeWeightScale(13, 0, 3);
    try {
      config = config.configurationByApplyingConfiguration(
        $.NSImageSymbolConfiguration.configurationWithPaletteColors($([color])));
    } catch (e) {}
    return img.imageWithSymbolConfiguration(config);
  } catch (e) { return null; }
}

function drawStrip(W, gauge, rings, variant) {
  var rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
    null, W * SCALE, H * SCALE, 8, 4, true, false, 'NSDeviceRGBColorSpace', 0, 0);
  rep.size = $.NSMakeSize(W, H);  // points: the PNG says 144 dpi, so it shows at W x H points
  var ctx = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext(ctx);
  var x = PAD;
  if (gauge.shown) {
    var symbol = gaugeSymbol(gauge.symbol, rgb(variant.fg, 1));
    if (symbol) {
      symbol.drawInRect(gaugeRect(symbol, x));
      x += GAUGE_D + 3;
    }
    var color = gauge.level === 'n' ? rgb(variant.fg, 1) : rgb(LEVEL[gauge.level], 1);
    var attrs = $.NSDictionary.dictionaryWithObjectsForKeys($([$.NSFont.menuBarFontOfSize(0), color]),
                                                            $(['NSFont', 'NSColor']));
    var text = $(gauge.text), size = text.sizeWithAttributes(attrs);
    text.drawAtPointWithAttributes($.NSMakePoint(x, (H - size.height) / 2), attrs);
    x += size.width + GAUGE_SPACE;
  }
  rings.forEach(function (ring, n) { drawRing(x + n * (RING + RING_SPACE), ring, variant); });
  ctx.flushGraphics;
  $.NSGraphicsContext.restoreGraphicsState;
  return rep.representationUsingTypeProperties(4, $({})).base64EncodedStringWithOptions(0).js;  // 4: PNG
}

function run(argv) {
  if (argv.length < 6 || argv[0] !== 'strip') return '';
  var gauge = {shown: argv[1] === '1', symbol: argv[2], text: argv[3], level: argv[4]};
  var count = Number(argv[5]);
  if (argv.length !== 6 + 8 * count) return '';
  var rings = [];
  for (var i = 6; i < argv.length; i += 8) {
    rings.push({shape: argv[i], initials: argv[i + 1], logoLight: argv[i + 2], logoDark: argv[i + 3],
                right: argv[i + 4], rightLevel: argv[i + 5], left: argv[i + 6], leftLevel: argv[i + 7]});
  }
  var W = PAD * 2 + rings.length * RING + Math.max(0, rings.length - 1) * RING_SPACE;
  if (gauge.shown) {
    var symbol = gaugeSymbol(gauge.symbol, $.NSColor.blackColor);
    var attrs = $.NSDictionary.dictionaryWithObjectsForKeys($([$.NSFont.menuBarFontOfSize(0)]), $(['NSFont']));
    W += (symbol ? GAUGE_D + 3 : 0) + $(gauge.text).sizeWithAttributes(attrs).width
         + (rings.length ? GAUGE_SPACE : 0);
  }
  W = Math.ceil(W);
  return VARIANTS.map(function (v, n) {
    return drawStrip(W, gauge, rings, {fg: v.fg, track: v.track, dark: n === 1});
  }).join('\n');
}
"""


def groups(value):
    """{provider: group} for the ring of each provider that has quota groups: the config's `ring_group` where it is
    a valid choice, otherwise the default (the first choice)."""
    value = value if isinstance(value, dict) else {}
    return {p: value[p] if value.get(p) in choices else choices[0] for p, choices in GROUPS.items()}


def specs(results, chosen, now):
    """One ring per shown provider that has one, in the menu's order: {"key", "name", "right", "left"} with the
    current 5-hour (right) and weekly (left) percentage, or None where there is none (missing, stale, or rows that
    don't say their window). Rows are picked by their `window` and `group` only, never by label. Foundry (no
    percentage) and providers without a section get no ring."""
    out = []
    for key, r in results:
        if key not in INITIALS or not isinstance(r, dict):
            continue
        rows = [x for x in r.get("rows", []) if isinstance(x, dict) and x.get("percent") is not None]
        if key in SINGLE_LIMIT:
            right = left = render.current_percent(rows[0], now) if rows else None
        else:
            group = chosen.get(key)

            def pick(window):
                found = next((x for x in rows if x.get("window") == window and x.get("group") == group), None)
                return render.current_percent(found, now) if found else None
            right, left = pick("5h"), pick("weekly")
        out.append({"key": key, "name": RING_NAMES.get((key, chosen.get(key)), render.NAMES[key]),
                    "right": right, "left": left, "single": key in SINGLE_LIMIT})
    return out[:MAX_RINGS]


def _private(st, owners):
    return st.st_uid in owners and not st.st_mode & 0o022


def _logo(folder, name, types, owners):
    """(path, mtime_ns, size) of folder/<name>.<type> (the first type that exists and passes), or None: the folder
    and the file must not be symlinks, must be owned by one of `owners` and not writable by group or others, and the
    file must be a regular file of 1 byte to LOGO_MAX_BYTES."""
    try:
        st = os.lstat(folder)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or not _private(st, owners):
        return None
    for ext in types:
        path = os.path.join(folder, f"{name}.{ext}")
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and _private(st, owners) and 0 < st.st_size <= LOGO_MAX_BYTES:
            return path, st.st_mtime_ns, st.st_size
    return None


def logos(provider):
    """(light, dark): the logo file for each menu bar variant, (path, mtime_ns, size) or None (initials). Tried in
    order: the user's `<provider>-<variant>.*` and `<provider>.*`, then the shipped ones."""
    if provider not in INITIALS:
        return None, None
    user, shipped = {os.getuid()}, {os.getuid(), 0}
    out = []
    for variant in VARIANTS:
        out.append(_logo(LOGO_DIR, f"{provider}-{variant}", LOGO_TYPES, user)
                   or _logo(LOGO_DIR, provider, LOGO_TYPES, user)
                   or _logo(SHIPPED_DIR, f"{provider}-{variant}", SHIPPED_TYPES, shipped)
                   or _logo(SHIPPED_DIR, provider, SHIPPED_TYPES, shipped))
    return out[0], out[1]


def _value(percent):
    return "" if percent is None else str(int(round(max(0.0, min(100.0, percent)))))


def _level(percent):
    return LEVELS.get(render.color(percent), "n")


def inputs(gauge, rings):
    """The drawing's inputs as plain values (also the cache key): the gauge (shown, symbol, text, level) and per ring
    the initials, the logo files and the two arcs. gauge: (shown, percent) where percent may be None."""
    shown, percent = gauge
    percent = percent if shown else None  # a hidden gauge's number would only cause redraws
    text = "–" if percent is None else f"{max(0.0, min(999.0, percent)):.0f}%"
    out = {"gauge": [bool(shown), render.icon(percent), text, _level(percent)], "rings": []}
    for spec in rings[:MAX_RINGS]:
        light, dark = logos(spec["key"])
        out["rings"].append(["1" if spec.get("single") else "2", INITIALS[spec["key"]], light, dark,
                             _value(spec["right"]), _level(spec["right"]), _value(spec["left"]), _level(spec["left"])])
    return out


def arguments(values):
    """osascript's argument list for `inputs()`: the fixed script, then "strip", the gauge and eight strings per
    ring. Every value is checked first; anything unexpected raises ValueError and nothing is run."""
    shown, symbol, text, level = values["gauge"]
    if symbol not in GAUGE_SYMBOLS or not _GAUGE_TEXT.fullmatch(text) or level not in LEVELS.values():
        raise ValueError("bad gauge")
    rings = values["rings"]
    if len(rings) > MAX_RINGS:
        raise ValueError("too many rings")
    argv = [OSASCRIPT, "-l", "JavaScript", "-e", SCRIPT, "strip", "1" if shown else "0", symbol, text, level,
            str(len(rings))]
    for shape, initials, light, dark, right, right_level, left, left_level in rings:
        if shape not in ("1", "2") or initials not in INITIALS.values():
            raise ValueError("bad ring")
        for logo in (light, dark):
            if logo is not None and os.path.dirname(logo[0]) not in (LOGO_DIR, SHIPPED_DIR):
                raise ValueError("bad logo path")
        for value, lvl in ((right, right_level), (left, left_level)):
            if not _VALUE.fullmatch(value) or (value and int(value) > 100) or lvl not in LEVELS.values():
                raise ValueError("bad arc")
        argv += [shape, initials, light[0] if light else "", dark[0] if dark else "", right, right_level, left,
                 left_level]
    return argv


def check_png(data, width=None):
    """The PNG's width in pixels if `data` is an RGBA PNG HEIGHT * SCALE pixels high, at most MAX_WIDTH * SCALE wide
    (and exactly `width` if given) and MAX_PNG_BYTES long, ending with IEND; otherwise None."""
    if not isinstance(data, bytes) or not 33 + 12 <= len(data) <= MAX_PNG_BYTES:
        return None
    if data[:8] != _PNG_SIGNATURE or data[12:16] != b"IHDR" or data[-12:] != _PNG_END:
        return None
    w, h = struct.unpack(">II", data[16:24])
    if h != HEIGHT * SCALE or not 1 <= w <= MAX_WIDTH * SCALE or data[24] != 8 or data[25] != 6:
        return None
    if width is not None and w != width:
        return None
    return w


def parse(code, stdout):
    """(light, dark) base64 texts, re-encoded from the checked bytes, or None: a non-zero exit, not exactly two lines,
    a character outside the base64 alphabet, or a PNG that fails check_png (both must be the same width)."""
    if code != 0 or not isinstance(stdout, str):
        return None
    lines = stdout[:-1].split("\n") if stdout.endswith("\n") else stdout.split("\n")
    if len(lines) != 2:
        return None
    images, width = [], None
    for line in lines:
        if not _B64.fullmatch(line) or len(line) > MAX_PNG_BYTES * 4 // 3 + 4:
            return None
        try:
            data = base64.b64decode(line, validate=True)
        except (binascii.Error, ValueError):
            return None
        width = check_png(data, width)
        if width is None:
            return None
        images.append(base64.b64encode(data).decode("ascii"))
    return images[0], images[1]


def draw(values):
    """Runs the script for `inputs()` values: (light, dark) or None (refused input, failure, timeout, bad output)."""
    try:
        code, out, _ = run(arguments(values), timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return parse(code, out)


def _key(values):
    payload = json.dumps([hashlib.sha256(SCRIPT.encode()).hexdigest(), values], sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _cached(cache, key):
    try:
        with open(os.path.join(cache, CACHE_FILE), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("key") == key:
            light, dark = data.get("light"), data.get("dark")
            if isinstance(light, str) and isinstance(dark, str):
                return parse(0, f"{light}\n{dark}")  # checked again: the file is outside this process
    except (OSError, ValueError):
        pass
    return None


def strip(rings, gauge, cache):
    """The strip as (light, dark) base64 PNGs, or None (no ring to draw, or drawing failed: the menu bar then shows
    the gauge and percentage as text). rings: from specs(); gauge: (shown, percent). Cached in `cache` by its input
    values, so a run with the same values starts no process."""
    if not rings:
        return None
    values = inputs(gauge, rings)
    key = _key(values)
    found = _cached(cache, key)
    if found:
        return found
    found = draw(values)
    if found:
        try:
            write_private_json(cache, CACHE_FILE, {"key": key, "light": found[0], "dark": found[1]})
        except OSError:
            pass  # drawn again next time
    return found
