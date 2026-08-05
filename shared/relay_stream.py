"""Live view of a remote session, rendered as it is produced.

The bridge's `/stream` endpoint is server-sent events carrying every child
event, including the `stream_event` deltas that arrive token by token. This
turns that firehose into something readable in a terminal.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request

from relay_config import RelayError, VERSION, resolve

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


class Streamer:
    """Tails one host's event stream on a background thread.

    Used both by `watch --stream` and by `converse --stream`, where a streamer
    is attached to whichever machine currently holds the turn.
    """

    def __init__(self, host: str, label: str | None = None, out=sys.stdout,
                 colour: bool = True, show_thinking: bool = False) -> None:
        self.host = host
        self.label = label or host
        self.out = out
        self.colour = colour and out.isatty()
        self.show_thinking = show_thinking
        self._response = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._line_open = False
        self._block_kind: dict[int, str] = {}

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> Streamer:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        # Closing the response is what unblocks the reader mid-read.
        if self._response is not None:
            try:
                self._response.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)
        self._end_line()

    def __enter__(self) -> Streamer:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---- plumbing --------------------------------------------------------

    def _run(self) -> None:
        name, entry = resolve(self.host)
        request = urllib.request.Request(
            entry["url"].rstrip("/") + "/stream",
            headers={"Authorization": f"Bearer {entry.get('token', '')}",
                     "Accept": "text/event-stream",
                     "User-Agent": f"claude-remote-relay/{VERSION}"},
        )
        try:
            self._response = urllib.request.urlopen(request, timeout=None)
            for raw in self._response:
                if self._stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if not line.startswith("data: "):
                    continue  # comment frames are keepalives
                try:
                    item = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                # Frames are double-wrapped: the bridge's envelope carries the
                # child event, and a `stream_event` child carries the delta.
                child = item.get("event") or {}
                if child.get("type") == "stream_event":
                    self.render(child.get("event") or {})
                else:
                    self.render_turn_event(child)
        except Exception:
            if not self._stop.is_set():
                self._write(f"\n{self._dim(f'[{self.label}: stream disconnected]')}\n")

    # ---- rendering -------------------------------------------------------

    def _dim(self, text: str) -> str:
        return f"{DIM}{text}{RESET}" if self.colour else text

    def _write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def _begin_line(self) -> None:
        if not self._line_open:
            prefix = f"{BOLD}{self.label}{RESET} │ " if self.colour else f"{self.label} | "
            self._write(f"\n{prefix}")
            self._line_open = True

    def _end_line(self) -> None:
        if self._line_open:
            self._write("\n")
            self._line_open = False

    def render_turn_event(self, event: dict) -> None:
        """Whole-turn events: only the end-of-turn accounting is worth showing."""
        if event.get("type") != "result":
            return
        self._end_line()
        cost = event.get("total_cost_usd") or 0.0
        state = "interrupted" if event.get("is_error") else "done"
        self._write(self._dim(
            f"  [{self.label} {state}: {(event.get('duration_ms') or 0) / 1000:.1f}s, "
            f"${cost:.4f}]\n"))

    def render(self, event: dict) -> None:
        kind = event.get("type")

        if kind == "content_block_start":
            block = event.get("content_block") or {}
            self._block_kind[event.get("index", 0)] = block.get("type", "text")
            if block.get("type") == "tool_use":
                self._end_line()
                self._write(self._dim(f"  · {self.label} runs {block.get('name', '?')}\n"))

        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                self._begin_line()
                self._write(delta.get("text", ""))
            elif delta.get("type") == "thinking_delta" and self.show_thinking:
                self._begin_line()
                self._write(self._dim(delta.get("thinking", "")))

        elif kind == "content_block_stop":
            if self._block_kind.pop(event.get("index", 0), "text") == "text":
                self._end_line()

        elif kind == "message_stop":
            self._end_line()


def watch(host: str, show_thinking: bool = False) -> int:
    """Block, rendering a host's stream until interrupted."""
    streamer = Streamer(host, show_thinking=show_thinking).start()
    try:
        while streamer._thread and streamer._thread.is_alive():
            streamer._thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        streamer.stop()
    return 0
