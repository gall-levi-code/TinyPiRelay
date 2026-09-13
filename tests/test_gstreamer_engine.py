from __future__ import annotations

import unittest
import threading
from dataclasses import replace
from types import SimpleNamespace

from tinypirelay.gstreamer_engine import (
    DEFAULT_EOS_TIMEOUT_SECONDS,
    FLAC_ID,
    OPUS_ID,
    PCM_ID,
    RECORDING_QUEUE_NS,
    EngineError,
    GStreamerEngine,
    _allowed_structure_fields,
    _normalize_telemetry,
    capture_plan,
    monitoring_branch_plan,
    recording_branch_plan,
    stream_branch_plan,
)
from tinypirelay.media_config import (
    CaptureConfig,
    CaptureMode,
    MediaConfig,
    MonitoringConfig,
    RecordingConfig,
    StreamConfig,
)


def config(
    representation: str = OPUS_ID,
    *,
    capture_format: str = "S16LE",
    passphrase: str | None = None,
    monitoring_updates: float = 5.0,
) -> MediaConfig:
    return MediaConfig(
        1,
        CaptureConfig("hw:CARD=iMM6C,DEV=0", CaptureMode(capture_format, 48_000, 1)),
        StreamConfig(
            True,
            representation,
            "192.168.1.174",
            4001,
            200,
            "live_birdmic",
            passphrase,
            128_000,
        ),
        RecordingConfig("/var/lib/tinypirelay/recordings", 3600, None),
        MonitoringConfig(monitoring_updates, 512),
    )


def factories(plan: object) -> list[str]:
    return [element.factory for element in plan.elements]


def element(plan: object, name: str):
    return next(value for value in plan.elements if value.name == name)


class FakeStructure:
    def __init__(self, values: dict[str, object]):
        self.values = values

    def n_fields(self) -> int:
        return len(self.values)

    def nth_field_name(self, index: int) -> str:
        return tuple(self.values)[index]

    def get_value(self, name: str) -> object:
        return self.values[name]


class FakeGValue:
    def __init__(self, value: object) -> None:
        self.value = value

    def get_value(self) -> object:
        return self.value


class FakeValueArray:
    def __init__(self, values: list[object]) -> None:
        self.values = [FakeGValue(value) for value in values]
        self.n_values = len(values)

    def get_nth(self, index: int) -> FakeGValue:
        return self.values[index]


class TypedSpectrumStructure:
    def __init__(self) -> None:
        self.generic_reads: list[str] = []

    def get_clock_time(self, name: str):
        if name == "timestamp":
            return True, 1 << 40
        if name == "running-time":
            return True, (1 << 64) - 1
        raise OverflowError("cannot fit C long")

    def get_list(self, name: str):
        if name != "magnitude":
            return False, None
        return True, FakeValueArray([-80.0, -21.5])

    def get_value(self, name: str) -> object:
        self.generic_reads.append(name)
        raise TypeError("unknown type GstValueList")


class TypedStatsStructure:
    fields = ("bytes-sent-total", "rtt-ms", "send-rate-mbps")

    def __init__(self) -> None:
        self.accessor_calls: list[tuple[str, str]] = []

    def n_fields(self) -> int:
        return len(self.fields)

    def nth_field_name(self, index: int) -> str:
        return self.fields[index]

    def get_field_type(self, name: str):
        type_name = "guint64" if name == "bytes-sent-total" else "gdouble"
        return SimpleNamespace(name=type_name)

    def get_uint64(self, name: str):
        self.accessor_calls.append(("get_uint64", name))
        return (True, (1 << 63) + 7) if name == "bytes-sent-total" else (False, 0)

    def get_double(self, name: str):
        self.accessor_calls.append(("get_double", name))
        if name == "rtt-ms":
            return True, 2.5
        if name == "send-rate-mbps":
            return True, float("inf")
        return False, 0.0

    def get_value(self, _name: str) -> object:
        raise AssertionError("typed GstStructure fields must not use get_value")


class FakeSource:
    def __init__(self) -> None:
        self.callback = None
        self.context = None

    def set_callback(self, callback) -> None:
        self.callback = callback

    def attach(self, context) -> None:
        self.context = context


class FakeGLib:
    def __init__(self) -> None:
        self.sources: list[FakeSource] = []

    def idle_source_new(self) -> FakeSource:
        source = FakeSource()
        self.sources.append(source)
        return source

    def timeout_source_new(self, _milliseconds: int) -> FakeSource:
        source = FakeSource()
        self.sources.append(source)
        return source


class GStreamerPlanTests(unittest.TestCase):
    def test_recording_finalize_timeout_exceeds_queue_bound(self) -> None:
        self.assertGreater(
            DEFAULT_EOS_TIMEOUT_SECONDS,
            RECORDING_QUEUE_NS / 1_000_000_000,
        )

    def test_import_and_construction_do_not_load_gi(self) -> None:
        called = False

        def loader():
            nonlocal called
            called = True
            raise AssertionError("loader should remain lazy")

        GStreamerEngine(config(), lambda _event: None, gst_loader=loader)
        self.assertFalse(called)

    def test_owner_binding_accepts_worker_commands_without_opening_capture(self) -> None:
        glib = FakeGLib()
        glib.MainContext = SimpleNamespace(
            get_thread_default=lambda: None, default=lambda: "owner"
        )
        engine = GStreamerEngine(config(), lambda _event: None, gst_loader=lambda: (object(), glib))
        engine.bind_owner()
        futures = []
        worker = threading.Thread(target=lambda: futures.append(engine.invoke(engine.update_config, config())))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(glib.sources))
        self.assertFalse(futures[0].done())
        glib.sources[0].callback(None)
        self.assertIsNone(futures[0].result(timeout=1))
        self.assertIsNone(engine._pipeline)

    def test_capture_plan_is_native_caps_to_allow_not_linked_tee(self) -> None:
        plan = capture_plan(config())
        self.assertEqual(
            factories(plan), ["alsasrc", "capsfilter", "tee"]
        )
        self.assertEqual(
            plan.caps,
            "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=1",
        )
        self.assertEqual(
            element(plan, "capture_source").public_property_map()["device"],
            "hw:CARD=iMM6C,DEV=0",
        )
        self.assertIs(
            element(plan, "capture_tee").public_property_map()["allow-not-linked"],
            True,
        )

    def test_synthetic_source_is_an_explicit_injection(self) -> None:
        empty_capture = replace(config(), capture=CaptureConfig(None, None))
        plan = capture_plan(empty_capture, "audiotestsrc")
        self.assertEqual(factories(plan)[0], "audiotestsrc")
        self.assertEqual(plan.caps, "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2")
        with self.assertRaises(EngineError):
            capture_plan(empty_capture, "alsasrc")

    def test_exact_stream_factories_match_evidence(self) -> None:
        expected = {
            PCM_ID: [
                "queue", "audioconvert", "audioresample", "capsfilter",
                "matroskamux", "valve", "srtsink",
            ],
            FLAC_ID: [
                "queue", "audioconvert", "audioresample", "capsfilter",
                "flacenc", "matroskamux", "valve", "srtsink",
            ],
            OPUS_ID: [
                "queue", "audioconvert", "audioresample", "capsfilter",
                "opusenc", "opusparse", "matroskamux", "valve", "srtsink",
            ],
        }
        for representation, wanted in expected.items():
            with self.subTest(representation=representation):
                plan = stream_branch_plan(config(representation))
                self.assertEqual(factories(plan), wanted)
                queue = element(plan, "stream_queue").public_property_map()
                self.assertEqual(queue["leaky"], 0)
                self.assertGreater(queue["max-size-time"], 0)
                self.assertIs(
                    element(plan, "stream_mux").public_property_map()["streamable"],
                    True,
                )
                self.assertIs(
                    element(plan, "stream_transport_valve").public_property_map()[
                        "drop"
                    ],
                    False,
                )
                self.assertIs(
                    element(plan, "stream_srt_sink").public_property_map()["async"],
                    False,
                )

    def test_srt_properties_are_caller_managed_and_secrets_are_not_rendered(self) -> None:
        secret = "correct-horse-battery"
        plan = stream_branch_plan(config(passphrase=secret))
        sink = element(plan, "stream_srt_sink")
        props = sink.public_property_map()
        self.assertEqual(props["uri"], "srt://192.168.1.174:4001")
        self.assertEqual(props["mode"], 1)
        self.assertEqual(props["streamid"], "live_birdmic")
        self.assertIs(props["auto-reconnect"], False)
        self.assertIs(props["wait-for-connection"], True)
        self.assertIs(props["async"], False)
        self.assertEqual(props["pbkeylen"], 16)
        self.assertNotIn("passphrase", props)
        self.assertEqual(dict(sink.secret_properties)["passphrase"], secret)
        self.assertNotIn(secret, repr(plan))

    def test_stream_plan_rejects_unapproved_representation_and_uri_injection(self) -> None:
        with self.assertRaises(EngineError):
            stream_branch_plan(config("experimental"))
        bad = replace(
            config(),
            stream=replace(config().stream, destination_host="host?passphrase=leak"),
        )
        with self.assertRaises(EngineError):
            stream_branch_plan(bad)

    def test_recording_is_native_flac_and_true_s32_fails_closed(self) -> None:
        plan = recording_branch_plan(config(), "/safe/one.flac")
        self.assertEqual(factories(plan), ["queue", "flacenc", "valve", "filesink"])
        self.assertIs(element(plan, "recording_io_valve").public_property_map()["drop"], False)
        self.assertEqual(
            element(plan, "recording_queue").public_property_map()["leaky"], 0
        )
        sink = element(plan, "recording_file_sink").public_property_map()
        self.assertIs(sink["sync"], False)
        self.assertIs(sink["async"], False)
        with self.assertRaises(EngineError):
            recording_branch_plan(config(capture_format="S32LE"), "/safe/two.flac")

    def test_monitoring_queues_are_bounded_and_leaky(self) -> None:
        level = monitoring_branch_plan(config(), "level")
        spectrum = monitoring_branch_plan(config(), "spectrum")
        for plan in (level, spectrum):
            queue = element(plan, f"{plan.kind}_queue").public_property_map()
            self.assertEqual(queue["max-size-buffers"], 4)
            self.assertEqual(queue["max-size-time"], 0)
            self.assertEqual(queue["leaky"], 2)
            sink = element(plan, f"{plan.kind}_sink").public_property_map()
            self.assertIs(sink["sync"], False)
            self.assertIs(sink["async"], False)
        spectrum_props = element(spectrum, "spectrum_analyzer").public_property_map()
        level_props = element(level, "level_analyzer").public_property_map()
        self.assertEqual(level_props["interval"], 200_000_000)
        self.assertEqual(spectrum_props["bands"], 512)
        self.assertEqual(spectrum_props["interval"], 200_000_000)
        self.assertIs(spectrum_props["message-phase"], False)

        fast_level = monitoring_branch_plan(config(monitoring_updates=60), "level")
        fast_spectrum = monitoring_branch_plan(config(monitoring_updates=60), "spectrum")
        self.assertEqual(
            element(fast_level, "level_analyzer").public_property_map()["interval"],
            16_666_666,
        )
        self.assertEqual(
            element(fast_spectrum, "spectrum_analyzer").public_property_map()["interval"],
            16_666_666,
        )


class GStreamerNormalizationTests(unittest.TestCase):
    def test_stats_are_primitive_and_allow_listed(self) -> None:
        structure = FakeStructure(
            {
                "bytes-sent-total": 123,
                "rtt-ms": 2.5,
                "unknown-secret": "no",
                "packets-sent-total": object(),
            }
        )
        self.assertEqual(
            _allowed_structure_fields(
                structure, frozenset(("bytes-sent-total", "rtt-ms", "packets-sent-total"))
            ),
            {"bytes-sent-total": 123, "rtt-ms": 2.5},
        )

    def test_level_and_spectrum_messages_are_normalized(self) -> None:
        common = {"timestamp": 1, "running-time": 2, "duration": 3}
        level = _normalize_telemetry(
            "level",
            FakeStructure(
                common
                | {
                    "rms": [-30.0, -20.0],
                    "peak": [-1.0, 0.0],
                    "decay": [-2.0, -0.5],
                }
            ),
        )
        self.assertEqual(level["clipped"], [False, True])
        self.assertEqual(level["rms_db"], [-30.0, -20.0])
        spectrum = _normalize_telemetry(
            "spectrum", FakeStructure(common | {"magnitude": [-80.0, -21.5]})
        )
        self.assertEqual(spectrum["magnitude_db"], [-80.0, -21.5])

    def test_gst_value_list_and_clock_times_use_typed_accessors(self) -> None:
        structure = TypedSpectrumStructure()

        spectrum = _normalize_telemetry("spectrum", structure)

        self.assertEqual(spectrum["magnitude_db"], [-80.0, -21.5])
        self.assertEqual(spectrum["timestamp_ns"], 1 << 40)
        self.assertIsNone(spectrum["running_time_ns"])
        self.assertIsNone(spectrum["duration_ns"])
        self.assertEqual(structure.generic_reads, [])

    def test_typed_stats_preserve_uint64_and_drop_nonfinite_values(self) -> None:
        structure = TypedStatsStructure()
        self.assertEqual(
            _allowed_structure_fields(
                structure,
                frozenset(TypedStatsStructure.fields),
            ),
            {"bytes-sent-total": (1 << 63) + 7, "rtt-ms": 2.5},
        )
        self.assertEqual(
            structure.accessor_calls,
            [
                ("get_uint64", "bytes-sent-total"),
                ("get_double", "rtt-ms"),
                ("get_double", "send-rate-mbps"),
            ],
        )

    def test_element_message_marshalling_error_never_escapes_bus_callback(self) -> None:
        class BrokenStructure:
            def get_name(self) -> str:
                raise SystemError("GI exception state")

        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._Gst = SimpleNamespace(
            MessageType=SimpleNamespace(ELEMENT=1),
        )
        engine._latest = {"level": None, "spectrum": None}
        engine._emit = lambda *_args, **_kwargs: self.fail(
            "malformed telemetry must be dropped"
        )
        message = SimpleNamespace(type=1, get_structure=BrokenStructure)

        engine._on_bus_message(None, message)

    def test_finalizing_monitor_messages_cannot_repopulate_cleared_telemetry(
        self,
    ) -> None:
        class SpectrumStructure(FakeStructure):
            def get_name(self) -> str:
                return "spectrum"

        stale, current = object(), object()
        stale_source, current_source = object(), object()
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._Gst = SimpleNamespace(MessageType=SimpleNamespace(ELEMENT=1))
        engine._active = {"spectrum": current}
        engine._latest = {"level": None, "spectrum": None}
        engine._branch_for_object = lambda source: (
            stale if source is stale_source else current
        )
        emitted: list[object] = []
        engine._emit = lambda *_args, **details: emitted.append(details)
        structure = SpectrumStructure(
            {
                "timestamp": 1,
                "running-time": 2,
                "duration": 3,
                "magnitude": [-40.0],
            }
        )

        engine._handle_bus_message(
            SimpleNamespace(type=1, src=stale_source, get_structure=lambda: structure)
        )
        self.assertIsNone(engine._latest["spectrum"])
        self.assertEqual([], emitted)

        engine._handle_bus_message(
            SimpleNamespace(type=1, src=current_source, get_structure=lambda: structure)
        )
        self.assertEqual([-40.0], engine._latest["spectrum"]["magnitude_db"])
        self.assertEqual(1, len(emitted))


class GStreamerLifecycleContractTests(unittest.TestCase):
    def test_capture_start_attaches_level_but_not_unleased_spectrum(self) -> None:
        class Element:
            def set_property(self, _name, _value) -> None:
                pass

        pipeline = SimpleNamespace(
            add=lambda _element: None,
            set_state=lambda _state: "playing",
        )
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._config = config()
        engine._source_factory = "audiotestsrc"
        engine._Gst = SimpleNamespace(
            Pipeline=SimpleNamespace(new=lambda _name: pipeline),
            Caps=SimpleNamespace(from_string=lambda value: value),
            State=SimpleNamespace(PLAYING="playing"),
            StateChangeReturn=SimpleNamespace(FAILURE="failure"),
        )
        engine._make = lambda _spec: Element()
        engine._link_many = lambda _elements: None
        engine._install_bus_watch = lambda: None
        attached: list[tuple[str, int]] = []
        engine._try_add_monitoring_branch = (
            lambda kind, generation: attached.append((kind, generation))
        )

        engine._start_capture_attempt(7)

        self.assertEqual([("level", 7)], attached)

    def test_spectrum_activation_is_idempotent_and_removes_only_spectrum(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._config = config()
        engine._pipeline = object()
        engine._tee = object()
        engine._capture_generation = 3
        engine._require_owner = lambda: None
        engine._latest = {"level": {"rms": -20.0}, "spectrum": {"magnitude": []}}
        stream = SimpleNamespace(kind="stream")
        level = SimpleNamespace(kind="level")
        engine._active = {"stream": stream, "level": level}
        attached: list[str] = []

        def attach(kind: str, generation: int) -> None:
            attached.append(kind)
            engine._active[kind] = SimpleNamespace(kind=kind, generation=generation)

        removed: list[str] = []

        def remove(kind: str, *, drain: bool) -> None:
            self.assertFalse(drain)
            removed.append(kind)
            engine._active.pop(kind, None)

        engine._try_add_monitoring_branch = attach
        engine._begin_remove = remove

        self.assertTrue(engine.set_spectrum_active(True))
        self.assertTrue(engine.set_spectrum_active(True))
        self.assertEqual(["spectrum"], attached)

        self.assertFalse(engine.set_spectrum_active(False))
        self.assertFalse(engine.set_spectrum_active(False))
        self.assertEqual(["spectrum"], removed)
        self.assertEqual({"stream", "level"}, set(engine._active))
        self.assertIsNone(engine._latest["spectrum"])

    def test_live_config_accepts_only_recording_and_opus_bitrate(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._require_owner = lambda: None
        engine._config = config()
        engine._active = {}
        engine._finalizing = {}
        candidate = replace(
            engine._config,
            stream=replace(engine._config.stream, opus_bitrate_bps=96_000),
            recording=replace(engine._config.recording, rotation_seconds=120),
        )

        engine.update_config(candidate)

        self.assertEqual(candidate, engine._config)
        changed_endpoint = replace(
            candidate,
            stream=replace(candidate.stream, destination_port=5000),
        )
        with self.assertRaisesRegex(EngineError, "requires a media-service restart"):
            engine.update_config(changed_endpoint)

    def test_synchronous_repeated_idle_probe_schedules_one_drain(self) -> None:
        class TeePad:
            def __init__(self) -> None:
                self.callback = None
                self.results: list[object] = []

            def add_probe(self, _probe_type, callback) -> int:
                self.callback = callback
                self.results.append(callback(self, None))
                self.results.append(callback(self, None))
                return 73

        branch = SimpleNamespace(
            token=44,
            tee_pad=TeePad(),
            timeout_source=None,
            block_probe_id=None,
            drain_scheduled=False,
            drain_lock=threading.Lock(),
        )
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._finalizing = {}
        engine._eos_timeout_seconds = 3.0
        engine._Gst = SimpleNamespace(
            PadProbeType=SimpleNamespace(IDLE="idle"),
            PadProbeReturn=SimpleNamespace(REMOVE="remove"),
        )
        engine._new_timeout = lambda _seconds, _callback: object()
        scheduled: list[tuple[object, ...]] = []
        engine._dispatch_owner_async = lambda callback, *args: scheduled.append(
            (callback, *args)
        )

        engine._begin_remove_handle(branch, drain=True)
        branch.tee_pad.results.append(branch.tee_pad.callback(branch.tee_pad, None))

        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][1:], (44, True))
        self.assertEqual(branch.tee_pad.results, ["remove", "remove", "remove"])
        self.assertIsNone(branch.block_probe_id)

    def test_extended_message_check_avoids_32_bit_gi_enum_member(self) -> None:
        class MessageTypes:
            ELEMENT = 1
            STATE_CHANGED = 2
            ERROR = 3
            EOS = 4

            @property
            def DEVICE_REMOVED(self):
                raise OverflowError("Python int too large to convert to C long")

        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._Gst = SimpleNamespace(MessageType=MessageTypes())
        failures: list[int] = []
        engine._fail_capture = lambda generation, **_details: failures.append(generation)
        engine._capture_generation = 9

        engine._on_bus_message(None, SimpleNamespace(type=2048))
        engine._on_bus_message(None, SimpleNamespace(type=(1 << 31) + 2))

        self.assertEqual(failures, [9])

    def test_branch_eos_enters_real_queue_sink_and_leaves_encoder_src(self) -> None:
        class ProbePad:
            def __init__(self) -> None:
                self.callback = None

            def add_probe(self, _probe_type, callback) -> int:
                self.callback = callback
                return 41

        class EntryPad:
            def __init__(self) -> None:
                self.events: list[object] = []

            def send_event(self, event: object) -> bool:
                self.events.append(event)
                return True

        probe_pad = ProbePad()
        entry_pad = EntryPad()
        ghost = SimpleNamespace(
            send_event=lambda _event: self.fail("EOS must not enter the unlinked ghost pad")
        )
        branch = SimpleNamespace(
            token=31,
            kind="recording",
            plan=SimpleNamespace(eos_probe_element="encoder"),
            elements={"encoder": SimpleNamespace(get_static_pad=lambda _name: probe_pad)},
            entry_sink=entry_pad,
            ghost_sink=ghost,
            eos_probe_id=None,
            eos_probe_pad=None,
        )
        eos = object()
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._finalizing = {31: branch}
        engine._unlink_branch = lambda _branch: None
        engine._Gst = SimpleNamespace(
            PadProbeType=SimpleNamespace(EVENT_DOWNSTREAM=1),
            PadProbeReturn=SimpleNamespace(DROP="drop", OK="ok"),
            EventType=SimpleNamespace(EOS="eos"),
            Event=SimpleNamespace(new_eos=lambda: eos),
        )
        finished: list[tuple[int, bool]] = []
        engine._finish_remove = lambda token, forced=False: finished.append(
            (token, forced)
        )
        engine._dispatch_owner_async = lambda callback, *args: callback(*args)

        engine._unlink_and_drain(31, True)
        result = probe_pad.callback(
            probe_pad,
            SimpleNamespace(get_event=lambda: SimpleNamespace(type="eos")),
        )

        self.assertEqual(entry_pad.events, [eos])
        self.assertEqual(finished, [(31, False)])
        self.assertEqual(result, "drop")

    def test_glib_source_callbacks_accept_user_data_argument(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._owner_thread = -1
        engine._context = object()
        engine._GLib = FakeGLib()

        future = engine.invoke(lambda value: value + 1, 4)
        engine._GLib.sources.pop(0).callback(None)
        self.assertEqual(future.result(), 5)

        calls: list[str] = []
        engine._dispatch_owner(lambda: calls.append("owner"))
        engine._GLib.sources.pop(0).callback(None)
        engine._dispatch_owner_async(lambda: calls.append("async"))
        engine._GLib.sources.pop(0).callback(None)
        engine._new_timeout(0.1, lambda: calls.append("timeout") or False)
        self.assertIs(engine._GLib.sources.pop(0).callback(None), False)
        self.assertEqual(calls, ["owner", "async", "timeout"])

    def test_capture_failure_during_shutdown_still_completes_shutdown(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._capture_start_pending_generation = 5
        engine._capture_stop_pending = True
        engine._shutdown_pending = True
        order: list[str] = []
        engine._force_all_branches = lambda: order.append("branches-null")
        engine._force_pipeline_null = lambda: order.append("pipeline-null")
        engine._emit = lambda kind, _scope, _generation, **_details: order.append(kind)

        engine._fail_capture(5, code="device_lost", message="gone")

        self.assertEqual(
            order,
            [
                "branches-null",
                "pipeline-null",
                "error",
                "capture-stopped",
                "shutdown-complete",
            ],
        )
        self.assertFalse(engine._shutdown_pending)

    def test_repeated_stream_start_does_not_rebuild_connecting_branch(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._active = {"stream": SimpleNamespace(generation=8, connected=False)}
        engine._require_capture = lambda: None
        engine._begin_remove = lambda *_args, **_kwargs: self.fail(
            "connecting branch must remain attached"
        )
        engine._add_branch = lambda *_args, **_kwargs: self.fail(
            "connecting branch must not be duplicated"
        )

        self.assertEqual(engine.start_stream(generation=8), 8)

    def test_abort_stream_attempt_is_nondraining_even_at_connection_boundary(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        branch = SimpleNamespace(generation=8, connected=False)
        engine._active = {"stream": branch}
        engine._require_owner = lambda: None
        removals: list[tuple[str, bool]] = []
        engine._begin_remove = lambda kind, *, drain: removals.append((kind, drain))

        self.assertEqual(engine.abort_stream_attempt(generation=8), 8)
        self.assertEqual(removals, [("stream", False)])

        # Runtime's generation/token-safe timeout is authoritative.  A late
        # caller-added callback may set this flag before runtime receives its
        # queued connected event, and must not leave the timed-out branch live.
        branch.connected = True
        self.assertEqual(engine.abort_stream_attempt(generation=8), 8)
        self.assertEqual(removals, [("stream", False), ("stream", False)])

        with self.assertRaisesRegex(EngineError, "generation"):
            engine.abort_stream_attempt(generation=9)
        self.assertEqual(removals, [("stream", False), ("stream", False)])

    def test_normal_stream_stop_retains_ordered_drain(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._active = {
            "stream": SimpleNamespace(generation=8, connected=False),
        }
        engine._require_owner = lambda: None
        removals: list[tuple[str, bool]] = []
        engine._begin_remove = lambda kind, *, drain: removals.append((kind, drain))

        self.assertEqual(engine.stop_stream(generation=8), 8)
        self.assertEqual(removals, [("stream", True)])

    def test_monitor_attach_failure_isolated_from_capture(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._config = config()
        engine._add_branch = lambda _plan, _generation: (_ for _ in ()).throw(
            EngineError("analyzer missing")
        )
        emitted: list[tuple[str, str, int, dict[str, object]]] = []
        engine._emit = lambda kind, scope, generation, **details: emitted.append(
            (kind, scope, generation, details)
        )

        engine._try_add_monitoring_branch("level", 2)

        self.assertEqual(emitted[0][:3], ("error", "level", 2))
        self.assertEqual(emitted[0][3]["code"], "branch_start_failed")

    def test_stream_overrun_reports_loss_before_physical_removal(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        branch = SimpleNamespace(token=7)
        engine._active = {"stream": branch}
        observed: list[tuple[str, object]] = []
        engine._emit = lambda kind, _scope, _generation, **_details: observed.append(
            ("event", kind)
        )
        engine._force_remove_now = lambda value, *, forced: observed.append(
            ("remove", (value.token, forced))
        )

        engine._handle_queue_overrun("stream", 7, 3)

        self.assertEqual(
            observed,
            [("event", "connection"), ("remove", (7, False))],
        )

    def test_srt_sink_error_reports_loss_before_physical_removal(self) -> None:
        sink = object()
        branch = SimpleNamespace(
            token=7,
            generation=3,
            elements={"stream_srt_sink": sink},
        )
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._Gst = SimpleNamespace(
            MessageType=SimpleNamespace(ELEMENT=1, STATE_CHANGED=2, ERROR=3, EOS=4),
        )
        engine._config = config()
        engine._capture_generation = 1
        engine._scope_for_source = lambda _source: ("stream", branch)
        observed: list[tuple[str, object]] = []
        engine._emit = lambda kind, _scope, _generation, **_details: observed.append(
            ("event", kind)
        )
        engine._detach_branch = lambda value, *, drain: observed.append(
            ("remove", (value.token, drain))
        )
        error = SimpleNamespace(domain="srt", code=1)
        message = SimpleNamespace(
            type=engine._Gst.MessageType.ERROR,
            src=sink,
            parse_error=lambda: (error, "connection failed"),
        )

        engine._handle_bus_message(message)

        self.assertEqual(
            observed,
            [("event", "connection"), ("remove", (7, False))],
        )

    def test_srt_sync_error_closes_valve_before_async_detach(self) -> None:
        observed: list[tuple[str, object]] = []

        class Valve:
            def set_property(self, name: str, value: object) -> None:
                observed.append(("valve", (name, value)))

        sink = object()
        branch = SimpleNamespace(
            token=7,
            generation=3,
            elements={
                "stream_srt_sink": sink,
                "stream_transport_valve": Valve(),
            },
        )
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._Gst = SimpleNamespace(
            BusSyncReply=SimpleNamespace(PASS="pass"),
            MessageType=SimpleNamespace(ELEMENT=1, STATE_CHANGED=2, ERROR=3, EOS=4),
        )
        engine._active = {"stream": branch}
        engine._transport_guard_lock = threading.Lock()
        engine._transport_guards = {7: (sink, branch.elements["stream_transport_valve"])}
        engine._config = config()
        engine._capture_generation = 1
        engine._scope_for_source = lambda _source: ("stream", branch)
        engine._emit = lambda kind, _scope, _generation, **_details: observed.append(
            ("event", kind)
        )
        engine._detach_branch = lambda value, *, drain: observed.append(
            ("remove", (value.token, drain))
        )
        error = SimpleNamespace(domain="srt", code=1)
        message = SimpleNamespace(
            type=engine._Gst.MessageType.ERROR,
            src=sink,
            parse_error=lambda: (error, "connection failed"),
        )

        reply = engine._on_bus_sync_message(None, message, None)
        engine._handle_bus_message(message)

        self.assertEqual(reply, "pass")
        self.assertEqual(
            observed,
            [
                ("valve", ("drop", True)),
                ("event", "connection"),
                ("remove", (7, False)),
            ],
        )

    def test_filesink_close_error_cannot_acknowledge_successful_finalization(self) -> None:
        engine = GStreamerEngine(config(), lambda _event: None)
        engine._Gst = SimpleNamespace(
            State=SimpleNamespace(NULL="null"), MessageType=SimpleNamespace(ERROR=3),
            BusSyncReply=SimpleNamespace(PASS="pass"),
        )
        sink = object()
        properties = {"drop": False}
        valve = SimpleNamespace(set_property=lambda name, value: properties.update({name: value}),
                                get_property=lambda name: properties[name])
        engine._transport_guards[7] = (sink, valve)
        emitted = []
        engine._emit = lambda kind, _scope, _generation, **details: emitted.append((kind, details))
        branch = SimpleNamespace(
            kind="recording", token=7, generation=1, forced=False, timeout_source=None,
            eos_probe_id=None, unlinked=True, plan=SimpleNamespace(location="partial.flac"),
            elements={"recording_io_valve": valve},
            bin=SimpleNamespace(
                set_state=lambda _state: engine._on_bus_sync_message(None, SimpleNamespace(type=3, src=sink)),
                get_parent=lambda: None,
            ),
        )
        engine._finalizing[7] = branch
        engine._unlink_branch = lambda _branch: None
        engine._maybe_finish_capture_stop = lambda: None
        engine._finish_remove(7)
        self.assertTrue(properties["drop"])
        self.assertTrue(emitted[-1][1]["forced"])
        self.assertEqual("error", emitted[0][0])
        self.assertNotIn(7, engine._transport_guards)

    def test_recording_guard_contains_only_its_own_sink_error(self) -> None:
        engine = GStreamerEngine(config(), lambda _event: None)
        engine._Gst = SimpleNamespace(MessageType=SimpleNamespace(ERROR=3),
                                      BusSyncReply=SimpleNamespace(PASS="pass"))
        observed = []
        old_sink, new_sink = object(), object()
        for token, sink in ((7, old_sink), (8, new_sink)):
            valve = SimpleNamespace(set_property=lambda name, value, token=token: observed.append((token, name, value)))
            engine._register_transport_guard(token, {"recording_file_sink": sink, "recording_io_valve": valve}, "recording")
        engine._on_bus_sync_message(None, SimpleNamespace(type=3, src=old_sink))
        engine._on_bus_sync_message(None, SimpleNamespace(type=3, src=object()))
        engine._unregister_transport_guard(7)
        engine._on_bus_sync_message(None, SimpleNamespace(type=3, src=old_sink))
        engine._on_bus_sync_message(None, SimpleNamespace(type=3, src=new_sink))
        self.assertEqual([(7, "drop", True), (8, "drop", True)], observed)

    def test_transport_guard_precedes_tee_unblock(self) -> None:
        observed: list[tuple[str, object]] = []

        class Element:
            def __init__(self, name: str) -> None:
                self.name = name

            def connect(self, *_args) -> int:
                return 1

            def get_static_pad(self, name: str):
                return object() if self.name == "stream_queue" and name == "sink" else None

        class Valve(Element):
            def set_property(self, name: str, value: object) -> None:
                observed.append(("valve", (name, value)))

        class Bin:
            def add(self, _element) -> bool:
                return True

            def add_pad(self, _pad) -> bool:
                return True

            def sync_state_with_parent(self) -> bool:
                return True

            def get_parent(self):
                return object()

            def set_state(self, _state) -> None:
                pass

        sink = Element("stream_srt_sink")
        valve = Valve("stream_transport_valve")
        engine = GStreamerEngine.__new__(GStreamerEngine)

        class TeePad:
            def add_probe(self, _probe_type, _callback) -> int:
                return 41

            def link(self, _ghost) -> str:
                return "ok"

            def remove_probe(self, _probe_id: int) -> None:
                observed.append(("active_at_unblock", "stream" in engine._active))
                message = SimpleNamespace(type=3, src=sink)
                observed.append(
                    ("sync_reply", engine._on_bus_sync_message(None, message, None))
                )

        tee_pad = TeePad()
        engine._Gst = SimpleNamespace(
            Bin=SimpleNamespace(new=lambda _name: Bin()),
            GhostPad=SimpleNamespace(new=lambda _name, _pad: object()),
            PadProbeType=SimpleNamespace(BLOCK_DOWNSTREAM="block"),
            PadProbeReturn=SimpleNamespace(OK="probe-ok"),
            PadLinkReturn=SimpleNamespace(OK="ok"),
            MessageType=SimpleNamespace(ERROR=3),
            BusSyncReply=SimpleNamespace(PASS="pass"),
            State=SimpleNamespace(NULL="null"),
        )
        engine._pipeline = SimpleNamespace(
            add=lambda _bin: True,
            remove=lambda _bin: True,
        )
        engine._tee = SimpleNamespace(
            request_pad_simple=lambda _name: tee_pad,
            release_request_pad=lambda _pad: None,
        )
        engine._active = {}
        engine._transport_guard_lock = threading.Lock()
        engine._transport_guards = {}
        engine._require_capture = lambda: None
        engine._next_token = lambda: 7
        engine._link_many = lambda _elements: None
        engine._emit = lambda *_args, **_kwargs: None

        def make(spec, _token):
            value = (
                sink
                if spec.name == "stream_srt_sink"
                else valve
                if spec.name == "stream_transport_valve"
                else Element(spec.name)
            )
            return value

        engine._make = make

        branch = engine._add_branch(stream_branch_plan(config()), generation=3)

        self.assertIs(branch.elements["stream_srt_sink"], sink)
        self.assertEqual(
            observed,
            [
                ("active_at_unblock", False),
                ("valve", ("drop", True)),
                ("sync_reply", "pass"),
            ],
        )
        self.assertEqual(engine._transport_guards[7], (sink, valve))

    def test_finalizing_stream_guard_survives_active_replacement(self) -> None:
        observed: list[str] = []

        class Valve:
            def __init__(self, name: str) -> None:
                self.name = name

            def set_property(self, _name: str, _value: object) -> None:
                observed.append(self.name)

        old_sink, new_sink = object(), object()
        old_valve, new_valve = Valve("old"), Valve("new")
        old = SimpleNamespace(token=7, kind="stream", generation=3)
        new = SimpleNamespace(token=8, kind="stream", generation=3)
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._Gst = SimpleNamespace(
            MessageType=SimpleNamespace(ERROR=3),
            BusSyncReply=SimpleNamespace(PASS="pass"),
        )
        engine._active = {"stream": old}
        engine._finalizing = {}
        engine._transport_guard_lock = threading.Lock()
        engine._transport_guards = {
            7: (old_sink, old_valve),
            8: (new_sink, new_valve),
        }
        engine._cancel_rotation_for_branch = lambda _branch: None
        engine._begin_remove_handle = (
            lambda branch, *, drain: engine._finalizing.update(
                {branch.token: branch}
            )
        )

        engine._begin_remove("stream", drain=False)
        engine._active["stream"] = new
        engine._on_bus_sync_message(
            None, SimpleNamespace(type=3, src=old_sink), None
        )
        engine._on_bus_sync_message(
            None, SimpleNamespace(type=3, src=new_sink), None
        )
        engine._unregister_transport_guard(old.token)
        engine._on_bus_sync_message(
            None, SimpleNamespace(type=3, src=old_sink), None
        )

        self.assertIs(engine._active["stream"], new)
        self.assertIn(7, engine._finalizing)
        self.assertEqual(observed, ["old", "new"])

    def test_bus_sync_handler_is_installed_and_cleared_with_pipeline(self) -> None:
        class Bus:
            def __init__(self) -> None:
                self.sync_handlers: list[object] = []
                self.disconnected: list[int] = []
                self.watch_removed = False

            def set_sync_handler(self, callback, *_unused) -> None:
                self.sync_handlers.append(callback)

            def add_signal_watch(self) -> None:
                pass

            def connect(self, _name, _callback) -> int:
                return 17

            def disconnect(self, handler: int) -> None:
                self.disconnected.append(handler)

            def remove_signal_watch(self) -> None:
                self.watch_removed = True

        bus = Bus()
        states: list[object] = []
        pipeline = SimpleNamespace(
            get_bus=lambda: bus,
            set_state=lambda state: states.append(state),
        )
        engine = GStreamerEngine.__new__(GStreamerEngine)
        engine._Gst = SimpleNamespace(State=SimpleNamespace(NULL="null"))
        engine._pipeline = pipeline
        engine._bus = None
        engine._bus_handler_id = None
        engine._tee = object()
        engine._source = object()
        engine._core_elements = (object(),)
        engine._capture_start_pending_generation = 1
        engine._transport_guard_lock = threading.Lock()
        engine._transport_guards = {1: (object(), object())}

        engine._install_bus_watch()
        installed = bus.sync_handlers[-1]
        engine._force_pipeline_null()

        self.assertEqual(installed.__self__, engine)
        self.assertEqual(installed.__func__, engine._on_bus_sync_message.__func__)
        self.assertIsNone(bus.sync_handlers[-1])
        self.assertEqual(states, ["null"])
        self.assertEqual(bus.disconnected, [17])
        self.assertTrue(bus.watch_removed)
        self.assertEqual(engine._transport_guards, {})
        self.assertIsNone(engine._pipeline)

    def test_forced_old_fragment_reports_rotation_against_replacement(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        old = SimpleNamespace(
            token=11,
            generation=4,
            plan=SimpleNamespace(location="old.flac"),
        )
        new = SimpleNamespace(
            token=12,
            generation=4,
            playing=True,
            plan=SimpleNamespace(location="new.flac"),
        )
        engine._rotations = {11: (old, new)}
        engine._rotation_forced = {11}
        engine._finalizing = {}
        engine._active = {"recording": new}
        emitted: list[tuple[str, dict[str, object]]] = []
        engine._emit = lambda kind, _scope, _generation, **details: emitted.append(
            (kind, details)
        )

        engine._complete_rotation_if_ready(11)

        self.assertEqual(emitted[0][0], "recording-rotated")
        self.assertIs(emitted[0][1]["forced"], True)
        self.assertEqual(emitted[0][1]["new_instance_token"], 12)
        self.assertNotIn(11, engine._rotations)

    def test_old_fragment_error_preserves_rotation_for_replacement_failure(self) -> None:
        engine = GStreamerEngine.__new__(GStreamerEngine)
        old = SimpleNamespace(token=21, kind="recording")
        new = SimpleNamespace(token=22, kind="recording")
        engine._rotations = {21: (old, new)}
        engine._rotation_forced = set()
        engine._finalizing = {21: old}
        engine._active = {"recording": new}
        finished: list[tuple[int, bool]] = []
        engine._finish_remove = lambda token, forced=False: finished.append(
            (token, forced)
        )

        engine._detach_branch(old, drain=False)

        self.assertEqual(finished, [(21, True)])
        self.assertIn(21, engine._rotations)
        self.assertIn(21, engine._rotation_forced)


if __name__ == "__main__":
    unittest.main()
