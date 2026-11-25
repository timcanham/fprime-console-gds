"""Tests of the fprime-tui entry point's arguments and failure handling"""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from fprime_console_gds import main as fprime_tui
from fprime_gds.executables.cli import MiddleWareParser, StandardPipelineParser

DICTIONARY = Path(__file__).parent / "simple_dictionary.json"


def test_default_transport_is_yamcs_for_the_tui_only():
    assert fprime_tui.TuiMiddleWareParser.DEFAULT_TRANSPORT == "yamcs"
    assert MiddleWareParser.DEFAULT_TRANSPORT != "yamcs"
    assert fprime_tui.TuiMiddleWareParser in fprime_tui.TuiPipelineParser.CONSTITUENTS
    assert MiddleWareParser not in fprime_tui.TuiPipelineParser.CONSTITUENTS
    assert len(fprime_tui.TuiPipelineParser.CONSTITUENTS) == len(StandardPipelineParser.CONSTITUENTS)
    assert MiddleWareParser in StandardPipelineParser.CONSTITUENTS


def test_selection_help_names_the_tui_default():
    arguments = fprime_tui.TuiMiddleWareParser().get_arguments()
    assert "default: yamcs" in arguments[("--data-transport-selection",)]["help"]


def run_main(arguments, logs):
    """Exit code of main, whether returned or raised by the argument parser; logs go to the given directory"""
    try:
        return fprime_tui.main(arguments + ["--logs", str(logs)])
    except SystemExit as exit_request:
        return exit_request.code


@pytest.mark.parametrize("value", ["0", "-5"])
def test_max_events_must_be_positive(value, capsys, tmp_path):
    arguments = ["--max-events", value, "--dictionary", str(DICTIONARY), "--data-transport-selection", "zmq"]
    assert run_main(arguments, tmp_path) not in (0, None)
    error = capsys.readouterr().err
    assert "--max-events must be at least 1" in error and "Traceback" not in error


def test_bad_dictionary_is_reported_without_traceback(capsys, tmp_path):
    arguments = ["--dictionary", str(tmp_path / "missing.json"), "--data-transport-selection", "zmq"]
    assert run_main(arguments, tmp_path) not in (0, None)
    error = capsys.readouterr().err
    assert "[ERROR]" in error and "does not exist" in error and "Traceback" not in error


def test_missing_transport_names_the_package_and_the_alternatives(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(fprime_tui.TuiMiddleWareParser, "installed_transports", lambda self: ["zmq"])
    assert run_main(["--dictionary", str(DICTIONARY)], tmp_path) not in (0, None)
    error = capsys.readouterr().err
    assert "'yamcs' is not installed" in error and "fprime-yamcs" in error
    assert "--data-transport-selection: zmq" in error


class FakeCoders:
    def __init__(self):
        self.channel_consumers = []
        self.event_consumers = []

    def register_channel_consumer(self, consumer):
        self.channel_consumers.append(consumer)

    def register_event_consumer(self, consumer):
        self.event_consumers.append(consumer)


class FakePipeline:
    def __init__(self):
        self.coders = FakeCoders()
        self.dictionaries = SimpleNamespace(command_name={"comp.NO_OP": object()}, dictionary_path="/a/Dict.json")
        self.disconnected = 0

    def send_command(self, command, args):
        """Stand-in for StandardPipeline.send_command"""

    def disconnect(self):
        self.disconnected += 1


@pytest.fixture
def wired(monkeypatch):
    """main() with the argument parser, pipeline and application replaced by recording stand-ins"""
    app_module = pytest.importorskip("fprime_console_gds.app")
    pipeline = FakePipeline()
    record = SimpleNamespace(pipeline=pipeline, app=None, return_code=None, run_error=None, factory_error=None)

    class FakeApp:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.return_code = record.return_code
            record.app = self

        def run(self):
            if record.run_error is not None:
                raise record.run_error

    def factory(args, pipeline_arg):
        if record.factory_error is not None:
            raise record.factory_error
        return pipeline

    args = SimpleNamespace(max_events=7, data_transport_selection="yamcs")
    monkeypatch.setattr(fprime_tui.ParserBase, "parse_args", staticmethod(lambda *_, **__: (args, None)))
    monkeypatch.setattr(fprime_tui.TuiPipelineParser, "pipeline_factory", staticmethod(factory))
    monkeypatch.setattr(app_module, "FprimeTui", FakeApp)
    return record


def test_main_connects_model_to_pipeline_and_disconnects(wired):
    assert fprime_tui.main([]) == 0
    pipeline, kwargs = wired.pipeline, wired.app.kwargs
    model = kwargs["model"]
    assert pipeline.coders.channel_consumers == [model]
    assert pipeline.coders.event_consumers == [model]
    assert kwargs["send_command"] == pipeline.send_command
    assert kwargs["command_templates"] is pipeline.dictionaries.command_name
    assert (kwargs["transport_name"], kwargs["dictionary_name"], kwargs["max_events"]) == ("yamcs", "Dict.json", 7)
    assert pipeline.disconnected == 1


def test_main_reports_a_crashed_ui_in_the_exit_code(wired):
    wired.return_code = 1
    assert fprime_tui.main([]) == 1
    assert wired.pipeline.disconnected == 1


def test_main_disconnects_when_the_ui_raises(wired):
    wired.run_error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        fprime_tui.main([])
    assert wired.pipeline.disconnected == 1


def test_main_reports_connection_failure(wired, capsys):
    wired.factory_error = ConnectionError("refused")
    assert fprime_tui.main([]) == 1
    error = capsys.readouterr().err
    assert "Could not connect using the 'yamcs' data transport: refused" in error
    assert wired.app is None


def test_main_reports_a_user_interface_that_cannot_be_loaded(wired, capsys, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "fprime_console_gds.app", None)
    assert fprime_tui.main([]) == 1
    assert "is Textual installed?" in capsys.readouterr().err
    assert wired.app is None


def test_connection_failure_text_cannot_control_the_terminal(wired, capsys):
    wired.factory_error = ConnectionError("refused \x1b]0;TITLE\x07\x1b[2J")
    assert fprime_tui.main([]) == 1
    error = capsys.readouterr().err
    assert "refused" in error and "\x1b" not in error and "\x07" not in error


def test_disconnect_failure_is_reported_not_raised(wired, capsys, monkeypatch):
    def fail():
        raise ConnectionResetError("already closed \x1b[2J")

    monkeypatch.setattr(wired.pipeline, "disconnect", fail)
    assert fprime_tui.main([]) == 0
    error = capsys.readouterr().err
    assert "[WARNING] Disconnecting the data transport failed: ConnectionResetError: already closed" in error
    assert "\x1b" not in error


def test_stuck_disconnect_does_not_hold_up_exit(wired, capsys, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(wired.pipeline, "disconnect", release.wait)
    monkeypatch.setattr(fprime_tui, "DISCONNECT_SECONDS", 0.1)
    assert fprime_tui.main([]) == 0
    release.set()
    assert "did not disconnect within" in capsys.readouterr().err


def test_deprecated_no_zmq_alias_is_not_mistaken_for_the_yamcs_default(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(fprime_tui.TuiMiddleWareParser, "installed_transports", lambda self: ["zmq"])
    run_main(["--dictionary", str(tmp_path / "missing.json"), "--no-zmq"], tmp_path)
    assert "is not installed" not in capsys.readouterr().err
