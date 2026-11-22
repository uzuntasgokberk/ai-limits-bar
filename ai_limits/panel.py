"""The "Providers…" panel: five checkboxes in a native macOS alert that stays open while the user ticks and unticks
them, then Save or Cancel.

Why a panel: macOS closes a menu when one of its items is clicked, and SwiftBar has no item that runs a command and
keeps the menu open, so ticking several providers in the menu itself is not possible.

The panel is a fixed JavaScript-for-Automation script (`SCRIPT`), run by /usr/bin/osascript without a shell and with
the fixed environment. Nothing is ever put into the script text: the provider keys, labels, states and notes are
osascript arguments, read by the script's `run(argv)`. Its output is parsed strictly (`parse`); anything unexpected,
a non-zero exit, Cancel or a timeout means no change.
"""
import re
import subprocess

from ai_limits.common import run

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT = 600  # seconds: a forgotten panel is closed, without a change, after 10 minutes
# "panel" is the only mode the plugin uses. The self-test modes (opt-in GUI test only, never reachable from the
# config, the environment or the command line) click the first checkbox and then Save or Cancel by themselves after
# about a second, and add diagnostic lines starting with "#".
MODES = ("panel", "selftest-save", "selftest-cancel")
_LINE = re.compile(r"([a-z0-9_]{1,20})=(on|off)")

# Numbers instead of AppKit constant names: the JavaScript bridge does not export all of them. Values read back from
# AppKit (button state, modal response) arrive as strings, hence Number(...).
SCRIPT = r"""
ObjC.import('Cocoa');

function run(argv) {
  var mode = argv[0];
  var rows = [];
  for (var i = 1; i + 3 < argv.length; i += 4) {
    rows.push({key: argv[i], label: argv[i + 1], on: argv[i + 2] === 'on', note: argv[i + 3]});
  }
  var app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(1);  // accessory: no Dock icon, no menu bar of its own

  var alert = $.NSAlert.alloc.init;
  alert.messageText = 'Providers';
  alert.informativeText = 'Tick the providers to read and show, then Save.';
  alert.addButtonWithTitle('Save');
  alert.addButtonWithTitle('Cancel');
  try {  // the menu bar's gauge instead of osascript's generic icon; optional (older macOS lacks the symbol API)
    var icon = $.NSImage.imageWithSystemSymbolNameAccessibilityDescription('gauge.with.dots.needle.67percent',
                                                                           'AI Limits Bar');
    if (icon && !icon.isNil()) {
      icon.size = $.NSMakeSize(48, 48);
      alert.icon = icon;
    }
  } catch (e) {}

  var rowHeight = 24, width = 340;
  var view = $.NSView.alloc.initWithFrame($.NSMakeRect(0, 0, width, rows.length * rowHeight));
  var boxes = rows.map(function (row, n) {
    var y = (rows.length - 1 - n) * rowHeight;
    var box = $.NSButton.alloc.initWithFrame($.NSMakeRect(0, y, width, rowHeight));
    box.setButtonType(3);  // checkbox
    box.title = row.label;
    box.state = row.on ? 1 : 0;
    box.sizeToFit;
    box.setFrameOrigin($.NSMakePoint(0, y + (rowHeight - box.frame.size.height) / 2));
    view.addSubview(box);
    if (row.note.length > 0) {  // grey text next to the checkbox (attributed titles are not bridged)
      var note = $.NSTextField.labelWithString(row.note);
      note.textColor = $.NSColor.secondaryLabelColor;
      note.setFrameOrigin($.NSMakePoint(box.frame.size.width, y + (rowHeight - note.fittingSize.height) / 2));
      note.setFrameSize(note.fittingSize);
      view.addSubview(note);
    }
    return box;
  });
  alert.accessoryView = view;
  alert.layout;
  alert.window.hidesOnDeactivate = false;  // an accessory app could not bring a hidden panel back
  alert.window.level = 3;  // floating: stays above other windows until Save or Cancel
  app.activateIgnoringOtherApps(true);

  var seen = [];
  if (mode === 'selftest-save' || mode === 'selftest-cancel') {
    var timer = $.NSTimer.timerWithTimeIntervalRepeatsBlock(1.0, false, function () {
      seen.push('#active=' + app.isActive, '#key=' + alert.window.isKeyWindow, '#visible=' + alert.window.isVisible);
      if (boxes.length > 0) boxes[0].performClick(null);
      alert.buttons.objectAtIndex(mode === 'selftest-save' ? 0 : 1).performClick(null);
    });
    $.NSRunLoop.currentRunLoop.addTimerForMode(timer, $.NSModalPanelRunLoopMode);
  } else if (mode !== 'panel') {
    return 'cancel';
  }

  var response = Number(alert.runModal);
  var out = response === 1000
    ? rows.map(function (row, n) { return row.key + '=' + (Number(boxes[n].state) === 1 ? 'on' : 'off'); })
    : ['cancel'];
  return out.concat(seen).join('\n');
}
"""


def arguments(rows, mode="panel"):
    """osascript's argument list: the fixed script, then the mode and four strings per provider (key, label, on/off,
    note). rows: [(key, label, enabled, note)]."""
    if mode not in MODES:
        raise ValueError("unknown panel mode")
    argv = [OSASCRIPT, "-l", "JavaScript", "-e", SCRIPT, mode]
    for key, label, enabled, note in rows:
        argv += [str(key), str(label), "on" if enabled else "off", str(note)]
    return argv


def show(rows, mode="panel", timeout=TIMEOUT):
    """Runs the panel and waits for it: (exit code, stdout, stderr); (1, "", "") if it could not run or timed out."""
    try:
        return run(arguments(rows, mode), timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def parse(code, stdout, names):
    """{name: on?} for exactly the given names, or None (no change): a non-zero exit, Cancel, an unknown or repeated
    key, a missing key, or any other text."""
    if code != 0 or not isinstance(stdout, str):
        return None
    text = stdout[:-1] if stdout.endswith("\n") else stdout
    chosen = {}
    for line in text.split("\n"):
        found = _LINE.fullmatch(line)
        if not found or found.group(1) not in names or found.group(1) in chosen:
            return None
        chosen[found.group(1)] = found.group(2) == "on"
    return chosen if set(chosen) == set(names) else None
