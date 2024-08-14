"""

The Remote driver uses the following packet structure.

1 byte for packet type. "D" for data, "M" for meta.
4 byte little endian integer for the size of the payload.
Arbitrary payload.


"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import uuid
from codecs import getincrementaldecoder
from functools import partial
from pathlib import Path
from threading import Event, Thread
from typing import Any, BinaryIO, Literal, TextIO, cast

import msgpack

from .. import events, log, messages
from .._xterm_parser import XTermParser
from ..app import App
from ..driver import Driver
from ..geometry import Size
from ._byte_stream import ByteStream
from ._input_reader import InputReader

WINDOWS = sys.platform == "win32"


class _ExitInput(Exception):
    """Internal exception to force exit of input loop."""


class WebDriver(Driver):
    """A headless driver that may be run remotely."""

    def __init__(
        self,
        app: App[Any],
        *,
        debug: bool = False,
        mouse: bool = True,
        size: tuple[int, int] | None = None,
    ):
        if size is None:
            try:
                width = int(os.environ.get("COLUMNS", 80))
                height = int(os.environ.get("ROWS", 24))
            except ValueError:
                pass
            else:
                size = width, height
        super().__init__(app, debug=debug, mouse=mouse, size=size)
        self.stdout = sys.__stdout__
        self.fileno = sys.__stdout__.fileno()
        self._write = partial(os.write, self.fileno)
        self.exit_event = Event()
        self._key_thread: Thread = Thread(target=self.run_input_thread)
        self._input_reader = InputReader()

        self._deliveries: dict[str, BinaryIO | TextIO] = {}
        """Maps delivery keys to file-like objects, used
        for delivering files to the browser."""

    def write(self, data: str) -> None:
        """Write string data to the output device, which may be piped to
        the controlling process (i.e. textual-web).

        Args:
            data: Raw data.
        """

        data_bytes = data.encode("utf-8")
        self._write(b"D%s%s" % (len(data_bytes).to_bytes(4, "big"), data_bytes))

    def write_meta(self, data: dict[str, object]) -> None:
        """Write a dictionary containing some metadata to stdout, which
        may be piped to the controlling process (i.e. textual-web).

        Args:
            data: Meta dict.
        """
        meta_bytes = json.dumps(data).encode("utf-8", errors="ignore")
        self._write(b"M%s%s" % (len(meta_bytes).to_bytes(4, "big"), meta_bytes))

    def write_packed(self, data: tuple[str | bytes, ...]) -> None:
        """Pack a msgpack compatible data-structure and write to stdout.

        Args:
            data: The dictionary to pack and write.
        """
        packed_bytes = msgpack.packb(data)
        self._write(b"P%s%s" % (len(packed_bytes).to_bytes(4, "big"), packed_bytes))

    def flush(self) -> None:
        pass

    def _enable_mouse_support(self) -> None:
        """Enable reporting of mouse events."""
        write = self.write
        write("\x1b[?1000h")  # SET_VT200_MOUSE
        write("\x1b[?1003h")  # SET_ANY_EVENT_MOUSE
        write("\x1b[?1015h")  # SET_VT200_HIGHLIGHT_MOUSE
        write("\x1b[?1006h")  # SET_SGR_EXT_MODE_MOUSE

    def _enable_bracketed_paste(self) -> None:
        """Enable bracketed paste mode."""
        self.write("\x1b[?2004h")

    def _disable_bracketed_paste(self) -> None:
        """Disable bracketed paste mode."""
        self.write("\x1b[?2004l")

    def _disable_mouse_support(self) -> None:
        """Disable reporting of mouse events."""
        write = self.write
        write("\x1b[?1000l")  #
        write("\x1b[?1003l")  #
        write("\x1b[?1015l")
        write("\x1b[?1006l")

    def _request_terminal_sync_mode_support(self) -> None:
        """Writes an escape sequence to query the terminal support for the sync protocol."""
        self.write("\033[?2026$p")

    def start_application_mode(self) -> None:
        """Start application mode."""

        loop = asyncio.get_running_loop()

        def do_exit() -> None:
            """Callback to force exit."""
            asyncio.run_coroutine_threadsafe(
                self._app._post_message(messages.ExitApp()), loop=loop
            )

        if not WINDOWS:
            for _signal in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(_signal, do_exit)

        self._write(b"__GANGLION__\n")

        self.write("\x1b[?1049h")  # Alt screen
        self._enable_mouse_support()

        self.write("\x1b[?25l")  # Hide cursor
        self.write("\033[?1003h")

        size = Size(80, 24) if self._size is None else Size(*self._size)
        event = events.Resize(size, size)
        asyncio.run_coroutine_threadsafe(
            self._app._post_message(event),
            loop=loop,
        )

        self._request_terminal_sync_mode_support()
        self._enable_bracketed_paste()
        self.flush()
        self._key_thread.start()
        self._app.call_later(self._app.post_message, events.AppBlur())

    def disable_input(self) -> None:
        """Disable further input."""

    def stop_application_mode(self) -> None:
        """Stop application mode, restore state."""
        self.exit_event.set()
        self._input_reader.close()
        self.write_meta({"type": "exit"})

    def run_input_thread(self) -> None:
        """Wait for input and dispatch events."""
        input_reader = self._input_reader
        parser = XTermParser(debug=self._debug)
        utf8_decoder = getincrementaldecoder("utf-8")().decode
        decode = utf8_decoder
        # The server sends us a stream of bytes, which contains the equivalent of stdin, plus
        # in band data packets.
        byte_stream = ByteStream()
        try:
            for data in input_reader:
                for packet_type, payload in byte_stream.feed(data):
                    if packet_type == "D":
                        # Treat as stdin
                        for event in parser.feed(decode(payload)):
                            self.process_event(event)
                    else:
                        # Process meta information separately
                        self._on_meta(packet_type, payload)
        except _ExitInput:
            pass
        except Exception:
            from traceback import format_exc

            log(format_exc())
        finally:
            input_reader.close()

    def _on_meta(self, packet_type: str, payload: bytes) -> None:
        """Private method to dispatch meta.

        Args:
            packet_type: Packet type (currently always "M")
            payload: Meta payload (JSON encoded as bytes).
        """
        payload_map: dict[str, object] = json.loads(payload)
        _type = payload_map.get("type", {})
        if isinstance(_type, str):
            self.on_meta(_type, payload_map)
        else:
            log.error(
                f"Protocol error: type field value is not a string. Value is {_type!r}"
            )

    def on_meta(self, packet_type: str, payload: dict[str, object]) -> None:
        """Process a dictionary containing information received from the controlling process.

        Args:
            packet_type: The type of the packet.
            payload: meta dict.
        """
        if packet_type == "resize":
            self._size = (payload["width"], payload["height"])
            requested_size = Size(*self._size)
            self._app.post_message(events.Resize(requested_size, requested_size))
        elif packet_type == "focus":
            self._app.post_message(events.AppFocus())
        elif packet_type == "blur":
            self._app.post_message(events.AppBlur())
        elif packet_type == "quit":
            self._app.post_message(messages.ExitApp())
        elif packet_type == "exit":
            raise _ExitInput()
        elif packet_type == "deliver_chunk_request":
            # A request from the server to deliver another chunk of a file
            log.debug(f"Deliver chunk request: {payload}")
            try:
                delivery_key = cast(str, payload["key"])
                requested_size = cast(int, payload["size"])
            except KeyError:
                log.error("Protocol error: deliver_chunk_request missing key or size")
                return

            deliveries = self._deliveries

            binary_io: BinaryIO | TextIO | None = None
            try:
                binary_io = deliveries[delivery_key]
            except KeyError:
                log.error(
                    f"Protocol error: deliver_chunk_request invalid key {delivery_key!r}"
                )
            else:
                # Read the requested number of bytes from the file
                try:
                    log.debug(f"Reading {requested_size} bytes from {delivery_key}")
                    chunk = binary_io.read(requested_size)
                    log.debug(f"Delivering chunk {delivery_key!r} of len {len(chunk)}")
                    self.write_packed(("deliver_chunk", delivery_key, chunk))
                    if not chunk:
                        log.info(f"Delivery complete for {delivery_key}")
                        binary_io.close()
                        del deliveries[delivery_key]
                except Exception:
                    binary_io.close()
                    del deliveries[delivery_key]

                    log.error(
                        f"Error delivering file chunk for key {delivery_key!r}. "
                        "Cancelling delivery."
                    )
                    import traceback

                    log.error(str(traceback.format_exc()))

    def open_url(self, url: str, new_tab: bool = True) -> None:
        """Open a URL in the default web browser.

        Args:
            url: The URL to open.
            new_tab: Whether to open the URL in a new tab.
        """
        self.write_meta({"type": "open_url", "url": url, "new_tab": new_tab})

    def deliver_binary(
        self,
        binary: BinaryIO | TextIO,
        *,
        save_path: Path,
        open_method: Literal["browser", "download"] = "download",
        encoding: str | None = None,
        mime_type: str | None = None,
    ) -> None:
        self._deliver_file(
            binary,
            save_path=save_path,
            open_method=open_method,
            encoding=encoding,
            mime_type=mime_type,
        )

    def _deliver_file(
        self,
        binary: BinaryIO | TextIO,
        *,
        save_path: Path,
        open_method: Literal["browser", "download"],
        encoding: str | None = None,
        mime_type: str | None = None,
    ) -> None:
        """Deliver a file to the end-user of the application."""
        binary.seek(0)

        # Generate a unique key for this delivery
        key = str(uuid.uuid4().hex)

        self._deliveries[key] = binary

        # Inform the server that we're starting a new file delivery
        meta: dict[str, object] = {
            "type": "deliver_file_start",
            "key": key,
            "path": str(save_path.resolve()),
            "open_method": open_method,
            "encoding": encoding,
            "mime_type": mime_type,
        }
        self.write_meta(meta)
        log.info(f"Delivering file {meta['path']!r}: {meta!r}")
