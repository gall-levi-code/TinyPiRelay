"""Validate stream-spike evidence and fail closed on capability claims."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
DEFAULT_EVIDENCE_PATH = (
    Path(__file__).resolve().parents[2] / "evidence" / "stream_compatibility.json"
)

_EXPECTED_REPRESENTATIONS = {
    "pcm_s16le_48000_stereo_matroska": {
        "name": "pcm_s16le",
        "encoder_element": None,
        "parser_element": None,
        "bitrate_bps": None,
    },
    "flac_48000_stereo_matroska": {
        "name": "flac",
        "encoder_element": "flacenc",
        "parser_element": None,
        "bitrate_bps": None,
    },
    "opus_128k_48000_stereo_matroska": {
        "name": "opus",
        "encoder_element": "opusenc",
        "parser_element": "opusparse",
        "bitrate_bps": 128_000,
    },
}
_RECEIVER_CODEC_ELEMENTS = {
    "pcm_s16le_48000_stereo_matroska": (),
    "flac_48000_stereo_matroska": ("flacparse", "flacdec"),
    "opus_128k_48000_stereo_matroska": ("opusparse", "opusdec"),
}
_AVAILABILITY = {"available", "unavailable", "unknown"}
_APPLICABILITY = {"applicable", "not_applicable", "unknown"}
_RESULT_STATUSES = {"pass", "fail", "pending", "unavailable", "not_applicable"}


class EvidenceError(ValueError):
    """The evidence cannot safely be used to declare a capability."""


def _reject_constant(value: str) -> None:
    raise EvidenceError(f"non-finite JSON number is not allowed: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_evidence(path: str | Path = DEFAULT_EVIDENCE_PATH) -> dict[str, Any]:
    """Load JSON evidence without accepting duplicate keys or non-finite numbers."""

    try:
        text = Path(path).read_text(encoding="utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except EvidenceError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise EvidenceError(f"cannot read evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceError("evidence root must be an object")
    return value


def _object(value: Any, name: str, keys: Iterable[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EvidenceError(f"{name} must be an object")
    expected = set(keys)
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise EvidenceError(f"{name} has missing keys {missing} and extra keys {extra}")
    return value


def _string(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"{name} must be a non-empty string")
    return value


def _argv(value: Any, name: str, executable: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(argument, str) or not argument for argument in value)
        or value[0] != executable
    ):
        raise EvidenceError(f"{name} must be an argument list beginning with {executable!r}")


def _string_list(value: Any, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise EvidenceError(f"{name} must be a non-empty list of unique strings")
    return value


def _result(value: Any, name: str) -> str:
    result = _object(value, name, ("status", "tested_at_utc", "tool_version", "notes"))
    status = result["status"]
    if status not in _RESULT_STATUSES:
        raise EvidenceError(f"{name}.status must be one of {sorted(_RESULT_STATUSES)}")
    tested_at = _string(result["tested_at_utc"], f"{name}.tested_at_utc", nullable=True)
    tool_version = _string(result["tool_version"], f"{name}.tool_version", nullable=True)
    _string(result["notes"], f"{name}.notes")
    if status in {"pass", "fail"} and (tested_at is None or tool_version is None):
        raise EvidenceError(f"{name} {status!r} requires tested_at_utc and tool_version")
    return status


def _attempt(value: Any, name: str, *, required: bool) -> str:
    attempt = _object(
        value,
        name,
        ("required", "availability", "applicability", "uri", "argv", "result"),
    )
    if attempt["required"] is not required:
        raise EvidenceError(f"{name}.required must be {required}")
    if attempt["availability"] not in _AVAILABILITY:
        raise EvidenceError(f"{name}.availability must be one of {sorted(_AVAILABILITY)}")
    if attempt["applicability"] not in _APPLICABILITY:
        raise EvidenceError(f"{name}.applicability must be one of {sorted(_APPLICABILITY)}")
    _string(attempt["uri"], f"{name}.uri")
    executable = {"gstreamer": "gst-launch-1.0", "ffplay": "ffplay", "vlc": "cvlc"}[
        name.rsplit(".", 1)[-1]
    ]
    _argv(attempt["argv"], f"{name}.argv", executable)
    status = _result(attempt["result"], f"{name}.result")
    availability = attempt["availability"]
    applicability = attempt["applicability"]
    if status in {"pass", "fail"} and (
        availability != "available" or applicability != "applicable"
    ):
        raise EvidenceError(f"{name} {status!r} requires available/applicable evidence")
    if status == "unavailable" and availability != "unavailable":
        raise EvidenceError(f"{name} 'unavailable' result requires unavailable tool")
    if status == "not_applicable" and applicability != "not_applicable":
        raise EvidenceError(f"{name} 'not_applicable' result requires matching applicability")
    return status


def _attempt_resolves_gate(attempt: Mapping[str, Any]) -> bool:
    status = attempt["result"]["status"]
    if attempt["required"]:
        return (
            attempt["availability"] == "available"
            and attempt["applicability"] == "applicable"
            and status == "pass"
        )
    if attempt["applicability"] == "not_applicable":
        return status == "not_applicable"
    if attempt["availability"] == "unavailable":
        return status == "unavailable"
    return (
        attempt["availability"] == "available"
        and attempt["applicability"] == "applicable"
        and status in {"pass", "fail"}
    )


def _validate_recipes(
    representation: Mapping[str, Any],
    identifier: str,
    name: str,
) -> None:
    """Bind every recorded command and element group to its representation."""

    source = representation["source_audio"]
    codec = representation["codec"]
    framing = representation["framing"]
    transport = representation["transport"]
    caps = (
        f'{source["media_type"]},format={source["format"]},layout={source["layout"]},'
        f'rate={source["rate_hz"]},channels={source["channels"]}'
    )

    codec_elements = [
        element
        for element in (codec["encoder_element"], codec["parser_element"])
        if element is not None
    ]
    codec_argv: list[str] = []
    if codec["encoder_element"] is not None:
        codec_argv.extend(("!", codec["encoder_element"]))
        if identifier == "flac_48000_stereo_matroska":
            codec_argv.append("quality=5")
        elif codec["bitrate_bps"] is not None:
            codec_argv.append(f'bitrate={codec["bitrate_bps"]}')
    if codec["parser_element"] is not None:
        codec_argv.extend(("!", codec["parser_element"]))

    sender_uri = (
        f'srt://{transport["host"]}:{transport["port"]}'
        f'?mode={transport["sender_mode"]}&latency={transport["gstreamer_latency_ms"]}'
    )
    sender_tail = [
        "!",
        "audioconvert",
        "!",
        "audioresample",
        "!",
        caps,
        *codec_argv,
        "!",
        framing["muxer_element"],
        "streamable=true",
        "!",
        "srtsink",
        f"uri={sender_uri}",
        "wait-for-connection=true",
    ]
    expected_synthetic = [
        "gst-launch-1.0",
        "-e",
        "-v",
        "audiotestsrc",
        "is-live=true",
        "wave=sine",
        *sender_tail,
    ]
    expected_alsa = [
        "gst-launch-1.0",
        "-e",
        "-v",
        "alsasrc",
        "device=<ALSA_DEVICE>",
        *sender_tail,
    ]

    receiver_codec_elements = list(_RECEIVER_CODEC_ELEMENTS[identifier])
    receiver_codec_argv = [
        token
        for element in receiver_codec_elements
        for token in ("!", element)
    ]
    gst_receiver_uri = (
        f'srt://:{transport["port"]}'
        f'?mode={transport["receiver_mode"]}&latency={transport["gstreamer_latency_ms"]}'
    )
    expected_gstreamer = [
        "gst-launch-1.0",
        "-v",
        "srtsrc",
        f"uri={gst_receiver_uri}",
        "!",
        "matroskademux",
        "!",
        "queue",
        *receiver_codec_argv,
        "!",
        caps,
        "!",
        "fakesink",
        "sync=false",
    ]
    ffplay_uri = (
        f'srt://0.0.0.0:{transport["port"]}'
        f'?mode={transport["receiver_mode"]}&latency={transport["ffmpeg_latency_us"]}'
    )
    vlc_uri = f'srt://@:{transport["port"]}?mode={transport["receiver_mode"]}'

    expected_elements = {
        "sender_synthetic": [
            "audiotestsrc",
            "audioconvert",
            "audioresample",
            "capsfilter",
            *codec_elements,
            framing["muxer_element"],
            "srtsink",
        ],
        "sender_alsa": [
            "alsasrc",
            "audioconvert",
            "audioresample",
            "capsfilter",
            *codec_elements,
            framing["muxer_element"],
            "srtsink",
        ],
        "gstreamer_receiver": [
            "srtsrc",
            "matroskademux",
            "queue",
            *receiver_codec_elements,
            "capsfilter",
            "fakesink",
        ],
    }
    receivers = representation["receivers"]
    expected_values = (
        (
            representation["required_elements"],
            expected_elements,
            f"{name}.required_elements",
        ),
        (
            representation["sender"]["synthetic_argv"],
            expected_synthetic,
            f"{name}.sender.synthetic_argv",
        ),
        (
            representation["sender"]["alsa_argv"],
            expected_alsa,
            f"{name}.sender.alsa_argv",
        ),
        (
            receivers["gstreamer"]["uri"],
            gst_receiver_uri,
            f"{name}.receivers.gstreamer.uri",
        ),
        (
            receivers["gstreamer"]["argv"],
            expected_gstreamer,
            f"{name}.receivers.gstreamer.argv",
        ),
        (receivers["ffplay"]["uri"], ffplay_uri, f"{name}.receivers.ffplay.uri"),
        (
            receivers["ffplay"]["argv"],
            [
                "ffplay",
                "-hide_banner",
                "-loglevel",
                "info",
                "-nodisp",
                "-autoexit",
                ffplay_uri,
            ],
            f"{name}.receivers.ffplay.argv",
        ),
        (receivers["vlc"]["uri"], vlc_uri, f"{name}.receivers.vlc.uri"),
        (
            receivers["vlc"]["argv"],
            ["cvlc", "--play-and-exit", "--no-video", vlc_uri],
            f"{name}.receivers.vlc.argv",
        ),
    )
    for actual, expected, field_name in expected_values:
        if actual != expected:
            raise EvidenceError(
                f"{field_name} does not match the schema v1 representation recipe"
            )


def _validate_representation(value: Any, index: int) -> tuple[str, bool]:
    name = f"representations[{index}]"
    representation = _object(
        value,
        name,
        (
            "id",
            "description",
            "source_audio",
            "codec",
            "framing",
            "transport",
            "required_elements",
            "sender",
            "receivers",
        ),
    )
    identifier = _string(representation["id"], f"{name}.id")
    if identifier not in _EXPECTED_REPRESENTATIONS:
        raise EvidenceError(f"{name}.id is not a schema v1 representation: {identifier}")
    _string(representation["description"], f"{name}.description")

    source = _object(
        representation["source_audio"],
        f"{name}.source_audio",
        ("media_type", "format", "layout", "rate_hz", "channels"),
    )
    if source != {
        "media_type": "audio/x-raw",
        "format": "S16LE",
        "layout": "interleaved",
        "rate_hz": 48_000,
        "channels": 2,
    }:
        raise EvidenceError(f"{name}.source_audio must be 48 kHz stereo PCM S16LE")

    codec = _object(
        representation["codec"],
        f"{name}.codec",
        ("name", "encoder_element", "parser_element", "bitrate_bps"),
    )
    if codec != _EXPECTED_REPRESENTATIONS[identifier]:
        raise EvidenceError(f"{name}.codec does not match {identifier}")

    framing = _object(
        representation["framing"],
        f"{name}.framing",
        ("container", "muxer_element", "streamable"),
    )
    if framing != {
        "container": "matroska",
        "muxer_element": "matroskamux",
        "streamable": True,
    }:
        raise EvidenceError(f"{name}.framing must be streamable Matroska")

    transport = _object(
        representation["transport"],
        f"{name}.transport",
        (
            "protocol",
            "sender_mode",
            "receiver_mode",
            "host",
            "port",
            "gstreamer_latency_ms",
            "ffmpeg_latency_us",
        ),
    )
    if (
        transport["protocol"] != "srt"
        or transport["sender_mode"] != "caller"
        or transport["receiver_mode"] != "listener"
        or not isinstance(transport["host"], str)
        or not transport["host"]
        or not isinstance(transport["port"], int)
        or isinstance(transport["port"], bool)
        or not 1 <= transport["port"] <= 65_535
        or not isinstance(transport["gstreamer_latency_ms"], int)
        or isinstance(transport["gstreamer_latency_ms"], bool)
        or transport["gstreamer_latency_ms"] < 0
        or not isinstance(transport["ffmpeg_latency_us"], int)
        or isinstance(transport["ffmpeg_latency_us"], bool)
        or transport["ffmpeg_latency_us"] < 0
    ):
        raise EvidenceError(f"{name}.transport must describe an SRT caller/listener pair")

    elements = _object(
        representation["required_elements"],
        f"{name}.required_elements",
        ("sender_synthetic", "sender_alsa", "gstreamer_receiver"),
    )
    for group, names in elements.items():
        _string_list(names, f"{name}.required_elements.{group}")

    sender = _object(
        representation["sender"],
        f"{name}.sender",
        ("required", "availability", "applicability", "synthetic_argv", "alsa_argv", "result"),
    )
    if sender["required"] is not True:
        raise EvidenceError(f"{name}.sender.required must be true")
    if sender["availability"] not in _AVAILABILITY:
        raise EvidenceError(f"{name}.sender.availability must be one of {sorted(_AVAILABILITY)}")
    if sender["applicability"] not in _APPLICABILITY:
        raise EvidenceError(f"{name}.sender.applicability must be one of {sorted(_APPLICABILITY)}")
    _argv(sender["synthetic_argv"], f"{name}.sender.synthetic_argv", "gst-launch-1.0")
    _argv(sender["alsa_argv"], f"{name}.sender.alsa_argv", "gst-launch-1.0")
    sender_status = _result(sender["result"], f"{name}.sender.result")
    if sender_status in {"pass", "fail"} and (
        sender["availability"] != "available" or sender["applicability"] != "applicable"
    ):
        raise EvidenceError(
            f"{name}.sender {sender_status!r} requires available/applicable evidence"
        )
    if sender_status == "unavailable" and sender["availability"] != "unavailable":
        raise EvidenceError(f"{name}.sender 'unavailable' result requires unavailable tool")
    if sender_status == "not_applicable" and sender["applicability"] != "not_applicable":
        raise EvidenceError(
            f"{name}.sender 'not_applicable' result requires matching applicability"
        )

    receivers = _object(
        representation["receivers"],
        f"{name}.receivers",
        ("gstreamer", "ffplay", "vlc"),
    )
    _attempt(receivers["gstreamer"], f"{name}.receivers.gstreamer", required=True)
    _attempt(receivers["ffplay"], f"{name}.receivers.ffplay", required=False)
    _attempt(receivers["vlc"], f"{name}.receivers.vlc", required=False)
    _validate_recipes(representation, identifier, name)

    sender_passes = (
        sender["availability"] == "available"
        and sender["applicability"] == "applicable"
        and sender_status == "pass"
    )
    receivers_resolved = all(_attempt_resolves_gate(attempt) for attempt in receivers.values())
    return identifier, sender_passes and receivers_resolved


def computed_capabilities(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    """Return eligible capability IDs after strict schema and result validation."""

    root = _object(
        evidence,
        "evidence",
        (
            "schema_version",
            "evidence_context",
            "tool_versions",
            "element_versions",
            "declared_capabilities",
            "representations",
        ),
    )
    if root["schema_version"] != SCHEMA_VERSION or isinstance(root["schema_version"], bool):
        raise EvidenceError(f"schema_version must be integer {SCHEMA_VERSION}")

    context = _object(
        root["evidence_context"],
        "evidence_context",
        (
            "recorded_at_utc",
            "host",
            "os_release",
            "kernel",
            "architecture",
            "userland_bits",
            "dpkg_architecture",
            "board_model",
        ),
    )
    for key, value in context.items():
        if key == "userland_bits":
            if value not in (None, 32, 64):
                raise EvidenceError("evidence_context.userland_bits must be 32, 64, or null")
        else:
            _string(value, f"evidence_context.{key}", nullable=True)

    versions = _object(
        root["tool_versions"],
        "tool_versions",
        ("gst-launch-1.0", "ffplay", "cvlc"),
    )
    for key, value in versions.items():
        _string(value, f"tool_versions.{key}", nullable=True)

    if not isinstance(root["element_versions"], dict) or not root["element_versions"]:
        raise EvidenceError("element_versions must be a non-empty object")
    for element, version in root["element_versions"].items():
        _string(element, "element_versions key")
        _string(version, f"element_versions.{element}", nullable=True)

    declared = root["declared_capabilities"]
    if (
        not isinstance(declared, list)
        or any(not isinstance(item, str) or not item for item in declared)
        or len(set(declared)) != len(declared)
    ):
        raise EvidenceError("declared_capabilities must be a list of unique strings")

    representations = root["representations"]
    if not isinstance(representations, list) or len(representations) != len(
        _EXPECTED_REPRESENTATIONS
    ):
        raise EvidenceError("schema v1 requires exactly three representations")
    seen: set[str] = set()
    eligible: list[str] = []
    referenced_elements: set[str] = set()
    context_complete = all(value is not None for value in context.values())
    gst_version_recorded = versions["gst-launch-1.0"] is not None
    for index, representation in enumerate(representations):
        identifier, enabled = _validate_representation(representation, index)
        if identifier in seen:
            raise EvidenceError(f"duplicate representation id: {identifier}")
        seen.add(identifier)
        representation_elements = {
            element
            for names in representation["required_elements"].values()
            for element in names
        }
        referenced_elements.update(representation_elements)
        element_versions_recorded = all(
            root["element_versions"].get(element) is not None
            for element in representation_elements
        )
        if enabled and context_complete and gst_version_recorded and element_versions_recorded:
            eligible.append(identifier)
    if seen != set(_EXPECTED_REPRESENTATIONS):
        raise EvidenceError("schema v1 representations are incomplete")
    missing_versions = referenced_elements - set(root["element_versions"])
    if missing_versions:
        raise EvidenceError(f"missing element version slots: {sorted(missing_versions)}")
    return tuple(eligible)


def validate_evidence(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    """Validate the document and ensure its declarations equal the fail-closed gate."""

    eligible = computed_capabilities(evidence)
    declared = tuple(evidence["declared_capabilities"])
    if declared != eligible:
        raise EvidenceError(
            "declared_capabilities does not match passing evidence: "
            f"declared={list(declared)!r}, eligible={list(eligible)!r}"
        )
    return eligible


def receiver_results(evidence: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Expose receiver outcomes without turning an interop failure into support."""

    computed_capabilities(evidence)
    return {
        representation["id"]: {
            name: attempt["result"]["status"]
            for name, attempt in representation["receivers"].items()
        }
        for representation in evidence["representations"]
    }


def _list_rows(evidence: Mapping[str, Any]) -> Iterable[str]:
    eligible = set(validate_evidence(evidence))
    yield "representation\tsender\tgstreamer\tffplay\tvlc\tdeclared"
    for representation in evidence["representations"]:
        statuses = [representation["sender"]["result"]["status"]]
        statuses.extend(
            representation["receivers"][name]["result"]["status"]
            for name in ("gstreamer", "ffplay", "vlc")
        )
        yield "\t".join(
            [representation["id"], *statuses, str(representation["id"] in eligible).lower()]
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "list", "capabilities"))
    parser.add_argument("evidence", nargs="?", type=Path, default=DEFAULT_EVIDENCE_PATH)
    args = parser.parse_args(argv)
    try:
        evidence = load_evidence(args.evidence)
        capabilities = validate_evidence(evidence)
    except EvidenceError as exc:
        print(f"invalid compatibility evidence: {exc}", file=sys.stderr)
        return 2
    if args.action == "validate":
        print(f"valid schema v{SCHEMA_VERSION}; {len(capabilities)} declared capabilities")
    elif args.action == "capabilities":
        print(json.dumps(list(capabilities), separators=(",", ":")))
    else:
        print("\n".join(_list_rows(evidence)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
