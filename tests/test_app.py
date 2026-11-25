"""Headless tests of the terminal UI driven through Textual's pilot"""
import asyncio
import os
import signal
import threading
import time

import pytest

pytest.importorskip("textual")

from textual.widgets import Checkbox, DataTable, Input, OptionList, Select, Static  # noqa: E402

import fprime_console_gds.app as app_module  # noqa: E402
from fprime_console_gds.app import LIMIT_STYLES, SEVERITY_STYLES, TYPE_AHEAD_SECONDS, DetailScreen, FprimeTui, HelpScreen  # noqa: E402
from fprime_console_gds.model import TuiModel  # noqa: E402

from .helpers import make_channel, make_commands, make_event  # noqa: E402


class Sender:
    """Stand-in for the pipeline's send_command"""

    def __init__(self):
        self.sent = []
        self.error = None
        self.thread_names = []
        self.delays = {}  # Seconds to block, by command name

    def __call__(self, name, arguments):
        self.thread_names.append(threading.current_thread().name)
        time.sleep(self.delays.get(name, 0))
        if self.error is not None:
            raise self.error
        self.sent.append((name, list(arguments)))


def run(scenario, max_events=5000, size=(160, 48)):
    """Run an async scenario(app, pilot, model, sender) against a headless app"""

    async def runner():
        model = TuiModel(max_events=max_events)
        sender = Sender()
        app = FprimeTui(model, make_commands(), sender, "yamcs", "Dictionary.json", max_events=max_events)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await scenario(app, pilot, model, sender)

    asyncio.run(runner())


async def refresh(app, pilot):
    """Move model data into the tables now instead of waiting for the timer"""
    app.refresh_data()
    await pilot.pause()


async def settle(app, pilot):
    """Wait for send workers and the messages they post"""
    for _ in range(200):
        await pilot.pause(0.01)
        if not app.sending and not app.send_queue:
            return
    raise AssertionError("sends did not finish")


def column(table, key):
    """Plain text of one column of a table, top to bottom"""
    return [table.get_cell(row_key, key).plain for row_key in table.rows]


def text_of(app, selector):
    """Plain text of a Static"""
    return str(app.query_one(selector, Static).render())


async def type_text(pilot, text):
    """Type text into the focused widget"""
    await pilot.press(*["space" if char == " " else char for char in text])
    await pilot.pause()


# ---- Telemetry ----


def test_telemetry_shows_latest_values_sorted():
    async def scenario(app, pilot, model, sender):
        model.data_callback(make_channel(1, "Zeta", 1))
        model.data_callback(make_channel(2, "Alpha", 2))
        await refresh(app, pilot)
        table = app.query_one("#telemetry-table", DataTable)
        assert column(table, "name") == ["comp.Alpha", "comp.Zeta"]
        model.data_callback(make_channel(1, "Zeta", 42, seconds=2000))
        await refresh(app, pilot)
        assert column(table, "value") == ["2", "42"]
        assert table.row_count == 2

    run(scenario)


def test_telemetry_filter():
    async def scenario(app, pilot, model, sender):
        for index, name in enumerate(["cpu0", "cpu1", "memory", "disk"]):
            model.data_callback(make_channel(index, name, index))
        await refresh(app, pilot)
        table = app.query_one("#telemetry-table", DataTable)
        await pilot.press("f3", "slash")
        assert app.focused.id == "telemetry-filter"
        await type_text(pilot, "CPU")
        assert column(table, "name") == ["comp.cpu0", "comp.cpu1"]
        await type_text(pilot, " -or- disk")
        assert column(table, "name") == ["comp.cpu0", "comp.cpu1", "comp.disk"]
        # A channel first seen while filtered appears only when it matches
        model.data_callback(make_channel(10, "cpu2", 9))
        model.data_callback(make_channel(11, "other", 9))
        await refresh(app, pilot)
        assert column(table, "name") == ["comp.cpu0", "comp.cpu1", "comp.cpu2", "comp.disk"]
        assert "4/6" in str(app.query_one("#telemetry").border_subtitle)
        app.query_one("#telemetry-filter", Input).value = ""
        await pilot.pause()
        assert table.row_count == 6

    run(scenario)


def test_telemetry_filter_on_value_follows_updates():
    async def scenario(app, pilot, model, sender):
        model.data_callback(make_channel(1, "mode", 111))
        await refresh(app, pilot)
        app.query_one("#telemetry-filter", Input).value = "111"
        await pilot.pause()
        table = app.query_one("#telemetry-table", DataTable)
        assert table.row_count == 1
        model.data_callback(make_channel(1, "mode", 222))
        await refresh(app, pilot)
        assert table.row_count == 0
        model.data_callback(make_channel(1, "mode", 111))
        await refresh(app, pilot)
        assert table.row_count == 1

    run(scenario)


# ---- Events ----


def test_events_listed_in_order_and_coloured_by_severity():
    async def scenario(app, pilot, model, sender):
        for index, severity in enumerate(SEVERITY_STYLES):
            model.data_callback(make_event(index, f"E{index}", severity, f"message {index}"))
        await refresh(app, pilot)
        table = app.query_one("#events-table", DataTable)
        assert column(table, "severity") == list(SEVERITY_STYLES)
        assert column(table, "message") == [f"message {index}" for index in range(len(SEVERITY_STYLES))]
        for row_key, severity in zip(table.rows, SEVERITY_STYLES):
            assert str(table.get_cell(row_key, "message").style) == SEVERITY_STYLES[severity]
        assert len(set(SEVERITY_STYLES.values())) == len(SEVERITY_STYLES)

    run(scenario)


def test_event_text_filter_and_severity_toggles():
    async def scenario(app, pilot, model, sender):
        model.data_callback(make_event(1, "Started", "ACTIVITY_HI", "engine started"))
        model.data_callback(make_event(2, "Hot", "WARNING_HI", "engine hot"))
        model.data_callback(make_event(3, "Ping", "DIAGNOSTIC", "ping"))
        await refresh(app, pilot)
        table = app.query_one("#events-table", DataTable)
        await pilot.press("f4", "slash")
        assert app.focused.id == "events-filter"
        await type_text(pilot, "ENGINE")
        assert column(table, "name") == ["comp.Started", "comp.Hot"]
        app.query_one("#severity-WARNING_HI", Checkbox).value = False
        await pilot.pause()
        assert column(table, "name") == ["comp.Started"]
        # New events obey the filters as they arrive
        model.data_callback(make_event(2, "Hot", "WARNING_HI", "engine hot"))
        model.data_callback(make_event(1, "Started", "ACTIVITY_HI", "engine started again"))
        await refresh(app, pilot)
        assert column(table, "message") == ["engine started", "engine started again"]
        assert "filtered" in str(app.query_one("#events").border_subtitle)
        app.query_one("#severity-WARNING_HI", Checkbox).value = True
        app.query_one("#events-filter", Input).value = ""
        await pilot.pause()
        assert table.row_count == 5

    run(scenario)


def test_events_capped():
    async def scenario(app, pilot, model, sender):
        table = app.query_one("#events-table", DataTable)
        for index in range(25):
            model.data_callback(make_event(1, "EV", "COMMAND", f"message {index}"))
            if index % 4 == 0:
                await refresh(app, pilot)
        await refresh(app, pilot)
        assert column(table, "message") == [f"message {index}" for index in range(15, 25)]

    run(scenario, max_events=10)


def test_event_flood_rebuilds():
    async def scenario(app, pilot, model, sender):
        for index in range(1200):
            model.data_callback(make_event(1, "EV", "COMMAND", f"message {index}"))
        await refresh(app, pilot)
        table = app.query_one("#events-table", DataTable)
        assert table.row_count == 1000
        assert column(table, "message")[-1] == "message 1199"
        assert table.cursor_row == 999

    run(scenario, max_events=1000)


def test_events_follow_tail_unless_scrolled_back():
    async def scenario(app, pilot, model, sender):
        table = app.query_one("#events-table", DataTable)
        for index in range(100):
            model.data_callback(make_event(1, "EV", "COMMAND", f"message {index}"))
        await refresh(app, pilot)
        assert table.cursor_row == 99
        table.move_cursor(row=10)
        model.data_callback(make_event(1, "EV", "COMMAND", "late"))
        await refresh(app, pilot)
        assert table.cursor_row == 10

    run(scenario)


def test_clear_events():
    async def scenario(app, pilot, model, sender):
        model.data_callback(make_event(1, "EV", "COMMAND", "x"))
        await refresh(app, pilot)
        await pilot.press("ctrl+l")
        assert app.query_one("#events-table", DataTable).row_count == 0
        assert model.events() == []

    run(scenario)


def test_filter_change_while_events_are_pending_shows_each_event_once():
    async def scenario(app, pilot, model, sender):
        model.data_callback(make_event(1, "EV_A", "ACTIVITY_HI", "first"))
        await refresh(app, pilot)
        # Arrives after the last refresh, then the filter rebuilds the table from the model before the next one
        model.data_callback(make_event(2, "EV_B", "ACTIVITY_HI", "second"))
        app.query_one("#events-filter", Input).value = "ev_"
        await pilot.pause()
        await refresh(app, pilot)
        table = app.query_one("#events-table", DataTable)
        assert column(table, "name") == ["comp.EV_A", "comp.EV_B"]
        app.query_one("#severity-ACTIVITY_HI", Checkbox).value = False
        model.data_callback(make_event(3, "EV_C", "WARNING_HI", "third"))
        await pilot.pause()
        await refresh(app, pilot)
        assert column(table, "name") == ["comp.EV_C"]

    run(scenario)


def test_markup_and_escapes_in_downlinked_text_are_inert():
    async def scenario(app, pilot, model, sender):
        hostile = "[bold red]x[/] [@click=app.quit]y[/] \x1b[2J \x1b]0;t\x07"
        model.data_callback(make_event(1, "EV", "WARNING_LO", hostile))
        await refresh(app, pilot)
        message = column(app.query_one("#events-table", DataTable), "message")[0]
        assert "[bold red]x[/] [@click=app.quit]y[/]" in message
        assert "\x1b" not in message and "\x07" not in message

    run(scenario)


# ---- Commanding ----


def test_send_from_command_string():
    async def scenario(app, pilot, model, sender):
        assert app.focused.id == "command-text"
        await type_text(pilot, 'comp.NO_OP_STRING, "hello, world"')
        # The form follows the command string
        assert app.query_one("#command-select", Select).value == "comp.NO_OP_STRING"
        assert app.query_one("#argument-0", Input).value == "hello, world"
        await pilot.press("enter")
        await settle(app, pilot)
        assert sender.sent == [("comp.NO_OP_STRING", ["hello, world"])]
        assert sender.thread_names[0] != threading.current_thread().name
        history = app.query_one("#history", OptionList)
        assert history.option_count == 1
        assert 'sent    comp.NO_OP_STRING, "hello, world"' in str(history.get_option_at_index(0).prompt)
        assert "sent: 1" in text_of(app, "#status")

    run(scenario)


def test_pasted_command_string_fills_form():
    async def scenario(app, pilot, model, sender):
        from textual import events

        app.query_one("#command-text", Input).post_message(events.Paste("other.MIXED, 7, 1.5, yes, SECOND"))
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#command-select", Select).value == "other.MIXED"
        assert app.query_one("#argument-0", Input).value == "7"
        assert app.query_one("#argument-1", Input).value == "1.5"
        assert app.query_one("#argument-2", Select).value == "True"
        assert app.query_one("#argument-3", Select).value == "SECOND"
        await pilot.press("enter")
        await settle(app, pilot)
        assert sender.sent == [("other.MIXED", ["7", "1.5", "yes", "SECOND"])]

    run(scenario)


def test_send_from_form():
    async def scenario(app, pilot, model, sender):
        select = app.query_one("#command-select", Select)
        select.focus()
        select.value = "other.MIXED"
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#command-text", Input).value == 'other.MIXED, , , , ""'
        # Tab moves through the argument fields in order
        await pilot.press("tab")
        assert app.focused.id == "argument-0"
        await type_text(pilot, "12")
        await pilot.press("tab")
        assert app.focused.id == "argument-1"
        await type_text(pilot, "0.5")
        await pilot.press("tab")
        assert app.focused.id == "argument-2"
        app.focused.value = "False"
        await pilot.press("tab")
        assert app.focused.id == "argument-3"
        app.focused.value = "FIRST"
        await pilot.pause()
        assert app.query_one("#command-text", Input).value == "other.MIXED, 12, 0.5, False, FIRST"
        await pilot.click("#send")
        await settle(app, pilot)
        assert sender.sent == [("other.MIXED", ["12", "0.5", "False", "FIRST"])]

    run(scenario)


def test_enter_in_argument_field_sends():
    async def scenario(app, pilot, model, sender):
        select = app.query_one("#command-select", Select)
        select.focus()
        select.value = "comp.NO_OP_STRING"
        await pilot.pause()
        await pilot.pause()
        await pilot.press("tab")
        await type_text(pilot, "a, b")
        assert app.query_one("#command-text", Input).value == 'comp.NO_OP_STRING, "a, b"'
        await pilot.press("enter")
        await settle(app, pilot)
        assert sender.sent == [("comp.NO_OP_STRING", ["a, b"])]

    run(scenario)


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("", "empty command"),
        ("comp.MISSING", "unknown command"),
        ("NO_OP_STRING, x", "did you mean comp.NO_OP_STRING"),
        ("comp.NO_OP, 1", "takes 0 argument(s)"),
        ("other.MIXED, 1, 2", "takes 4 argument(s) (count, ratio, flag, choice), 2 given"),
        ("other.MIXED, x, 1.0, true, FIRST", "count[U32Type]"),
        ("other.MIXED, 1, 1.0, maybe, FIRST", "flag[BoolType]"),
        ("other.MIXED, 1, 1.0, true, THIRD", "choice["),
        ("other.MIXED, 99999999999, 1.0, true, FIRST", "count[U32Type]"),
        ('comp.NO_OP_STRING, "open', "unterminated quote"),
        ("comp.NO_OP_STRING, " + "x" * 41, "text[String_40]"),
    ],
)
def test_invalid_commands_are_not_sent(text, fragment):
    async def scenario(app, pilot, model, sender):
        app.query_one("#command-text", Input).value = text
        await pilot.pause()
        await pilot.press("enter")
        await settle(app, pilot)
        assert sender.sent == []
        error = text_of(app, "#command-error")
        assert error.startswith("Not sent:") and fragment in error
        assert app.query_one("#history", OptionList).option_count == 0

    run(scenario)


def test_transport_failure_is_reported_not_raised():
    async def scenario(app, pilot, model, sender):
        sender.error = ConnectionError("link \x1b[31mdown")
        app.query_one("#command-text", Input).value = "comp.NO_OP"
        await pilot.press("enter")
        await settle(app, pilot)
        error = text_of(app, "#command-error")
        assert "Send failed: ConnectionError: link" in error and "\x1b" not in error
        history = app.query_one("#history", OptionList)
        assert "FAILED  comp.NO_OP" in str(history.get_option_at_index(0).prompt)
        assert "sent: 0" in text_of(app, "#status")

    run(scenario)


def test_rapid_sends_go_out_one_at_a_time_in_order():
    async def scenario(app, pilot, model, sender):
        sender.delays = {"comp.NO_OP": 0.2}  # The first command is the slowest to send
        texts = ["comp.NO_OP", "comp.NO_OP_STRING, a", "comp.NO_OP_STRING, b", "other.MIXED, 1, 2.0, true, FIRST"]
        for text in texts:
            app.send_text(text)
        assert "SENDING: 4" in text_of(app, "#status")
        await settle(app, pilot)
        assert [name for name, _ in sender.sent] == [text.split(",")[0] for text in texts]
        assert sender.sent[1:3] == [("comp.NO_OP_STRING", ["a"]), ("comp.NO_OP_STRING", ["b"])]
        history = app.query_one("#history", OptionList)
        assert [str(history.get_option_at_index(index).prompt).split("sent    ")[1] for index in range(4)] == [
            "other.MIXED, 1, 2.0, true, FIRST",
            "comp.NO_OP_STRING, b",
            "comp.NO_OP_STRING, a",
            "comp.NO_OP",
        ]
        status = text_of(app, "#status")
        assert "sent: 4" in status and "SENDING" not in status

    run(scenario)


def test_comma_in_a_numeric_field_stays_in_that_argument():
    async def scenario(app, pilot, model, sender):
        select = app.query_one("#command-select", Select)
        select.focus()
        select.value = "other.MIXED"
        await pilot.pause()
        await pilot.pause()
        await pilot.press("tab")
        await type_text(pilot, "1,2")
        assert app.query_one("#command-text", Input).value == 'other.MIXED, "1,2", , , ""'
        await pilot.press("enter")
        await settle(app, pilot)
        assert sender.sent == []
        assert "Not sent" in text_of(app, "#command-error")

    run(scenario)


def test_unknown_command_string_clears_the_form():
    async def scenario(app, pilot, model, sender):
        command_text = app.query_one("#command-text", Input)
        command_text.value = "comp.NO_OP_STRING, hello"
        await pilot.pause()
        assert app.current_command == "comp.NO_OP_STRING"
        command_text.value = "comp.NOT_A_COMMAND, hello"
        await pilot.pause()
        assert app.current_command is None
        assert app.query_one("#command-select", Select).is_blank()
        assert len(app.query("#arguments Input")) == 0
        assert command_text.value == "comp.NOT_A_COMMAND, hello"

    run(scenario)


def test_history_reloads_command():
    async def scenario(app, pilot, model, sender):
        command_text = app.query_one("#command-text", Input)
        for text in ("comp.NO_OP", "other.MIXED, 1, 2.0, true, SECOND"):
            command_text.value = text
            await pilot.pause(TYPE_AHEAD_SECONDS + 0.1)
            await pilot.press("enter")
            await settle(app, pilot)
        command_text.value = ""
        await pilot.pause()
        assert app.query_one("#command-select", Select).is_blank()
        history = app.query_one("#history", OptionList)
        history.focus()
        history.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        assert command_text.value == "other.MIXED, 1, 2.0, true, SECOND"
        assert app.focused.id == "command-text"
        assert app.query_one("#argument-3", Select).value == "SECOND"
        history.focus()
        history.highlighted = 1
        await pilot.press("enter")
        await pilot.pause()
        assert command_text.value == "comp.NO_OP"
        assert len(app.query("#arguments Input, #arguments Select")) == 0
        assert len(app.query("#no-arguments")) == 1
        assert len(app.argument_widgets) == 0

    run(scenario)


# ---- Keys and status ----


def test_panel_keys():
    async def scenario(app, pilot, model, sender):
        assert app.current_panel() == "commands"
        await pilot.press("f3")
        assert app.focused.id == "telemetry-table"
        await pilot.press("f4")
        assert app.focused.id == "events-table"
        await pilot.press("f2")
        assert app.focused.id == "command-text"
        # Ctrl+Tab cycles Commands -> Telemetry -> Events -> Commands
        visited = []
        for _ in range(3):
            await pilot.press("ctrl+tab")
            visited.append(app.current_panel())
        assert visited == ["telemetry", "events", "commands"]
        await pilot.press("ctrl+shift+tab")
        assert app.current_panel() == "events"
        # Function keys work from inside a text field; Esc leaves the field
        await pilot.press("slash")
        assert app.focused.id == "events-filter"
        await pilot.press("escape")
        assert app.focused.id == "events-table"
        await pilot.press("slash", "f3")
        assert app.focused.id == "telemetry-table"

    run(scenario)


def test_ctrl_up_and_down_resize_the_commands_pane_from_any_panel():
    async def scenario(app, pilot, model, sender):
        commands = app.query_one("#commands")
        start = commands.styles.height.value
        await pilot.press("f3")  # Ctrl+Up/Down work the same from telemetry or events as from commands
        await pilot.press("ctrl+up")
        assert commands.styles.height.value == start + app_module.PANE_RESIZE_STEP
        await pilot.press("ctrl+down")
        await pilot.press("ctrl+down")
        assert commands.styles.height.value == start - app_module.PANE_RESIZE_STEP

    run(scenario)


def test_resizing_the_commands_pane_stops_at_the_floor_on_either_side():
    async def scenario(app, pilot, model, sender):
        commands = app.query_one("#commands")
        for _ in range(30):
            await pilot.press("ctrl+down")
        assert commands.styles.height.value == app_module.COMMANDS_MIN_HEIGHT
        for _ in range(30):
            await pilot.press("ctrl+up")
        assert commands.styles.height.value == app.size.height - app_module.TOP_MIN_HEIGHT

    run(scenario)


def test_ctrl_left_and_right_resize_the_telemetry_events_boundary():
    async def scenario(app, pilot, model, sender):
        telemetry = app.query_one("#telemetry")
        start = telemetry.styles.width.value
        await pilot.press("f4")  # Ctrl+Left/Right work the same from events as from telemetry
        await pilot.press("ctrl+right")
        assert telemetry.styles.width.value == start + app_module.PANE_RESIZE_STEP_COLUMNS
        await pilot.press("ctrl+left")
        await pilot.press("ctrl+left")
        assert telemetry.styles.width.value == start - app_module.PANE_RESIZE_STEP_COLUMNS

    run(scenario)


def test_resizing_the_telemetry_events_boundary_stops_at_the_floor_on_either_side():
    async def scenario(app, pilot, model, sender):
        telemetry = app.query_one("#telemetry")
        for _ in range(60):
            await pilot.press("ctrl+left")
        assert telemetry.styles.width.value == app_module.TELEMETRY_MIN_WIDTH
        for _ in range(60):
            await pilot.press("ctrl+right")
        assert telemetry.styles.width.value == app.size.width - app_module.EVENTS_MIN_WIDTH

    run(scenario)


def test_ctrl_left_and_right_do_nothing_useful_in_a_narrow_terminal():
    async def scenario(app, pilot, model, sender):
        telemetry = app.query_one("#telemetry")
        await pilot.press("f4")
        await pilot.press("ctrl+right")
        assert telemetry.styles.width.value == 1  # Flexes (1fr) to fill whichever of the two panes is shown

    run(scenario, size=(80, 24))


def test_ctrl_left_and_right_resize_the_boundary_even_while_typing_a_command():
    async def scenario(app, pilot, model, sender):
        telemetry = app.query_one("#telemetry")
        start = telemetry.styles.width.value
        command_text = app.query_one("#command-text", Input)
        command_text.value = "comp.NO_OP"
        command_text.cursor_position = len(command_text.value)
        await pilot.press("ctrl+right")
        assert telemetry.styles.width.value == start + app_module.PANE_RESIZE_STEP_COLUMNS
        assert command_text.value == "comp.NO_OP"  # The cursor did not jump a word left instead

    run(scenario)


def test_slash_types_in_text_fields():
    async def scenario(app, pilot, model, sender):
        await pilot.press("f3", "slash", "a", "slash", "b")
        assert app.query_one("#telemetry-filter", Input).value == "a/b"

    run(scenario)


def test_tab_moves_between_fields():
    async def scenario(app, pilot, model, sender):
        seen = []
        for _ in range(40):
            await pilot.press("tab")
            seen.append(app.focused.id)
            if seen.count(seen[0]) == 2:
                break
        for expected in ("telemetry-filter", "telemetry-table", "events-filter", "events-table",
                         "command-select", "command-text", "send", "severity-FATAL"):
            assert expected in seen

    run(scenario)


def test_help_screen():
    async def scenario(app, pilot, model, sender):
        await pilot.press("f1")
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        assert not isinstance(app.screen, HelpScreen)

    run(scenario)


def test_status_line():
    async def scenario(app, pilot, model, sender):
        status = text_of(app, "#status")
        assert "transport: yamcs" in status and "no data yet" in status and "Dictionary.json" in status
        model.data_callback(make_channel(1, "A", 1))
        await refresh(app, pilot)
        assert "receiving" in text_of(app, "#status")
        app.last_data_time -= 60
        app.update_status()
        assert "NO DATA for 60s" in text_of(app, "#status")

    run(scenario)


@pytest.mark.parametrize("size", [(80, 24), (60, 20), (100, 30), (132, 40), (220, 60)])
def test_usable_at_terminal_sizes(size):
    async def scenario(app, pilot, model, sender):
        model.data_callback(make_channel(1, "A" * 120, 1))
        model.data_callback(make_event(1, "EV" * 60, "FATAL", "x" * 500))
        await refresh(app, pilot)
        narrow = size[0] < 100
        events, telemetry = app.query_one("#events-table", DataTable), app.query_one("#telemetry-table", DataTable)
        # A narrow terminal shows one of the two tables at a time: events first, telemetry on F3, events again on F4
        for key, shown, hidden in (("f4", events, telemetry), ("f3", telemetry, events), ("f4", events, telemetry)):
            await pilot.press(key)
            await pilot.pause()
            await refresh(app, pilot)  # The periodic refresh refits the columns of a panel that changed size
            await pilot.pause()
            assert shown.region.width > 0 and shown.region.height >= 2, key
            assert (hidden.region.width == 0) == narrow, key
            # Every column fits the table: nothing is reached only by scrolling sideways, however long the content
            assert shown.virtual_size.width <= shown.size.width, key
            assert not shown.show_horizontal_scrollbar, key
        assert column(telemetry, "value") == ["1"]
        assert column(events, "severity") == ["FATAL"]
        assert column(events, "name")[0].startswith("\N{HORIZONTAL ELLIPSIS}")
        for selector in ("#command-text", "#command-select", "#status", "#history"):
            region = app.query_one(selector).region
            assert region.width > 0 and region.height > 0, selector
        # All severity toggles are on screen
        last = app.query_one("#severity-DIAGNOSTIC", Checkbox).region
        assert last.width > 0 and last.right <= app.size.width
        await pilot.press("f2")
        await type_text(pilot, "comp.NO_OP")
        await pilot.press("enter")
        await settle(app, pilot)
        assert sender.sent == [("comp.NO_OP", [])]

    run(scenario, size=size)


def test_enter_on_a_row_shows_it_in_full():
    async def scenario(app, pilot, model, sender):
        message = "start " + "x" * 400 + " the end"
        model.data_callback(make_event(1, "EV", "WARNING_HI", message))
        model.data_callback(make_channel(7, "LONG_VALUE", 12345))
        await refresh(app, pilot)
        for key, expected in (("f4", message), ("f3", "comp.LONG_VALUE")):
            await pilot.press(key, "enter")
            await pilot.pause()
            assert isinstance(app.screen, DetailScreen)
            assert expected in app.screen.text
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, DetailScreen)

    run(scenario)


def test_f6_cycles_panels_and_f5_sends_from_any_commanding_field():
    async def scenario(app, pilot, model, sender):
        visited = []
        for _ in range(3):
            await pilot.press("f6")
            visited.append(app.current_panel())
        assert visited == ["telemetry", "events", "commands"]
        await pilot.press("shift+f6")
        assert app.current_panel() == "events"
        await pilot.press("f2")
        app.query_one("#command-text", Input).value = "other.MIXED, 1, 2.0, true, FIRST"
        await pilot.pause()
        app.query_one("#argument-3", Select).focus()  # Enter would open this drop-down
        await pilot.pause(TYPE_AHEAD_SECONDS + 0.1)
        await pilot.press("f5")
        await settle(app, pilot)
        assert sender.sent == [("other.MIXED", ["1", "2.0", "True", "FIRST"])]

    run(scenario)


def test_typing_after_a_send_replaces_the_sent_command():
    async def scenario(app, pilot, model, sender):
        await type_text(pilot, "comp.NO_OP")
        await pilot.press("enter")
        await settle(app, pilot)
        command_text = app.query_one("#command-text", Input)
        assert command_text.value == "comp.NO_OP"  # Still shown, and selected
        await type_text(pilot, "comp.NO_OP_STRING, next")
        assert command_text.value == "comp.NO_OP_STRING, next"

    run(scenario)


def test_command_names_are_accepted_in_any_case():
    async def scenario(app, pilot, model, sender):
        await type_text(pilot, "COMP.no_op_string, hi")
        assert app.current_command == "comp.NO_OP_STRING"
        await pilot.press("enter")
        await settle(app, pilot)
        assert sender.sent == [("comp.NO_OP_STRING", ["hi"])]

    run(scenario)


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "1e400", "1e39"])
def test_non_finite_floats_are_not_sent(value):
    async def scenario(app, pilot, model, sender):
        app.send_text(f"other.MIXED, 1, {value}, true, FIRST")
        await settle(app, pilot)
        assert sender.sent == []
        assert "ratio must be a finite number" in text_of(app, "#command-error")

    run(scenario)


def test_stale_link_is_highlighted():
    async def scenario(app, pilot, model, sender):
        status = app.query_one("#status", Static)
        model.data_callback(make_channel(1, "A", 1))
        await refresh(app, pilot)
        assert not status.has_class("-stale")
        app.last_data_time -= 60
        app.update_status()
        assert status.has_class("-stale")
        model.data_callback(make_channel(1, "A", 2))
        await refresh(app, pilot)
        assert not status.has_class("-stale")

    run(scenario)


def test_filter_on_a_long_event_list_waits_for_a_pause_in_typing():
    async def scenario(app, pilot, model, sender):
        for index in range(1500):
            model.data_callback(make_event(1, "EV", "ACTIVITY_HI", f"number {index}"))
        await refresh(app, pilot)
        table = app.query_one("#events-table", DataTable)
        await pilot.press("f4", "slash")
        await pilot.press(*"number")
        assert table.row_count == 1500  # Not redrawn per keystroke
        await pilot.press("space", *"1499")
        await pilot.pause(0.4)
        assert column(table, "message") == ["number 1499"]
        assert app.query_one("#events").border_subtitle == "1 of 1500 shown (filtered)"

    run(scenario)


def test_part_of_a_command_name_lists_the_matching_commands():
    async def scenario(app, pilot, model, sender):
        await type_text(pilot, "no_op")
        matches = app.query_one("#command-matches", OptionList)
        assert [str(matches.get_option_at_index(index).prompt) for index in range(matches.option_count)] == [
            "comp.NO_OP",
            "comp.NO_OP_STRING",
        ]
        await pilot.press("enter")
        assert "Not sent: unknown command 'no_op'" in text_of(app, "#command-error")

    run(scenario)


def test_selecting_a_matching_command_fills_it_into_the_command_string():
    async def scenario(app, pilot, model, sender):
        await type_text(pilot, "no_op_string")
        matches = app.query_one("#command-matches", OptionList)
        assert matches.option_count == 1
        matches.action_select()
        await pilot.pause()
        assert app.query_one("#command-text", Input).value == "comp.NO_OP_STRING"
        assert app.query_one("#command-select", Select).value == "comp.NO_OP_STRING"
        assert matches.option_count == 0

    run(scenario)


def test_a_green_arrow_shows_next_to_the_command_string_while_it_has_matches():
    async def scenario(app, pilot, model, sender):
        arrow = app.query_one("#command-matches-arrow", Static)
        assert arrow.has_class("-hidden")
        await type_text(pilot, "no_op")
        assert not arrow.has_class("-hidden")
        await type_text(pilot, "no_such_command")
        assert arrow.has_class("-hidden")

    run(scenario)


def test_down_arrow_drops_from_the_command_string_into_its_matches():
    async def scenario(app, pilot, model, sender):
        await type_text(pilot, "no_op")
        matches = app.query_one("#command-matches", OptionList)
        assert matches.option_count == 2
        await pilot.press("down")
        assert app.focused is matches

    run(scenario)


def test_down_arrow_is_ignored_when_the_command_string_has_no_matches():
    async def scenario(app, pilot, model, sender):
        await type_text(pilot, "no_such_command")
        matches = app.query_one("#command-matches", OptionList)
        assert matches.option_count == 0
        command_text = app.query_one("#command-text", Input)
        await pilot.press("down")
        assert app.focused is command_text

    run(scenario)


def test_row_cursor_shows_only_in_the_focused_table_and_newest_event_stays_in_view():
    async def scenario(app, pilot, model, sender):
        events, telemetry = app.query_one("#events-table", DataTable), app.query_one("#telemetry-table", DataTable)
        for index in range(200):
            model.data_callback(make_event(1, "EV", "WARNING_HI", f"number {index}"))
        await refresh(app, pilot)
        assert not events.show_cursor and not telemetry.show_cursor  # Focus is on the command string
        assert events.scroll_y == events.max_scroll_y > 0  # Following the newest event without the cursor
        await pilot.press("f4")
        await pilot.pause()
        assert events.show_cursor and not telemetry.show_cursor
        await pilot.press("f3")
        await pilot.pause()
        assert telemetry.show_cursor and not events.show_cursor

    run(scenario)


def test_send_key_typed_ahead_of_the_screen_is_not_obeyed(monkeypatch):
    async def scenario(app, pilot, model, sender):
        command_text = app.query_one("#command-text", Input)
        command_text.value = "comp.NO_OP"
        await pilot.pause()
        # However slowly this machine handles the keys, they count as typed ahead
        monkeypatch.setattr(app_module, "TYPE_AHEAD_SECONDS", 3600.0)
        # Keys arriving in one burst: the drop-down is not open yet when its keys are handled
        await pilot.press("shift+tab", "enter", "down", "enter", "f2", "enter")
        await settle(app, pilot)
        assert sender.sent == []
        # A second Enter straight after the first (key bounce) does not send twice
        command_text.focus()
        command_text.value = "comp.NO_OP"
        await pilot.pause()
        await pilot.press("x", "backspace", "enter", "enter")
        await settle(app, pilot)
        assert sender.sent == [("comp.NO_OP", [])]
        assert "press it again" in text_of(app, "#command-error")
        # Enter after a pause is obeyed
        monkeypatch.setattr(app_module, "TYPE_AHEAD_SECONDS", 0.0)
        await pilot.press("enter")
        await settle(app, pilot)
        assert sender.sent == [("comp.NO_OP", [])] * 2

    run(scenario)


def test_send_is_refused_while_form_and_command_string_differ():
    async def scenario(app, pilot, model, sender):
        command_text = app.query_one("#command-text", Input)
        command_text.value = "comp.NO_OP"
        await pilot.pause()
        # The form is ahead of the command string, as when a drop-down choice has not reached the string yet
        app.query_one("#command-select", Select).value = "comp.NO_OP_STRING"
        await app.action_send()
        await settle(app, pilot)
        assert sender.sent == []
        assert "did not match" in text_of(app, "#command-error")
        assert app.query_one("#command-select", Select).value == "comp.NO_OP"
        await app.action_send()
        await settle(app, pilot)
        assert sender.sent == [("comp.NO_OP", [])]

    run(scenario)


def test_form_follows_a_string_being_typed_and_is_cleared_when_not_understood():
    async def scenario(app, pilot, model, sender):
        command_text = app.query_one("#command-text", Input)
        command_text.value = 'comp.NO_OP_STRING, "unterminat'
        await pilot.pause()
        assert app.current_command == "comp.NO_OP_STRING"
        assert app.query_one("#argument-0", Input).value == "unterminat"
        command_text.value = "comp.NO_OP_STRING, ]"
        await pilot.pause()
        assert app.current_command is None
        assert len(app.query("#arguments Input")) == 0

    run(scenario)


def test_many_channels_update_quickly():
    async def scenario(app, pilot, model, sender):
        for value in (1, 2):
            for channel in range(3000):
                model.data_callback(make_channel(channel, f"CH_{channel:04}", value))
            start = time.perf_counter()
            await refresh(app, pilot)
            assert time.perf_counter() - start < 5.0  # Was minutes when every update re-measured the column
        assert app.query_one("#telemetry-table", DataTable).row_count == 3000

    run(scenario)


def test_event_flood_is_redrawn_at_intervals_and_ends_complete():
    async def scenario(app, pilot, model, sender):
        table = app.query_one("#events-table", DataTable)
        for burst in range(3):
            for index in range(600):
                model.data_callback(make_event(1, "EV", "ACTIVITY_LO", f"{burst}-{index}"))
            await refresh(app, pilot)
        assert table.row_count == 600  # The first burst was drawn at once, the next ones wait their turn
        await pilot.pause(1.3)  # The periodic refresh catches up
        assert table.row_count == 1800
        assert column(table, "message")[-1] == "2-599"

    run(scenario)


def test_gds_style_arrays_are_made_json_and_strings_are_left_alone():
    async def scenario(app, pilot, model, sender):
        app.send_text("other.ARRAY, [1, 2.5, 3], [test]")
        await settle(app, pilot)
        app.send_text('comp.NO_OP_STRING, "[a, b]"')
        await settle(app, pilot)
        assert sender.sent == [("other.ARRAY", ["[1, 2.5, 3]", "[test]"]), ("comp.NO_OP_STRING", ["[a, b]"])]

    run(scenario)


@pytest.mark.parametrize("values", ["[1e999, 1, 1]", "[NaN, 1, 1]", "[1, -Infinity, 1]"])
def test_non_finite_numbers_inside_arrays_are_not_sent(values):
    async def scenario(app, pilot, model, sender):
        app.send_text(f"other.ARRAY, {values}, x")
        await settle(app, pilot)
        assert sender.sent == []
        assert "values holds a number that is not finite" in text_of(app, "#command-error")

    run(scenario)


def test_f5_sends_what_the_form_shows_and_only_from_the_commanding_panel():
    async def scenario(app, pilot, model, sender):
        command_text = app.query_one("#command-text", Input)
        command_text.value = "other.MIXED, 1, 2.0, true, FIRST"
        await pilot.pause()
        # Keys typed just ahead of F5 are part of what is sent
        await pilot.press("f2", "end", "backspace", "backspace", "backspace", "backspace", "backspace")
        await pilot.press(*"SECOND", "f5")
        await settle(app, pilot)
        assert sender.sent == [("other.MIXED", ["1", "2.0", "true", "SECOND"])]
        # A drop-down argument changed in the form is sent as changed, before the command string has caught up
        choice = app.query_one("#argument-3", Select)
        choice.focus()
        await pilot.pause()
        choice.value = "FIRST"
        await app.action_send()
        await settle(app, pilot)
        assert sender.sent[-1] == ("other.MIXED", ["1", "2.0", "True", "FIRST"])
        # Not from the other panels, nor under the help or a detail screen
        for keys in (("f3",), ("f4",), ("f2", "f1")):
            await pilot.pause(TYPE_AHEAD_SECONDS + 0.1)
            await pilot.press(*keys)
            await pilot.pause(TYPE_AHEAD_SECONDS + 0.1)
            await pilot.press("f5")
            await settle(app, pilot)
        assert len(sender.sent) == 2

    run(scenario)


def test_commands_waiting_behind_a_failed_send_are_not_sent():
    async def scenario(app, pilot, model, sender):
        sender.delays = {"comp.NO_OP": 0.2}
        sender.error = ConnectionError("link down")
        for text in ("comp.NO_OP", "comp.NO_OP_STRING, a", "comp.NO_OP_STRING, b"):
            app.send_text(text)
        await settle(app, pilot)
        assert sender.thread_names == ["fprime-console-send"]  # One attempt only
        assert "2 command(s) waiting behind it were not sent" in text_of(app, "#command-error")
        assert app.query_one("#history", OptionList).option_count == 1

    run(scenario)


def test_alert_events_are_announced_while_a_narrow_terminal_shows_telemetry():
    async def scenario(app, pilot, model, sender):
        status = app.query_one("#status", Static)
        await pilot.press("f3")
        model.data_callback(make_event(1, "EV", "ACTIVITY_HI", "routine"))
        await refresh(app, pilot)
        assert "EVENT(S)" not in text_of(app, "#status") and not status.has_class("-stale")
        model.data_callback(make_event(2, "EV", "FATAL", "bad"))
        model.data_callback(make_event(3, "EV", "WARNING_HI", "worse"))
        await refresh(app, pilot)
        assert text_of(app, "#status").startswith("2 FATAL/WARNING_HI EVENT(S): F4")
        assert status.has_class("-stale")
        await pilot.press("f4")
        await refresh(app, pilot)
        assert "EVENT(S)" not in text_of(app, "#status") and not status.has_class("-stale")

    run(scenario, size=(80, 24))


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_terminating_signal_leaves_through_the_normal_exit(number):
    async def scenario(app, pilot, model, sender):
        os.kill(os.getpid(), number)
        await pilot.pause(0.2)
        assert app.return_code == 128 + number
        assert not app.is_running

    run(scenario)


def test_out_of_limit_telemetry_is_marked_in_text_as_well_as_colour():
    async def scenario(app, pilot, model, sender):
        limits = {"low_red": 0, "high_red": 100, "high_orange": 80, "high_yellow": 60}
        for channel, value in enumerate((50, 70, 90, 150)):
            model.data_callback(make_channel(channel, f"CH_{channel}", value, **limits))
        await refresh(app, pilot)
        table = app.query_one("#telemetry-table", DataTable)
        assert column(table, "value") == ["50", "!Y 70", "!O 90", "!R 150"]
        styles = [str(table.get_cell(row_key, "value").style) for row_key in table.rows]
        assert styles == ["", LIMIT_STYLES["yellow"], LIMIT_STYLES["orange"], LIMIT_STYLES["red"]]

    run(scenario)
