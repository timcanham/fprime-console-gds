"""
fprime_console_gds.app:

Textual application of the F Prime terminal UI: latest telemetry on the left, colourized events as the primary area,
and commanding (command drop-down with argument form, paste-able command string, and history) across the bottom.
"""
import asyncio
import difflib
import json
import math
import re
import signal
import struct
import threading
import time
from collections import deque
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult, ScreenStackError
from textual.binding import Binding
from textual.containers import Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option

from fprime_gds.common.data_types.cmd_data import CmdData, CommandArgumentsException
from fprime_gds.common.models.serialize.array_type import ArrayType
from fprime_gds.common.models.serialize.bool_type import BoolType
from fprime_gds.common.models.serialize.enum_type import EnumType
from fprime_gds.common.models.serialize.numerical_types import F32Type, F64Type
from fprime_gds.common.models.serialize.serializable_type import SerializableType
from fprime_gds.common.models.serialize.string_type import StringType
from fprime_console_gds.command_text import CommandTextError, format_command_text, parse_command_text, relax_json
from fprime_console_gds.filtering import matches, parse_filter
from fprime_console_gds.model import SEVERITY_ORDER, ChannelRow, EventRow, TuiModel, sanitize

# Event colours are told apart with normal, protan, deutan and tritan colour vision (Okabe-Ito hues; the test suite
# checks every pair under simulation and each colour's contrast with the background). Colour is never the only cue:
# the severity is a text column, FATAL is a filled row, WARNING_HI is bold and DIAGNOSTIC is the dimmest.
SEVERITY_STYLES = {
    "FATAL": "bold #000000 on #D55E00",
    "WARNING_HI": "bold #E69F00",
    "WARNING_LO": "#F0E442",
    "ACTIVITY_HI": "#56B4E9",
    "ACTIVITY_LO": "#66FFB2",
    "COMMAND": "#E0E0E0",
    "DIAGNOSTIC": "#949494",
}
SEVERITY_SHORT_LABELS = {
    "FATAL": "F",
    "WARNING_HI": "WH",
    "WARNING_LO": "WL",
    "ACTIVITY_HI": "AH",
    "ACTIVITY_LO": "AL",
    "COMMAND": "C",
    "DIAGNOSTIC": "D",
}
# Below these terminal sizes one of telemetry/events is shown at a time and the commanding panel shrinks
NARROW_COLUMNS = 100
SHORT_ROWS = 30
# Events retained above which the text filter waits for a pause in typing before redrawing the table
FILTER_DEBOUNCE_EVENTS = 1000
FILTER_DEBOUNCE_SECONDS = 0.15
SEVERITY_LABELS = {
    "FATAL": "FATAL",
    "WARNING_HI": "WARN_HI",
    "WARNING_LO": "WARN_LO",
    "ACTIVITY_HI": "ACT_HI",
    "ACTIVITY_LO": "ACT_LO",
    "COMMAND": "CMD",
    "DIAGNOSTIC": "DIAG",
}
# Telemetry outside its limits: a filled cell in a colour-vision-safe hue, and a text mark so colour is not the only cue
LIMIT_STYLES = {"red": "bold #000000 on #D55E00", "orange": "#000000 on #E69F00", "yellow": "#000000 on #F0E442"}
LIMIT_MARKS = {"red": "!R ", "orange": "!O ", "yellow": "!Y "}
REFRESH_SECONDS = 0.2
# Above this many new events in one refresh the event table is rebuilt instead of appended to
EVENT_REBUILD_THRESHOLD = 500
# Above this many rows to drop at the retention limit the event table is rebuilt instead of trimmed row by row
EVENT_TRIM_REBUILD_THRESHOLD = 32
# Seconds between redraws of the event table while events arrive faster than they can be appended
FLOOD_REBUILD_SECONDS = 1.0
MAX_HISTORY = 100
MAX_COMMAND_MATCHES = 30  # Command names offered below the command string while it names no exact command
MAX_ERROR_LENGTH = 200
# A send key this soon after another control key (Tab, a function key, Enter) is not obeyed: typed ahead of the
# screen it could send what the field it was meant for does not show yet, and a bounced Enter would send twice
TYPE_AHEAD_SECONDS = 0.25
CONTROL_KEY = re.compile(r"(ctrl\+|shift\+)*(tab|enter|escape|up|down|pageup|pagedown|f\d+)")
ALERT_SEVERITIES = ("FATAL", "WARNING_HI")
MAX_COMMAND_LENGTH = 4096
# Seconds without data after which the link indicator turns stale
STALE_SECONDS = 10.0
FILTER_HELP = "filter: words AND, -or- for OR"

HELP_TEXT = """\
F Prime terminal UI

Panels
  F2 / F3 / F4      focus Commands / Telemetry / Events
  F6 / Shift+F6     next / previous panel (Ctrl+Tab too, where the terminal allows)
  Tab / Shift+Tab   next / previous field
  /                 focus the filter of the current panel
  Esc               leave a field (focus the panel's table)
  Enter             on a telemetry or event row: show it in full
  Ctrl+L            clear the event list
  Ctrl+Q            quit

Telemetry outside its limits is filled and marked !R (red), !O (orange) or !Y (yellow).

Filters (same as the GDS tables)
  Case-insensitive parts of the row as displayed. Words separated by spaces
  must all match; "-or-" between two words matches either.
    cmdDisp warning            rows with both words
    FATAL -or- WARNING_HI      rows with either word

Commanding
  Pick a command in the drop-down and fill in its argument fields, or type or
  paste a command string; the two stay in step:
    component.MNEMONIC, arg1, "string, with comma", [1, 2]
  Arrays and serializables are JSON; bare words are accepted as in the GDS:
    [ONE, TWO]   {first: ONE, second: 2}
  Enter, F5 or Send sends. Select a history entry to load it again.
  A send key straight after Tab, Enter or a function key sends nothing:
  press it again.
"""


def plain(value: str, style: str = "") -> Text:
    """Text cell that is never interpreted as console markup"""
    return Text(value, style=style, no_wrap=True, overflow="ellipsis")


def elide_left(value: str, width: int) -> str:
    """Value cut on the left to a width: the end of a qualified name tells channels and events apart"""
    return value if len(value) <= width else "\N{HORIZONTAL ELLIPSIS}" + value[len(value) - width + 1 :]


def all_finite(value) -> bool:
    """Whether every number inside a decoded JSON value is finite"""
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(all_finite(member) for member in value.values())
    if isinstance(value, list):
        return all(all_finite(member) for member in value)
    return True


def finite(float_type, value: float) -> bool:
    """Whether a value is a finite number that the F32 or F64 type can carry"""
    try:
        return math.isfinite(struct.unpack("<f", struct.pack("<f", value))[0] if float_type is F32Type else value)
    except OverflowError:
        return False


class DetailScreen(ModalScreen):
    """One telemetry or event row in full, wrapped"""

    BINDINGS = [Binding("escape,enter,q", "dismiss", "Close")]

    def __init__(self, text: str):
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail-box"):
            yield Static(self.text, markup=False)

    def on_click(self) -> None:
        self.dismiss()


class HelpScreen(ModalScreen):
    """Key and filter help"""

    BINDINGS = [Binding("escape,f1,q,enter", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-box"):
            yield Static(HELP_TEXT, markup=False)

    def on_click(self) -> None:
        self.dismiss()


class FprimeTui(App):
    """Terminal UI for F Prime telemetry, events, and commanding"""

    TITLE = "F Prime"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #top { height: 1fr; }
    #telemetry { width: 3fr; min-width: 30; }
    #events { width: 5fr; }
    #commands { height: 13; }
    #commands.-short { height: 9; }
    #commands.-narrow #command-select { width: 34; }
    #top.-narrow.-show-events #telemetry { display: none; }
    #top.-narrow.-show-telemetry #events { display: none; }
    #status.-stale { background: $error; color: $text; text-style: bold; }
    DetailScreen { align: center middle; }
    #detail-box { width: 90%; height: auto; max-height: 80%; border: round $accent; background: $surface;
                  padding: 1 2; }
    .panel { border: round $primary-darken-1; border-title-color: $text; padding: 0 1; }
    .panel:focus-within { border: round $accent; }
    .bar { height: 1; }
    Input { height: 1; border: none; padding: 0 1; background: $surface-lighten-1; }
    Input:focus { background: $surface-lighten-2; }
    Input.-invalid { color: $error; }
    #severity-bar { height: 1; }
    #severity-bar Checkbox { margin-right: 1; }
    DataTable { height: 1fr; }
    #command-select { width: 48; }
    #send { min-width: 10; height: 1; border: none; margin-left: 1; }
    #command-row { height: 3; }
    #arguments { height: 4; }
    .argument { width: auto; min-width: 20; max-width: 44; height: 4; margin-right: 1; }
    .argument Label { height: 1; color: $text-muted; }
    .argument Input { width: 26; }
    .argument Select { width: 30; }
    #no-arguments { color: $text-muted; }
    #command-text-row { height: 1; }
    #command-text-label { width: 9; }
    #command-matches-arrow { width: 2; color: $success; }
    #command-matches-arrow.-hidden { display: none; }
    #command-text { width: 1fr; }
    #command-error { height: auto; min-height: 1; max-height: 3; color: $error; }
    #command-matches { height: auto; max-height: 6; border: none; background: $surface; }
    #history { height: 1fr; min-height: 1; border: none; background: $surface; }
    #status { height: 1; background: $panel; color: $text; padding: 0 1; }
    HelpScreen { align: center middle; }
    #help-box { width: 92; max-width: 100%; height: auto; max-height: 100%; border: round $accent;
                background: $surface; padding: 1 2; }
    """
    BINDINGS = [
        Binding("f1", "help", "Help"),
        Binding("f2", "focus_panel('commands')", "Commands", priority=True),
        Binding("f3", "focus_panel('telemetry')", "Telemetry", priority=True),
        Binding("f4", "focus_panel('events')", "Events", priority=True),
        Binding("f5", "send", "Send"),
        Binding("f6", "next_panel", "Next panel", priority=True),
        Binding("shift+f6", "previous_panel", "Previous panel", show=False, priority=True),
        Binding("ctrl+tab", "next_panel", "Next panel", show=False, priority=True),
        Binding("ctrl+shift+tab", "previous_panel", "Previous panel", show=False, priority=True),
        Binding("slash", "focus_filter", "Filter"),
        Binding("down", "command_matches_down", "Command matches", show=False),
        Binding("escape", "leave_field", "Leave field", show=False),
        Binding("ctrl+l", "clear_events", "Clear events"),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]
    PANELS = ("commands", "telemetry", "events")
    PANEL_FOCUS = {"commands": "#command-text", "telemetry": "#telemetry-table", "events": "#events-table"}
    PANEL_FILTER = {"telemetry": "#telemetry-filter", "events": "#events-filter"}

    def __init__(
        self,
        model: TuiModel,
        command_templates: Dict[str, object],
        send_command: Callable[[str, Sequence[str]], None],
        transport_name: str = "",
        dictionary_name: str = "",
        max_events: int = 5000,
    ):
        """Create the application

        Args:
            model: store fed by the GDS pipeline
            command_templates: command templates by full command name (the pipeline's command dictionary)
            send_command: function sending a command by name with string arguments; raises on failure
            transport_name: name of the data transport plugin in use, for the status line
            dictionary_name: name of the loaded dictionary, for the status line
            max_events: number of events displayed at most
        """
        super().__init__()
        self.model = model
        self.command_templates = command_templates
        self.send_command = send_command
        self.transport_name = sanitize(transport_name)
        self.dictionary_name = sanitize(dictionary_name)
        self.max_events = max_events
        self.channel_filter: List[List[str]] = []
        self.event_filter: List[List[str]] = []
        self.severities = set(SEVERITY_ORDER)
        self.shown_channels: Dict[int, ChannelRow] = {}
        self.shown_events: Deque[int] = deque()
        self.last_event_sequence = 0
        self.current_command: Optional[str] = None
        self.argument_widgets: List[object] = []
        self.history: Deque[Tuple[str, Text]] = deque(maxlen=MAX_HISTORY)  # Newest first
        self.last_data_time: Optional[float] = None
        self.sent_count = 0
        self.send_queue: Deque[tuple] = deque()  # Validated commands waiting for the send in flight
        self.sending = False
        self.argument_options: Dict[str, List[str]] = {}  # Choices of each drop-down argument, by widget id
        # Lower-case command names that identify one command, so that a name may be typed in any case
        lowered: Dict[str, List[str]] = {}
        for name in command_templates:
            lowered.setdefault(name.lower(), []).append(name)
        self.command_lookup = {lower: names[0] for lower, names in lowered.items() if len(names) == 1}
        self.column_widths: Dict[str, Tuple[int, ...]] = {}
        self.narrow_panel = "events"  # Which of telemetry/events is shown when there is no room for both
        self.filter_timer = None
        self.form_lock = asyncio.Lock()  # One change of the argument form at a time
        self.last_key_time = 0.0
        self.last_key_moved = False
        self.last_key: Optional[events.Key] = None
        self.moved_before_last_key = False  # The key before the last one moved between fields, moments earlier
        self.next_flood_rebuild = 0.0
        self.events_behind = False  # Events were received that the table does not show yet
        self.unseen_alerts = 0  # FATAL/WARNING_HI events received while the events panel was not on screen

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            with Vertical(id="telemetry", classes="panel"):
                yield Input(placeholder=FILTER_HELP, id="telemetry-filter", select_on_focus=False)
                yield DataTable(id="telemetry-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="events", classes="panel"):
                yield Input(placeholder=FILTER_HELP, id="events-filter", select_on_focus=False)
                with Horizontal(id="severity-bar"):
                    for severity in SEVERITY_ORDER:
                        yield Checkbox(
                            SEVERITY_LABELS[severity], value=True, compact=True, id=f"severity-{severity}"
                        )
                yield DataTable(id="events-table", cursor_type="row")
        with Vertical(id="commands", classes="panel"):
            with Horizontal(id="command-row"):
                yield Select(
                    [(name, name) for name in sorted(self.command_templates)],
                    prompt="Select command",
                    id="command-select",
                    compact=True,
                )
                arguments = HorizontalScroll(id="arguments")
                arguments.can_focus = False  # Not a tab stop: the fields scroll into view when focused
                yield arguments
            with Horizontal(id="command-text-row"):
                yield Label("command>", id="command-text-label")
                yield Static("\N{BLACK DOWN-POINTING TRIANGLE}", id="command-matches-arrow", classes="-hidden")
                yield Input(
                    placeholder='component.MNEMONIC, arg1, "arg 2"   (Enter sends)',
                    id="command-text",
                    max_length=MAX_COMMAND_LENGTH,
                    suggester=SuggestFromList(sorted(self.command_templates), case_sensitive=False),
                    select_on_focus=False,
                )
                yield Button("Send", id="send", variant="primary", compact=True)
            yield Static("", id="command-error", markup=False)
            yield OptionList(id="command-matches", markup=False, compact=True)
            yield OptionList(id="history", markup=False, compact=True)
        yield Static("", id="status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#telemetry").border_title = "Telemetry (F3)"
        self.query_one("#events").border_title = "Events (F4)"
        self.query_one("#commands").border_title = "Commanding (F2)"
        self.query_one("#command-text", Input).focus()
        self.layout()
        # A terminating signal leaves through the normal exit, which restores the terminal and disconnects
        for number in (signal.SIGTERM, signal.SIGHUP):
            try:
                asyncio.get_running_loop().add_signal_handler(number, self.exit, None, 128 + number)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Not available on this platform or outside the main thread
        self.set_interval(REFRESH_SECONDS, self.refresh_data)
        self.update_status()

    async def on_event(self, event: events.Event) -> None:
        if isinstance(event, events.Key) and event is not self.last_key:  # A key comes by again as it bubbles up
            self.last_key = event
            now = time.monotonic()
            moved = self.last_key_moved and now - self.last_key_time < TYPE_AHEAD_SECONDS
            self.moved_before_last_key = moved
            self.last_key_moved = CONTROL_KEY.fullmatch(event.key) is not None
            self.last_key_time = now
        await super().on_event(event)

    # ---- Layout ----

    def on_resize(self) -> None:
        self.layout()

    def layout(self) -> None:
        """Fit the panels and the table columns to the terminal size"""
        narrow = self.size.width < NARROW_COLUMNS
        top = self.query_one("#top")
        top.set_class(narrow, "-narrow")
        top.set_class(self.narrow_panel == "events", "-show-events")
        top.set_class(self.narrow_panel == "telemetry", "-show-telemetry")
        commands = self.query_one("#commands")
        commands.set_class(self.size.height < SHORT_ROWS, "-short")
        commands.set_class(narrow, "-narrow")
        self.call_after_refresh(self.layout_tables)

    def layout_tables(self) -> None:
        """Size the table columns to their panels: every column is visible and the widest content is cut, not scrolled"""
        if not self.is_running or not self.query("#telemetry-table"):
            return
        telemetry = self.query_one("#telemetry-table", DataTable)
        events = self.query_one("#events-table", DataTable)
        # Each column takes its width plus one cell of padding on both sides; two cells are kept for the scrollbar
        width = max(0, telemetry.size.width - 2)
        if width:
            value = 14 if width < 60 else 20
            show_time = width >= 60
            name = max(12, width - (value + 2) - (14 if show_time else 0) - 2)
            widths = (name, value, 12 if show_time else 0)
            if self.column_widths.get("telemetry") != widths:
                self.column_widths["telemetry"] = widths
                telemetry.clear(columns=True)
                telemetry.add_column("Channel", key="name", width=name)
                telemetry.add_column("Value", key="value", width=value)
                if show_time:
                    telemetry.add_column("Time", key="time", width=12)
                self.rebuild_channels()
        width = max(0, events.size.width - 2)
        if width:
            show_time = width >= 50
            fixed = (11 + 2) + (12 + 2 if show_time else 0)
            name = min(44, max(10, (width - fixed) * 2 // 5))
            message = max(4, width - fixed - (name + 2) - 2)
            widths = (name, message, show_time)
            if self.column_widths.get("events") != widths:
                self.column_widths["events"] = widths
                events.clear(columns=True)
                if show_time:
                    events.add_column("Time", key="time", width=12)
                events.add_column("Severity", key="severity", width=11)
                events.add_column("Event", key="name", width=name)
                events.add_column("Message", key="message", width=message)
                self.rebuild_events()
            labels = SEVERITY_SHORT_LABELS if width < 84 else SEVERITY_LABELS
            for severity in SEVERITY_ORDER:
                self.query_one(f"#severity-{severity}", Checkbox).label = labels[severity]

    def events_hidden(self) -> bool:
        """Whether the events panel is off screen: a narrow terminal showing telemetry instead"""
        return self.size.width < NARROW_COLUMNS and self.narrow_panel == "telemetry"

    def alive(self) -> bool:
        """Whether the screen is up: messages still queued while shutting down find no widgets to act on"""
        return self.is_running and bool(self.query("#status"))

    def has_columns(self, table: DataTable) -> bool:
        """Whether a table has been laid out, which waits for the first refresh"""
        return bool(table.columns)

    @on(DataTable.RowSelected, "#events-table")
    def event_row_selected(self, event: DataTable.RowSelected) -> None:
        if not self.alive():
            return
        for row in self.model.events():
            if str(row.sequence) == event.row_key.value:
                self.push_screen(DetailScreen(f"{row.time}  {row.severity}\n{row.name}\n\n{row.message}"))
                return

    @on(DataTable.RowSelected, "#telemetry-table")
    def channel_row_selected(self, event: DataTable.RowSelected) -> None:
        if not self.alive():
            return
        row = self.shown_channels.get(int(event.row_key.value))
        if row is not None:
            limit = f"\nlimit: {row.limit}" if row.limit else ""
            self.push_screen(DetailScreen(f"{row.name}\n{row.time}{limit}\n\n{row.value}"))

    # ---- Data refresh ----

    def refresh_data(self) -> None:
        """Move data received since the last refresh from the model into the tables"""
        if not self.alive():
            return
        self.layout_tables()  # A panel that changed size since the last refresh gets its columns refitted
        channels, events = self.model.drain()
        if channels or events:
            self.last_data_time = time.monotonic()
        if self.events_hidden():
            self.unseen_alerts += sum(1 for row in events if row.severity in ALERT_SEVERITIES)
        else:
            self.unseen_alerts = 0
        if channels:
            self.update_channels(channels)
        if len(events) > EVENT_REBUILD_THRESHOLD or (events and self.events_behind):
            # A flood is drawn at most once a second so that keys are still answered between redraws
            self.events_behind = True
        elif events:
            self.append_events(events)
        if self.events_behind and time.monotonic() >= self.next_flood_rebuild:
            self.rebuild_events()
        self.update_status()

    def channel_cells(self, row: ChannelRow):
        """Table cells of a channel row"""
        name_width, _, time_width = self.column_widths["telemetry"]
        cells = (plain(elide_left(row.name, name_width)), plain(LIMIT_MARKS.get(row.limit, "") + row.value, LIMIT_STYLES.get(row.limit, "")))
        return cells + ((plain(row.time),) if time_width else ())

    def update_channels(self, rows: List[ChannelRow]) -> None:
        """Update displayed channels in place; a channel seen for the first time re-sorts the table"""
        table = self.query_one("#telemetry-table", DataTable)
        if not self.has_columns(table):
            return
        matching = [row for row in rows if matches(self.channel_filter, row.text)]
        if any(row.channel_id not in self.shown_channels for row in matching):
            self.rebuild_channels()
            return
        for row in rows:
            if row.channel_id not in self.shown_channels:
                continue
            key = str(row.channel_id)
            if not matches(self.channel_filter, row.text):
                # The new reading no longer passes the filter (e.g. a filter on the value)
                table.remove_row(key)
                del self.shown_channels[row.channel_id]
                continue
            cells = self.channel_cells(row)
            table.update_cell(key, "value", cells[1])
            if len(cells) > 2:
                table.update_cell(key, "time", cells[2])
            self.shown_channels[row.channel_id] = row

    def rebuild_channels(self) -> None:
        """Redraw the telemetry table from the model with the current filter, keeping the cursor on its channel"""
        table = self.query_one("#telemetry-table", DataTable)
        if not self.has_columns(table):
            return
        selected = None
        if table.row_count and table.is_valid_row_index(table.cursor_row):
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear()
        self.shown_channels = {}
        for row in self.model.channels():
            if matches(self.channel_filter, row.text):
                table.add_row(*self.channel_cells(row), key=str(row.channel_id))
                self.shown_channels[row.channel_id] = row
        if selected is not None and int(selected) in self.shown_channels:
            table.move_cursor(row=table.get_row_index(selected), animate=False)

    def event_visible(self, row: EventRow) -> bool:
        """Whether an event passes the severity toggles and the text filter"""
        known = row.severity in SEVERITY_ORDER
        return (row.severity in self.severities or not known) and matches(self.event_filter, row.text)

    def add_event_row(self, table: DataTable, row: EventRow) -> None:
        """Append one event to the event table"""
        style = SEVERITY_STYLES.get(row.severity, "")
        cells = (plain(row.time, style),) if self.column_widths["events"][2] else ()
        table.add_row(
            *cells,
            plain(row.severity, style),
            plain(elide_left(row.name, self.column_widths["events"][0]), style),
            plain(row.message, style),
            key=str(row.sequence),
        )
        self.shown_events.append(row.sequence)

    def following(self, table: DataTable) -> bool:
        """Whether the event table shows its newest rows, so new events should keep scrolling into view"""
        return table.row_count == 0 or table.cursor_row >= table.row_count - 1

    def append_events(self, rows: List[EventRow]) -> None:
        """Append new events, drop events beyond the retention limit, and follow the tail when already there"""
        table = self.query_one("#events-table", DataTable)
        if not self.has_columns(table):
            return
        follow = self.following(table)
        for row in rows:
            if row.sequence <= self.last_event_sequence:
                continue  # Already shown by a rebuild from the model
            self.last_event_sequence = row.sequence
            if self.event_visible(row):
                self.add_event_row(table, row)
        oldest_retained = self.last_event_sequence - self.max_events
        expired = sum(1 for sequence in self.shown_events if sequence <= oldest_retained)
        if max(expired, len(self.shown_events) - self.max_events) > EVENT_TRIM_REBUILD_THRESHOLD:
            # Removing rows one by one costs more than redrawing the table
            self.events_behind = True
            if time.monotonic() >= self.next_flood_rebuild:
                self.rebuild_events()
            return
        while self.shown_events and (
            self.shown_events[0] <= oldest_retained or len(self.shown_events) > self.max_events
        ):
            table.remove_row(str(self.shown_events.popleft()))
        if follow and table.row_count:
            self.show_newest(table)

    def rebuild_events(self) -> None:
        """Redraw the event table from the model with the current filters"""
        table = self.query_one("#events-table", DataTable)
        if not self.has_columns(table):
            return
        follow = self.following(table)
        selected = None
        if not follow and table.is_valid_row_index(table.cursor_row):
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear()
        self.shown_events.clear()
        if self.events_behind:
            self.events_behind = False
            self.next_flood_rebuild = time.monotonic() + FLOOD_REBUILD_SECONDS
        for row in self.model.events()[-self.max_events :]:
            self.last_event_sequence = row.sequence
            if self.event_visible(row):
                self.add_event_row(table, row)
        if not table.row_count:
            return
        if selected is not None and int(selected) in self.shown_events:
            table.move_cursor(row=table.get_row_index(selected), animate=False)
        else:
            self.show_newest(table)

    def show_newest(self, table: DataTable) -> None:
        """Put the cursor on the newest event and bring it into view"""
        table.move_cursor(row=table.row_count - 1, animate=False)
        table.scroll_end(animate=False, x_axis=False)

    def on_descendant_focus(self) -> None:
        self.show_table_cursors()

    def on_descendant_blur(self) -> None:
        self.show_table_cursors()

    def show_table_cursors(self) -> None:
        """Show the row cursor only in the table being used: elsewhere it would cover a row's severity or limit colour"""
        for table in self.query(DataTable):
            table.show_cursor = table.has_focus

    def update_status(self) -> None:
        """Refresh the status line and panel titles"""
        stale = False
        if self.last_data_time is None:
            link = "no data yet"
        else:
            age = time.monotonic() - self.last_data_time
            stale = age >= STALE_SECONDS
            link = f"NO DATA for {int(age)}s" if stale else "receiving"
        parts = [f"transport: {self.transport_name or '?'}", link]
        if self.dictionary_name:
            parts.append(f"dictionary: {self.dictionary_name}")
        parts.append(f"sent: {self.sent_count}")
        waiting = len(self.send_queue) + (1 if self.sending else 0)
        if waiting:
            parts.append(f"SENDING: {waiting}")
        dropped = self.model.dropped
        if dropped:
            parts.append(f"undisplayable: {dropped}")
        status = self.query_one("#status", Static)
        if self.unseen_alerts:
            parts.insert(0, f"{self.unseen_alerts} FATAL/WARNING_HI EVENT(S): F4")
        status.update(" | ".join(parts))
        status.set_class(stale or bool(self.unseen_alerts), "-stale")
        total_channels = len(self.model.channels()) if self.channel_filter else len(self.shown_channels)
        self.query_one("#telemetry").border_subtitle = (
            f"{len(self.shown_channels)}/{total_channels} channels"
            if self.channel_filter
            else f"{len(self.shown_channels)} channels"
        )
        filtered = bool(self.event_filter) or len(self.severities) != len(SEVERITY_ORDER)
        self.query_one("#events").border_subtitle = (
            f"{len(self.shown_events)} of {self.model.event_count()} shown (filtered)" if filtered else f"{len(self.shown_events)} events"
        )

    # ---- Filters ----

    @on(Input.Changed, "#telemetry-filter")
    def telemetry_filter_changed(self, event: Input.Changed) -> None:
        if not self.alive():
            return
        self.channel_filter = parse_filter(event.value)
        self.rebuild_channels()
        self.update_status()

    @on(Input.Changed, "#events-filter")
    def events_filter_changed(self, event: Input.Changed) -> None:
        if not self.alive():
            return
        self.event_filter = parse_filter(event.value)
        if self.filter_timer is not None:
            self.filter_timer.stop()
            self.filter_timer = None
        if self.model.event_count() > FILTER_DEBOUNCE_EVENTS:
            # Redrawing a long event list on every keystroke would make typing lag
            self.filter_timer = self.set_timer(FILTER_DEBOUNCE_SECONDS, self.apply_event_filter)
        else:
            self.apply_event_filter()

    def apply_event_filter(self) -> None:
        """Redraw the event table for the current filters"""
        self.filter_timer = None
        self.rebuild_events()
        self.update_status()

    @on(Checkbox.Changed, "#severity-bar Checkbox")
    def severity_changed(self, event: Checkbox.Changed) -> None:
        if not self.alive():
            return
        severity = event.checkbox.id.split("-", 1)[1]
        if event.value:
            self.severities.add(severity)
        else:
            self.severities.discard(severity)
        self.rebuild_events()
        self.update_status()

    # ---- Commanding ----

    def argument_values(self) -> List[str]:
        """Current values of the argument form as strings"""
        values = []
        for widget in self.argument_widgets:
            if isinstance(widget, Select):
                values.append("" if widget.is_blank() else str(widget.value))
            else:
                values.append(widget.value)
        return values

    def string_flags(self, name: str) -> List[bool]:
        """Per-argument flags of a command: True for string-typed arguments"""
        return [issubclass(arg_type, StringType) for _, _, arg_type in self.command_templates[name].get_args()]

    async def show_command(self, name: Optional[str], values: Sequence[str] = ()) -> None:
        """Show the argument form of a command, optionally filled in; changes of the form are made one at a time

        Args:
            name: full command name, None to clear the form
            values: argument values as strings; missing arguments are left empty
        """
        async with self.form_lock:
            if self.is_running and self.query("#arguments"):
                await self.build_form(name, values)

    async def build_form(self, name: Optional[str], values: Sequence[str]) -> None:
        """Show the argument form of a command, optionally filled in

        Args:
            name: full command name, None to clear the form
            values: argument values as strings; missing arguments are left empty
        """
        container = self.query_one("#arguments", HorizontalScroll)
        if name != self.current_command:
            await container.remove_children()
            self.argument_widgets = []
            self.argument_options = {}
            self.current_command = name
            if name is not None:
                arguments = self.command_templates[name].get_args()
                boxes = []
                for index, (arg_name, _, arg_type) in enumerate(arguments):
                    label = Label(sanitize(f"{arg_name}: {arg_type.__name__}"), markup=False)
                    if issubclass(arg_type, EnumType):
                        self.argument_options[f"argument-{index}"] = list(arg_type.keys())
                        widget = Select(
                            [(sanitize(key), key) for key in arg_type.keys()],
                            prompt="Select",
                            id=f"argument-{index}",
                            compact=True,
                        )
                    elif issubclass(arg_type, BoolType):
                        self.argument_options[f"argument-{index}"] = ["True", "False"]
                        widget = Select(
                            [("True", "True"), ("False", "False")], prompt="Select", id=f"argument-{index}", compact=True
                        )
                    else:
                        widget = Input(id=f"argument-{index}", select_on_focus=False)
                    self.argument_widgets.append(widget)
                    boxes.append(Vertical(label, widget, classes="argument"))
                if not boxes:
                    boxes.append(Static("(no arguments)", id="no-arguments"))
                await container.mount_all(boxes)
        for widget, value in zip(self.argument_widgets, list(values) + [""] * len(self.argument_widgets)):
            if isinstance(widget, Select):
                options = self.argument_options[widget.id]
                normalized = value.strip()
                if normalized.lower() in ("true", "yes", "false", "no") and "True" in options:
                    normalized = "True" if normalized.lower() in ("true", "yes") else "False"
                if normalized in options:
                    widget.value = normalized
                else:
                    widget.clear()
            elif widget.value != value:
                widget.value = value
        select = self.query_one("#command-select", Select)
        if name is None:
            if not select.is_blank():
                select.clear()
        elif select.value != name:
            select.value = name

    def form_focused(self) -> bool:
        """Whether the focused widget belongs to the command form (drop-down or an argument)"""
        try:
            focused = self.focused
        except ScreenStackError:  # Shutting down
            return False
        if focused is None:
            return False
        return focused.id == "command-select" or any(
            ancestor.id in ("arguments", "command-select") for ancestor in focused.ancestors
        )

    def set_command_error(self, message: str) -> None:
        """Show (or with an empty message clear) the line under the command string"""
        if not self.alive():
            return
        line = self.query_one("#command-error", Static)
        line.update(sanitize(message))

    def set_command_matches(self, names: Sequence[str]) -> None:
        """Offer commands whose name contains the typed text, to select instead of typing the exact name"""
        if not self.alive():
            return
        matches = self.query_one("#command-matches", OptionList)
        matches.clear_options()
        if names:
            matches.add_options([Option(name) for name in names[:MAX_COMMAND_MATCHES]])
            matches.highlighted = 0
        self.query_one("#command-matches-arrow").set_class(not names, "-hidden")

    def form_to_text(self) -> None:
        """Write the command string for the current state of the form"""
        if self.current_command is None or not self.alive():
            return
        text = format_command_text(
            self.current_command, self.argument_values(), self.string_flags(self.current_command)
        )
        command_text = self.query_one("#command-text", Input)
        if command_text.value != text:
            command_text.value = text
            command_text.cursor_position = len(text)
        self.set_command_error("")

    @on(Select.Changed, "#command-select")
    async def command_selected(self, event: Select.Changed) -> None:
        if not self.alive():
            return
        if not self.form_focused():
            return  # Set from the command string or history, which already updated the form
        name = None if event.select.is_blank() else str(event.value)
        if name == self.current_command:
            return
        await self.show_command(name)
        if name is None:
            self.query_one("#command-text", Input).value = ""
        self.form_to_text()

    @on(Select.Changed, "#arguments Select")
    def argument_selected(self, event: Select.Changed) -> None:
        if not self.alive():
            return
        if self.form_focused():
            self.form_to_text()

    @on(Input.Changed, "#arguments Input")
    def argument_changed(self, event: Input.Changed) -> None:
        if not self.alive():
            return
        if self.form_focused():
            self.form_to_text()

    @on(Input.Changed, "#command-text")
    async def command_text_changed(self, event: Input.Changed) -> None:
        if not self.alive():
            return
        if self.form_focused():
            return  # Written by the form
        await self.text_to_form(event.value)

    async def text_to_form(self, text: str) -> None:
        """Bring the form in step with the command string, as far as the string is understood"""
        self.set_command_error("")
        if not text.strip():
            await self.show_command(None)
            self.set_command_matches([])
            return
        try:
            name, values = parse_command_text(text)
        except CommandTextError:
            try:
                name, values = parse_command_text(text + '"')  # A string still being typed
            except CommandTextError:
                # Not understood (reported on send): no form is shown rather than one for another command
                await self.show_command(None)
                self.set_command_matches([])
                return
        name = self.command_lookup.get(name.lower(), name)
        if name in self.command_templates:
            await self.show_command(name, values)
            self.set_command_matches([])
        else:
            await self.show_command(None)  # No form for a command the dictionary does not have
            # Any part of a name finds the command, as in the GDS command selector
            found = [known for known in sorted(self.command_templates) if name.lower() in known.lower()]
            self.set_command_matches(found)

    @on(Input.Submitted, "#command-text")
    @on(Input.Submitted, "#arguments Input")
    @on(Button.Pressed, "#send")
    async def send_requested(self, event) -> None:
        if not self.alive():
            return
        event.stop()
        if not isinstance(event, Button.Pressed) and self.moved_before_last_key:
            self.set_command_error("Not sent: the send key came straight after another control key; press it again")
            return
        await self.request_send()

    async def action_send(self) -> None:
        """Send on the send key, which acts in the commanding panel only and never under a help or detail screen"""
        if isinstance(self.screen, ModalScreen) or self.current_panel() != "commands":
            return
        if self.moved_before_last_key:
            self.set_command_error("Not sent: the send key came straight after another control key; press it again")
            return
        await self.request_send()

    async def request_send(self) -> None:
        """Send the command string, unless the form shows another command than the string names

        Keys typed ahead of the screen can ask for a send before a change of the form has reached the command string;
        what goes out must be what both show.
        """
        if self.form_focused():
            self.form_to_text()  # The form is being edited: its present state is what is sent
        text = self.query_one("#command-text", Input).value
        select = self.query_one("#command-select", Select)
        try:
            name = parse_command_text(text)[0]
            name = self.command_lookup.get(name.lower(), name)
        except CommandTextError:
            name = None
        selected = None if select.is_blank() else str(select.value)
        if name in self.command_templates and (selected != name or self.current_command != name):
            await self.text_to_form(text)
            self.set_command_error(
                "Not sent: the command string and the command form did not match; they now do, check and send again"
            )
            return
        self.send_text(text)

    def send_text(self, text: str) -> None:
        """Validate the command string and send it"""
        try:
            name, values = parse_command_text(text)
        except CommandTextError as error:
            self.set_command_error(f"Not sent: {error}")
            return
        name = self.command_lookup.get(name.lower(), name)
        template = self.command_templates.get(name)
        if template is None:
            close = [known for known in sorted(self.command_templates) if name.lower() in known.lower()]
            close = close or difflib.get_close_matches(name, list(self.command_templates), n=3)
            hint = f" (did you mean {', '.join(close[:3])}?)" if close else ""
            self.set_command_error(f"Not sent: unknown command '{name}'{hint}")
            return
        arguments = template.get_args()
        if len(values) != len(arguments):
            expected = ", ".join(arg_name for arg_name, _, _ in arguments) or "none"
            self.set_command_error(
                f"Not sent: {name} takes {len(arguments)} argument(s) ({expected}), {len(values)} given"
            )
            return
        values = [
            relax_json(value) if issubclass(arg_type, (ArrayType, SerializableType)) else value
            for (_, _, arg_type), value in zip(arguments, values)
        ]
        try:
            CmdData(tuple(values), template)
        except CommandArgumentsException as error:
            problems = "; ".join(message for message in error.errors if message)
            self.set_command_error(f"Not sent: {problems}")
            return
        for (arg_name, _, arg_type), value in zip(arguments, values):
            if issubclass(arg_type, (F32Type, F64Type)) and not finite(arg_type, float(value)):
                self.set_command_error(f"Not sent: {arg_name} must be a finite number that fits {arg_type.__name__}")
                return
            if issubclass(arg_type, (ArrayType, SerializableType)) and not all_finite(json.loads(value)):
                self.set_command_error(f"Not sent: {arg_name} holds a number that is not finite")
                return
        self.set_command_error("")
        canonical = format_command_text(name, values, self.string_flags(name))
        self.send_queue.append((name, values, canonical))
        # Typing or pasting the next command replaces the one just sent
        self.query_one("#command-text", Input).select_all()
        self.send_next()

    def send_next(self) -> None:
        """Start sending the oldest queued command unless a send is in flight: commands go out one at a time, in order"""
        if self.sending or not self.send_queue:
            self.update_status()
            return
        self.sending = True
        self.dispatch_command(*self.send_queue.popleft())
        self.update_status()

    def dispatch_command(self, name: str, values: List[str], canonical: str) -> None:
        """Send on a thread of its own: a transport may block on the network, and must not hold up quitting"""

        def send() -> None:
            error = None
            try:
                self.send_command(name, values)
            except Exception as failure:  # Any transport failure is reported, never raised into the UI
                error = f"{type(failure).__name__}: {failure}"
            try:
                self.call_from_thread(self.command_finished, canonical, error)
            except RuntimeError:
                pass  # The application closed while the command was being sent

        threading.Thread(target=send, name="fprime-console-send", daemon=True).start()

    def command_finished(self, canonical: str, error: Optional[str]) -> None:
        """Record the outcome of a send in the history"""
        if not self.alive():
            return
        stamp = time.strftime("%H:%M:%S")
        history = self.query_one("#history", OptionList)
        if error is None:
            self.sent_count += 1
            prompt = Text(f"{stamp} sent    {canonical}", no_wrap=True)
        else:
            error = sanitize(error)[:MAX_ERROR_LENGTH]
            prompt = Text(f"{stamp} FAILED  {canonical}  ({error})", style="bold red", no_wrap=True)
            dropped = len(self.send_queue)
            self.send_queue.clear()  # What was entered after it assumed it would be sent
            waiting = f"; {dropped} command(s) waiting behind it were not sent" if dropped else ""
            self.set_command_error(f"Send failed: {error}{waiting}")
        self.history.appendleft((canonical, prompt))
        history.clear_options()
        history.add_options([Option(item) for _, item in self.history])
        self.sending = False
        self.send_next()

    @on(OptionList.OptionSelected, "#history")
    async def history_selected(self, event: OptionList.OptionSelected) -> None:
        if not self.alive():
            return
        text = self.history[event.option_index][0]
        command_text = self.query_one("#command-text", Input)
        command_text.focus()
        command_text.value = text
        command_text.cursor_position = len(text)
        await self.text_to_form(text)

    @on(OptionList.OptionSelected, "#command-matches")
    async def command_match_selected(self, event: OptionList.OptionSelected) -> None:
        if not self.alive():
            return
        name = str(event.option.prompt)
        command_text = self.query_one("#command-text", Input)
        command_text.focus()
        command_text.value = name
        command_text.cursor_position = len(name)
        await self.text_to_form(name)

    # ---- Keys ----

    def current_panel(self) -> str:
        """Name of the panel holding the focus (commands when nothing is focused)"""
        focused = self.focused
        if focused is not None:
            for node in (focused, *focused.ancestors):
                if node.id in self.PANELS:
                    return node.id
        return "commands"

    def action_focus_panel(self, panel: str) -> None:
        if panel != "commands":
            self.narrow_panel = panel
            self.layout()
        self.query_one(self.PANEL_FOCUS[panel]).focus()


    def action_next_panel(self) -> None:
        index = self.PANELS.index(self.current_panel())
        self.action_focus_panel(self.PANELS[(index + 1) % len(self.PANELS)])

    def action_previous_panel(self) -> None:
        index = self.PANELS.index(self.current_panel())
        self.action_focus_panel(self.PANELS[(index - 1) % len(self.PANELS)])

    def action_focus_filter(self) -> None:
        panel = self.current_panel()
        if panel in self.PANEL_FILTER:
            self.query_one(self.PANEL_FILTER[panel]).focus()

    def action_leave_field(self) -> None:
        self.action_focus_panel(self.current_panel())

    def action_command_matches_down(self) -> None:
        """From the command string, the down arrow drops into the list of matching commands, if any are shown"""
        focused = self.focused
        if focused is None or focused.id != "command-text":
            return
        matches = self.query_one("#command-matches", OptionList)
        if matches.option_count:
            matches.focus()

    def action_clear_events(self) -> None:
        self.model.clear_events()
        self.rebuild_events()
        self.update_status()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
