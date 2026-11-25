"""
fprime_console_gds.main:

Entry point of `fprime-console-gds` (also installed as `fprime-tui`), the terminal UI for F Prime telemetry, events,
and commanding. It connects through the standard GDS pipeline and a data transport plugin (default: yamcs), exactly
as `fprime-cli` and the integration test API do.
"""
import os
import sys
import threading
from typing import Any, Dict, List, Tuple

from fprime_gds.common.pipeline.standard import StandardPipeline
from fprime_gds.executables.cli import MiddleWareParser, ParserBase, StandardPipelineParser
from fprime_console_gds.model import DEFAULT_MAX_EVENTS, TuiModel, sanitize


class TuiMiddleWareParser(MiddleWareParser):
    """Data transport selection defaulting to the yamcs transport"""

    DEFAULT_TRANSPORT = "yamcs"

    def installed_transports(self) -> List[str]:
        """Names of the installed data transport plugins"""
        plugins = self.plugin_parser.plugin_system.get_plugins(self.CATEGORY)
        return sorted(plugin.get_name() for plugin in plugins)

    def handle_arguments(self, args, **kwargs):
        """Name the missing package when the selected transport, usually the default, is not installed"""
        selection = args.data_transport_selection
        if selection is None and getattr(args, "zmq", True):  # Not chosen, nor through the deprecated --no-zmq
            selection = self.DEFAULT_TRANSPORT
        installed = self.installed_transports()
        if selection is not None and selection not in installed:
            hint = ' (the yamcs transport is provided by the "fprime-yamcs" package)' if selection == "yamcs" else ""
            raise ValueError(
                f"Data transport '{selection}' is not installed{hint}. "
                f"Choose an installed transport with --data-transport-selection: {', '.join(installed)}"
            )
        return super().handle_arguments(args, **kwargs)


class TuiPipelineParser(StandardPipelineParser):
    """Standard pipeline parser with the terminal UI's default transport"""

    CONSTITUENTS = [
        TuiMiddleWareParser if constituent is MiddleWareParser else constituent
        for constituent in StandardPipelineParser.CONSTITUENTS
    ]


class TuiParser(ParserBase):
    """Arguments of the terminal UI itself"""

    DESCRIPTION = "Terminal UI options"

    def get_arguments(self) -> Dict[Tuple[str, ...], Dict[str, Any]]:
        """Arguments of the terminal UI"""
        return {
            ("--max-events",): {
                "dest": "max_events",
                "type": int,
                "default": DEFAULT_MAX_EVENTS,
                "help": "Number of most recent events kept for display [default: %(default)s]",
            }
        }

    def handle_arguments(self, args, **kwargs):
        """Check the terminal UI arguments"""
        if args.max_events < 1:
            raise ValueError("--max-events must be at least 1")
        return args


DISCONNECT_SECONDS = 5.0


def report(message: str) -> None:
    """Print a message to standard error with terminal control characters replaced: it may quote a remote system"""
    print(sanitize(message), file=sys.stderr)


def disconnect(pipeline) -> None:
    """Disconnect the pipeline, reporting rather than raising a failure and not waiting forever on a stuck transport"""
    failures = []

    def run() -> None:
        try:
            pipeline.disconnect()
        except Exception as error:  # Leaving anyway; the failure is reported
            failures.append(error)

    thread = threading.Thread(target=run, name="fprime-console-disconnect", daemon=True)
    thread.start()
    thread.join(DISCONNECT_SECONDS)
    if thread.is_alive():
        report(f"[WARNING] The data transport did not disconnect within {DISCONNECT_SECONDS:.0f} seconds")
    for error in failures:
        report(f"[WARNING] Disconnecting the data transport failed: {type(error).__name__}: {error}")


def main(arguments=None) -> int:
    """Run the terminal UI

    Args:
        arguments: command line arguments, None to use the process arguments
    Returns:
        process exit code
    """
    try:
        args, _ = ParserBase.parse_args(
            [TuiPipelineParser, TuiParser],
            description="Terminal UI for F Prime telemetry, events, and commanding",
            arguments=arguments,
            client=True,
        )
    except Exception as error:
        report(f"[ERROR] {error}")
        return 1

    try:
        from fprime_console_gds.app import FprimeTui
    except ImportError as error:
        report(f"[ERROR] The terminal UI could not be loaded; is Textual installed? ({error})")
        return 1
    model = TuiModel(max_events=args.max_events)
    pipeline = None
    try:
        pipeline = StandardPipeline()
        pipeline.histories.implementation = None  # No backlog needed: the terminal UI only shows live data
        pipeline = TuiPipelineParser.pipeline_factory(args, pipeline)
        pipeline.coders.register_channel_consumer(model)
        pipeline.coders.register_event_consumer(model)
    except Exception as error:
        report(f"[ERROR] Could not connect using the '{args.data_transport_selection}' data transport: {error}")
        if pipeline is not None:
            disconnect(pipeline)
        return 1
    try:
        app = FprimeTui(
            model=model,
            command_templates=pipeline.dictionaries.command_name,
            send_command=pipeline.send_command,
            transport_name=args.data_transport_selection,
            dictionary_name=os.path.basename(str(pipeline.dictionaries.dictionary_path or "")),
            max_events=args.max_events,
        )
        app.run()
    finally:
        disconnect(pipeline)
    return app.return_code or 0


if __name__ == "__main__":
    sys.exit(main())
