"""The "Providers…" panel: five checkboxes in a native macOS alert that stays open while the user ticks and unticks
them, then Save or Cancel. And the "Foundry prices…" window: one row of price fields per deployment.

Why a panel: macOS closes a menu when one of its items is clicked, and SwiftBar has no item that runs a command and
keeps the menu open, so ticking several providers in the menu itself is not possible.

Each window is a fixed JavaScript-for-Automation script (`SCRIPT`, `PRICES_SCRIPT`), run by /usr/bin/osascript
without a shell and with the fixed environment. Nothing is ever put into the script text: provider keys, labels,
states, notes, deployment names and prices are osascript arguments, read by the script's `run(argv)`. The output is
parsed strictly (`parse`, `parse_prices`); anything unexpected, a non-zero exit, Cancel or a timeout means no change.
The prices window answers with the typed texts by row position only: the deployment names never come back from it.
"""
import json
import re
import subprocess

from ai_limits.common import run

OSASCRIPT = "/usr/bin/osascript"
TIMEOUT = 600  # seconds: a forgotten panel is closed, without a change, after 10 minutes
# "panel" is the only mode the plugin uses. The self-test modes (opt-in GUI test only, never reachable from the
# config, the environment or the command line) click the first checkbox (the prices window: type "1,5" into the first
# field) and then Save or Cancel by themselves after about a second, and add diagnostic lines starting with "#".
MODES = ("panel", "selftest-save", "selftest-cancel")
_LINE = re.compile(r"([a-z0-9_]{1,20})=(on|off)")
PRICE_COLUMNS = 5  # Input, Output, Cache read, Write 5m, Write 1h: foundry.PRICE_KEYS in this order
MAX_TYPED = 40  # characters per price field in the answer

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


# The "Foundry prices…" window. Arguments: mode, message, then per row the deployment name, a note ("" or e.g.
# "no price") and the five prices as text. Save answers one line: a JSON array with, per row in the order given, the
# five field texts as typed; anything else (Cancel, OK without rows) answers "cancel".
PRICES_SCRIPT = r"""
ObjC.import('Cocoa');

function run(argv) {
  var mode = argv[0];
  var message = argv[1];
  var rows = [];
  for (var i = 2; i + 6 < argv.length; i += 7) {
    rows.push({label: argv[i], note: argv[i + 1], values: argv.slice(i + 2, i + 7)});
  }
  var app = $.NSApplication.sharedApplication;
  app.setActivationPolicy(1);  // accessory: no Dock icon, no menu bar of its own
  var editItems = 0;
  try {  // Cmd+X/C/V/A/Z reach a text field only through a main menu: an Edit menu that is never shown
    var edit = $.NSMenu.alloc.initWithTitle('Edit');
    [['Undo', 'undo:', 'z'], ['Cut', 'cut:', 'x'], ['Copy', 'copy:', 'c'], ['Paste', 'paste:', 'v'],
     ['Select All', 'selectAll:', 'a']].forEach(function (e) {
      edit.addItem($.NSMenuItem.alloc.initWithTitleActionKeyEquivalent(e[0], e[1], e[2]));
    });
    var holder = $.NSMenuItem.alloc.init;
    holder.submenu = edit;
    var menu = $.NSMenu.alloc.init;
    menu.addItem(holder);
    app.mainMenu = menu;
    editItems = Number(edit.numberOfItems);
  } catch (e) {}

  var alert = $.NSAlert.alloc.init;
  alert.messageText = 'Foundry prices';
  alert.informativeText = message;
  alert.addButtonWithTitle(rows.length > 0 ? 'Save' : 'OK');
  if (rows.length > 0) alert.addButtonWithTitle('Cancel');
  try {  // the menu bar's gauge instead of osascript's generic icon; optional (older macOS lacks the symbol API)
    var icon = $.NSImage.imageWithSystemSymbolNameAccessibilityDescription('gauge.with.dots.needle.67percent',
                                                                           'AI Limits Bar');
    if (icon && !icon.isNil()) {
      icon.size = $.NSMakeSize(48, 48);
      alert.icon = icon;
    }
  } catch (e) {}

  var headings = ['Input', 'Output', 'Cache read', 'Write 5m', 'Write 1h'];
  var nameWidth = 200, fieldWidth = 74, gap = 8, rowHeight = 40, headHeight = 20;
  var width = nameWidth + headings.length * (fieldWidth + gap);
  var height = rows.length > 0 ? headHeight + rows.length * rowHeight : 1;
  var view = $.NSView.alloc.initWithFrame($.NSMakeRect(0, 0, width, height));
  if (rows.length > 0) {
    headings.forEach(function (text, k) {
      var head = $.NSTextField.labelWithString(text);
      head.font = $.NSFont.boldSystemFontOfSize(11);
      head.setFrame($.NSMakeRect(nameWidth + k * (fieldWidth + gap), height - headHeight, fieldWidth, headHeight - 4));
      view.addSubview(head);
    });
  }
  var fields = rows.map(function (row, n) {
    var top = height - headHeight - n * rowHeight;  // the top edge of this row
    var name = $.NSTextField.labelWithString(row.label);
    name.lineBreakMode = 4;  // truncate at the end
    name.setFrame($.NSMakeRect(0, top - 20, nameWidth - gap, 18));
    view.addSubview(name);
    if (row.note.length > 0) {
      var note = $.NSTextField.labelWithString(row.note);
      note.font = $.NSFont.systemFontOfSize(11);
      note.textColor = $.NSColor.systemOrangeColor;
      note.lineBreakMode = 4;
      note.setFrame($.NSMakeRect(0, top - 36, nameWidth - gap, 15));
      view.addSubview(note);
    }
    return row.values.map(function (value, k) {
      var field = $.NSTextField.textFieldWithString(value);
      field.placeholderString = '–';
      field.setFrame($.NSMakeRect(nameWidth + k * (fieldWidth + gap), top - 26, fieldWidth, 22));
      view.addSubview(field);
      return field;
    });
  });
  var flat = [].concat.apply([], fields);
  for (var k = 0; k + 1 < flat.length; k++) flat[k].nextKeyView = flat[k + 1];  // Tab goes along each row
  alert.accessoryView = view;
  alert.layout;
  if (flat.length > 0) alert.window.initialFirstResponder = flat[0];
  alert.window.hidesOnDeactivate = false;  // an accessory app could not bring a hidden panel back
  alert.window.level = 3;  // floating: stays above other windows until Save or Cancel
  app.activateIgnoringOtherApps(true);

  var seen = [];
  if (mode === 'selftest-save' || mode === 'selftest-cancel') {
    var timer = $.NSTimer.timerWithTimeIntervalRepeatsBlock(1.0, false, function () {
      seen.push('#active=' + app.isActive, '#key=' + alert.window.isKeyWindow, '#visible=' + alert.window.isVisible,
                '#edit=' + editItems);
      if (flat.length > 0) flat[0].stringValue = '1,5';
      alert.buttons.objectAtIndex(mode === 'selftest-save' || rows.length === 0 ? 0 : 1).performClick(null);
    });
    $.NSRunLoop.currentRunLoop.addTimerForMode(timer, $.NSModalPanelRunLoopMode);
  } else if (mode !== 'panel') {
    return 'cancel';
  }

  var response = Number(alert.runModal);
  try { alert.window.makeFirstResponder(null); } catch (e) {}  // ends the editing of a field still being typed in
  var out = response === 1000 && rows.length > 0
    ? [JSON.stringify(fields.map(function (row) {
        return row.map(function (field) { return ObjC.unwrap(field.stringValue); });
      }))]
    : ['cancel'];
  return out.concat(seen).join('\n');
}
"""


def price_arguments(rows, message, mode="panel"):
    """osascript's argument list for the prices window: the fixed PRICES_SCRIPT, then the mode, the message and seven
    strings per row (deployment name, note, five prices as text). rows: [(name, note, [five texts])]."""
    if mode not in MODES:
        raise ValueError("unknown panel mode")
    argv = [OSASCRIPT, "-l", "JavaScript", "-e", PRICES_SCRIPT, mode, str(message)]
    for name, note, texts in rows:
        if len(texts) != PRICE_COLUMNS:
            raise ValueError("five prices per row")
        argv += [str(name), str(note), *(str(t) for t in texts)]
    return argv


def show_prices(rows, message, mode="panel", timeout=TIMEOUT):
    """Runs the prices window and waits for it: (exit code, stdout, stderr); (1, "", "") if it could not run or timed
    out."""
    try:
        return run(price_arguments(rows, message, mode), timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def parse_prices(code, stdout, count):
    """The typed texts, [[five strings]] for `count` rows in the order they were given, or None (no change): a
    non-zero exit, Cancel, more than one line, or anything but a JSON array of `count` arrays of five strings of at
    most MAX_TYPED characters. The texts themselves are checked by the caller (foundry.parse_price)."""
    if code != 0 or not isinstance(stdout, str):
        return None
    text = stdout[:-1] if stdout.endswith("\n") else stdout
    if "\n" in text or not text.startswith("["):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list) or len(data) != count:
        return None
    for typed in data:
        if not (isinstance(typed, list) and len(typed) == PRICE_COLUMNS
                and all(isinstance(t, str) and len(t) <= MAX_TYPED for t in typed)):
            return None
    return data
