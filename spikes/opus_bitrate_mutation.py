#!/usr/bin/env python3
"""Disposable Step 1 proof of live opusenc bitrate mutation over local SRT.

This is evidence tooling, not the TinyPiRelay production media engine.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCHEMA_VERSION = 1
SPIKE_NAME = "opus_runtime_bitrate_mutation_srt_loopback"
BITRATE_SEQUENCE_BPS = (128_000, 192_000, 64_000, 96_000, 128_000)
ELEMENTS = (
    "audiotestsrc",
    "audioconvert",
    "audioresample",
    "capsfilter",
    "opusenc",
    "opusparse",
    "matroskamux",
    "srtsink",
    "srtsrc",
    "matroskademux",
    "queue",
    "opusdec",
    "identity",
    "fakesink",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pipeline_descriptions(port: int = 9103) -> dict[str, str]:
    caps = "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2"
    return {
        "sender": (
            "audiotestsrc is-live=true wave=sine samplesperbuffer=960 "
            f"! audioconvert ! audioresample ! {caps} "
            "! opusenc name=encoder bitrate=128000 ! opusparse "
            "! matroskamux streamable=true "
            f"! srtsink uri=\"srt://127.0.0.1:{port}?mode=caller&latency=200\" "
            "wait-for-connection=true"
        ),
        "receiver": (
            f"srtsrc uri=\"srt://:{port}?mode=listener&latency=200\" "
            "! matroskademux ! queue ! opusparse ! opusdec "
            f"! {caps} ! identity name=receiver_counter signal-handoffs=true silent=true "
            "! fakesink sync=false"
        ),
    }


class BufferTracker:
    def __init__(self, clock_time_none: int):
        self._clock_time_none = clock_time_none
        self._lock = threading.Lock()
        self._count = 0
        self._last_pts_ns: int | None = None
        self._max_gap_ns = 0
        self._missing_pts = 0
        self._non_monotonic_pts = 0

    def record(self, pts_ns: int) -> None:
        with self._lock:
            self._count += 1
            if pts_ns == self._clock_time_none:
                self._missing_pts += 1
                return
            if self._last_pts_ns is not None:
                if pts_ns <= self._last_pts_ns:
                    self._non_monotonic_pts += 1
                else:
                    self._max_gap_ns = max(self._max_gap_ns, pts_ns - self._last_pts_ns)
            self._last_pts_ns = pts_ns

    def on_handoff(self, _identity: Any, buffer: Any) -> None:
        self.record(int(buffer.pts))

    def snapshot(self) -> dict[str, int | None]:
        with self._lock:
            return {
                "buffer_count": self._count,
                "last_pts_ns": self._last_pts_ns,
                "max_gap_ns": self._max_gap_ns,
                "missing_pts": self._missing_pts,
                "non_monotonic_pts": self._non_monotonic_pts,
            }


def load_gstreamer() -> tuple[Any, str]:
    import gi  # Imported only on the target so stdlib tests do not require GI.

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    return Gst, gi.__version__


def element_versions(Gst: Any) -> tuple[dict[str, dict[str, str | None]], list[str]]:
    versions: dict[str, dict[str, str | None]] = {}
    missing: list[str] = []
    for name in ELEMENTS:
        factory = Gst.ElementFactory.find(name)
        if factory is None:
            missing.append(name)
            continue
        plugin = factory.get_plugin()
        versions[name] = {
            "plugin": plugin.get_name() if plugin is not None else None,
            "version": plugin.get_version() if plugin is not None else None,
        }
    return versions, missing


def _state_name(pipeline: Any) -> str:
    _change, current, _pending = pipeline.get_state(0)
    return getattr(current, "value_nick", str(current)).lower()


def _collect_bus_events(
    Gst: Any,
    pipelines: Mapping[str, Any],
    started_monotonic: float,
    bus_events: dict[str, list[dict[str, Any]]],
) -> None:
    mask = Gst.MessageType.ERROR | Gst.MessageType.EOS
    for pipeline_name, pipeline in pipelines.items():
        bus = pipeline.get_bus()
        while True:
            message = bus.timed_pop_filtered(0, mask)
            if message is None:
                break
            source = message.src.get_name() if message.src is not None else None
            elapsed = round(time.monotonic() - started_monotonic, 6)
            if message.type == Gst.MessageType.ERROR:
                error, debug = message.parse_error()
                bus_events["errors"].append(
                    {
                        "pipeline": pipeline_name,
                        "source": source,
                        "elapsed_seconds": elapsed,
                        "message": str(error),
                        "debug": debug,
                    }
                )
            else:
                bus_events["eos"].append(
                    {
                        "pipeline": pipeline_name,
                        "source": source,
                        "elapsed_seconds": elapsed,
                    }
                )


def _wait_for_buffers(
    Gst: Any,
    pipelines: Mapping[str, Any],
    tracker: BufferTracker,
    target_count: int,
    timeout_seconds: float,
    started_monotonic: float,
    bus_events: dict[str, list[dict[str, Any]]],
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        _collect_bus_events(Gst, pipelines, started_monotonic, bus_events)
        snapshot = tracker.snapshot()
        if bus_events["errors"] or bus_events["eos"]:
            return False
        if snapshot["buffer_count"] >= target_count and snapshot["last_pts_ns"] is not None:
            return True
        time.sleep(0.02)
    _collect_bus_events(Gst, pipelines, started_monotonic, bus_events)
    return False


def assess_report(
    report: Mapping[str, Any], min_buffers_per_phase: int, max_gap_ns: int
) -> dict[str, Any]:
    reasons: list[str] = list(report.get("setup_errors", ()))
    initial = report.get("initial") or {}
    transitions = report.get("transitions") or []
    bus = report.get("bus") or {}
    receiver = report.get("receiver") or {}

    expected_targets = list(BITRATE_SEQUENCE_BPS[1:])
    property_ok = (
        initial.get("requested_bps") == BITRATE_SEQUENCE_BPS[0]
        and initial.get("readback_bps") == BITRATE_SEQUENCE_BPS[0]
        and initial.get("sender_state") == "playing"
        and initial.get("receiver_state") == "playing"
        and len(transitions) == len(expected_targets)
    )
    if not property_ok:
        reasons.append("initial bitrate/state or transition count was not demonstrated")

    for index, target in enumerate(expected_targets):
        if index >= len(transitions):
            break
        transition = transitions[index]
        transition_property_ok = (
            transition.get("from_bps") == BITRATE_SEQUENCE_BPS[index]
            and transition.get("requested_bps") == target
            and transition.get("readback_bps") == target
            and transition.get("sender_state_before") == "playing"
            and transition.get("sender_state_after") == "playing"
            and transition.get("receiver_state_before") == "playing"
            and transition.get("receiver_state_after") == "playing"
        )
        if not transition_property_ok:
            property_ok = False
            reasons.append(f"bitrate transition to {target} bps was not read back while PLAYING")

    continuous = bool(initial.get("progress_within_timeout")) and len(transitions) == len(
        expected_targets
    )
    if not continuous:
        reasons.append("receiver was not observed across every bitrate phase")
    for index, transition in enumerate(transitions):
        progressed = (
            transition.get("progress_within_timeout") is True
            and transition.get("buffer_delta", 0) >= min_buffers_per_phase
            and transition.get("pts_advanced") is True
        )
        if not progressed:
            continuous = False
            reasons.append(f"receiver continuity was not demonstrated across transition {index + 1}")

    if bus.get("errors"):
        continuous = False
        reasons.append("GStreamer bus error observed")
    if bus.get("eos"):
        continuous = False
        reasons.append("unexpected EOS observed")
    if receiver:
        if receiver.get("missing_pts", 0):
            continuous = False
            reasons.append("receiver buffers without PTS observed")
        if receiver.get("non_monotonic_pts", 0):
            continuous = False
            reasons.append("non-monotonic receiver PTS observed")
        if receiver.get("max_gap_ns", max_gap_ns + 1) > max_gap_ns:
            continuous = False
            reasons.append("receiver PTS gap exceeded the configured limit")
    elif not report.get("setup_errors"):
        continuous = False
        reasons.append("receiver summary is missing")
    if report.get("setup_errors"):
        property_ok = False
        continuous = False

    return {
        "status": "pass" if property_ok and continuous else "fail",
        "property_changes_demonstrated": property_ok,
        "continuous_receive_demonstrated": continuous,
        "reasons": list(dict.fromkeys(reasons)),
    }


def _base_report(args: argparse.Namespace) -> dict[str, Any]:
    descriptions = pipeline_descriptions(args.port)
    return {
        "schema_version": SCHEMA_VERSION,
        "spike": SPIKE_NAME,
        "step": 1,
        "production_component": False,
        "started_at_utc": utc_now(),
        "ended_at_utc": None,
        "duration_seconds": None,
        "platform": {
            "hostname": platform.node(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "configuration": {
            "port": args.port,
            "source_caps": "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2",
            "bitrate_sequence_bps": list(BITRATE_SEQUENCE_BPS),
            "min_buffers_per_phase": args.buffers_per_phase,
            "phase_timeout_seconds": args.phase_timeout,
            "max_pts_gap_ms": args.max_pts_gap_ms,
        },
        "pipelines": descriptions,
        "versions": {
            "pygobject": None,
            "gstreamer": None,
            "elements": {},
        },
        "setup_errors": [],
        "initial": None,
        "transitions": [],
        "receiver": {},
        "bus": {"errors": [], "eos": []},
        "result": None,
    }


def run_spike(args: argparse.Namespace, Gst: Any, report: dict[str, Any]) -> None:
    versions, missing = element_versions(Gst)
    report["versions"]["elements"] = versions
    if missing:
        raise RuntimeError(f"missing GStreamer elements: {', '.join(missing)}")

    sender = receiver = None
    started_monotonic = time.monotonic()
    tracker = BufferTracker(int(Gst.CLOCK_TIME_NONE))
    try:
        sender = Gst.parse_launch(report["pipelines"]["sender"])
        receiver = Gst.parse_launch(report["pipelines"]["receiver"])
        encoder = sender.get_by_name("encoder")
        counter = receiver.get_by_name("receiver_counter")
        if encoder is None or counter is None:
            raise RuntimeError("named encoder or receiver counter was not created")
        counter.connect("handoff", tracker.on_handoff)
        pipelines = {"sender": sender, "receiver": receiver}

        if receiver.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("receiver failed to enter PLAYING")
        if sender.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("sender failed to enter PLAYING")

        initial_progress = _wait_for_buffers(
            Gst,
            pipelines,
            tracker,
            args.buffers_per_phase,
            args.phase_timeout,
            started_monotonic,
            report["bus"],
        )
        report["initial"] = {
            "requested_bps": BITRATE_SEQUENCE_BPS[0],
            "readback_bps": int(encoder.get_property("bitrate")),
            "sender_state": _state_name(sender),
            "receiver_state": _state_name(receiver),
            "progress_within_timeout": initial_progress,
            "receiver": tracker.snapshot(),
        }
        if not initial_progress:
            _collect_bus_events(Gst, pipelines, started_monotonic, report["bus"])
            return

        previous = BITRATE_SEQUENCE_BPS[0]
        for target in BITRATE_SEQUENCE_BPS[1:]:
            before = tracker.snapshot()
            transition_started = time.monotonic()
            transition = {
                "from_bps": previous,
                "requested_bps": target,
                "sender_state_before": _state_name(sender),
                "receiver_state_before": _state_name(receiver),
                "before": before,
            }
            encoder.set_property("bitrate", target)
            transition["readback_bps"] = int(encoder.get_property("bitrate"))
            progress = _wait_for_buffers(
                Gst,
                pipelines,
                tracker,
                int(before["buffer_count"]) + args.buffers_per_phase,
                args.phase_timeout,
                started_monotonic,
                report["bus"],
            )
            after = tracker.snapshot()
            before_pts = before["last_pts_ns"]
            after_pts = after["last_pts_ns"]
            transition.update(
                {
                    "progress_within_timeout": progress,
                    "after": after,
                    "buffer_delta": int(after["buffer_count"]) - int(before["buffer_count"]),
                    "pts_advanced": (
                        before_pts is not None
                        and after_pts is not None
                        and int(after_pts) > int(before_pts)
                    ),
                    "sender_state_after": _state_name(sender),
                    "receiver_state_after": _state_name(receiver),
                    "elapsed_seconds": round(time.monotonic() - transition_started, 6),
                }
            )
            report["transitions"].append(transition)
            previous = target
            if not progress or report["bus"]["errors"] or report["bus"]["eos"]:
                break
        _collect_bus_events(Gst, pipelines, started_monotonic, report["bus"])
    finally:
        report["receiver"] = tracker.snapshot()
        if sender is not None:
            sender.set_state(Gst.State.NULL)
        if receiver is not None:
            receiver.set_state(Gst.State.NULL)


def _write_report(report: Mapping[str, Any], output: Path | None) -> None:
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if output is not None:
        output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9103)
    parser.add_argument("--buffers-per-phase", type=int, default=25)
    parser.add_argument("--phase-timeout", type=float, default=15.0)
    parser.add_argument("--max-pts-gap-ms", type=float, default=60.0)
    parser.add_argument("--output", type=Path)
    return parser


def main(
    argv: Sequence[str] | None = None,
    gst_loader: Callable[[], tuple[Any, str]] = load_gstreamer,
) -> int:
    args = build_parser().parse_args(argv)
    if (
        not 1 <= args.port <= 65_535
        or args.buffers_per_phase < 1
        or args.phase_timeout <= 0
        or args.max_pts_gap_ms <= 0
    ):
        build_parser().error("port and timing/count arguments must be positive and valid")

    report = _base_report(args)
    started = time.monotonic()
    setup_failed = False
    try:
        Gst, pygobject_version = gst_loader()
        report["versions"]["pygobject"] = pygobject_version
        report["versions"]["gstreamer"] = Gst.version_string()
        run_spike(args, Gst, report)
    except (Exception, KeyboardInterrupt) as exc:
        # The JSON report is the diagnostic boundary for this spike.
        setup_failed = True
        report["setup_errors"].append(f"{type(exc).__name__}: {exc}")
    report["ended_at_utc"] = utc_now()
    report["duration_seconds"] = round(time.monotonic() - started, 6)
    report["result"] = assess_report(
        report,
        args.buffers_per_phase,
        int(args.max_pts_gap_ms * 1_000_000),
    )
    _write_report(report, args.output)
    if report["result"]["status"] == "pass":
        return 0
    return 2 if setup_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
