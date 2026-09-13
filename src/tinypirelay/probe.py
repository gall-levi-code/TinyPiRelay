"""Read-only host and media dependency probe for the Step 1 spike."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
PRESENT = "present"
MISSING = "missing"
UNVERIFIED = "unverified"

APT_PACKAGES = (
    "python3",
    "python3-gi",
    "gir1.2-gstreamer-1.0",
    "gir1.2-gst-plugins-base-1.0",
    "alsa-utils",
    "ffmpeg",
    "avahi-daemon",
    "libgstreamer1.0-0",
    "gstreamer1.0-tools",
    "gstreamer1.0-alsa",
    "gstreamer1.0-plugins-base",
    "gstreamer1.0-plugins-good",
    "gstreamer1.0-plugins-bad",
)

REQUIRED_GSTREAMER_ELEMENTS = (
    "alsasrc",
    "audioconvert",
    "audioresample",
    "capsfilter",
    "tee",
    "queue",
    "valve",
    "level",
    "spectrum",
    "opusenc",
    "flacenc",
    "srtsink",
    # Production branch endpoints/framing used by the Step 2 engine.
    "matroskamux",
    "opusparse",
    "filesink",
    "fakesink",
)

# Sender and receiver helpers used by the retained Matroska PCM/FLAC/Opus spike.
SPIKE_GSTREAMER_ELEMENTS = (
    "audiotestsrc",
    "matroskamux",
    "matroskademux",
    "srtsrc",
    "flacparse",
    "opusparse",
    "flacdec",
    "opusdec",
    "fakesink",
    "identity",
)

ELEMENT_PACKAGES = {
    "alsasrc": "gstreamer1.0-alsa",
    "audioconvert": "gstreamer1.0-plugins-base",
    "audioresample": "gstreamer1.0-plugins-base",
    "capsfilter": "libgstreamer1.0-0",
    "tee": "libgstreamer1.0-0",
    "queue": "libgstreamer1.0-0",
    "valve": "libgstreamer1.0-0",
    "level": "gstreamer1.0-plugins-good",
    "spectrum": "gstreamer1.0-plugins-good",
    "opusenc": "gstreamer1.0-plugins-base",
    "flacenc": "gstreamer1.0-plugins-good",
    "srtsink": "gstreamer1.0-plugins-bad",
    "filesink": "libgstreamer1.0-0",
    "audiotestsrc": "gstreamer1.0-plugins-base",
    "matroskamux": "gstreamer1.0-plugins-good",
    "matroskademux": "gstreamer1.0-plugins-good",
    "srtsrc": "gstreamer1.0-plugins-bad",
    "flacparse": "gstreamer1.0-plugins-good",
    "opusparse": "gstreamer1.0-plugins-bad",
    "flacdec": "gstreamer1.0-plugins-good",
    "opusdec": "gstreamer1.0-plugins-base",
    "fakesink": "libgstreamer1.0-0",
    "identity": "libgstreamer1.0-0",
}


@dataclass(frozen=True)
class CommandResult:
    """Normalized result for an argv-only command invocation."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None


Runner = Callable[[Sequence[str]], CommandResult]
Reader = Callable[[str], bytes | None]


def run_command(
    argv: Sequence[str], *, gst_registry: str | None = None
) -> CommandResult:
    """Run a read-only command without a shell or persistent registry refresh."""

    env = os.environ.copy()
    env.update({"LANG": "C", "LC_ALL": "C"})
    if gst_registry is None:
        # An explicitly supplied registry may be read, but the probe must not change it.
        env["GST_REGISTRY_UPDATE"] = "no"
    else:
        # Let GStreamer populate only the disposable registry selected by collect_probe.
        env["GST_REGISTRY"] = gst_registry
        env["GST_REGISTRY_1_0"] = gst_registry
        env.pop("GST_REGISTRY_DISABLE", None)
        env.pop("GST_REGISTRY_UPDATE", None)
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=15,
        )
    except FileNotFoundError:
        return CommandResult(None, error="command not found")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            None,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            error="command timed out after 15 seconds",
        )
    except OSError as exc:
        return CommandResult(None, error=str(exc))
    return CommandResult(result.returncode, result.stdout, result.stderr)


def read_host_file(path: str) -> bytes | None:
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def parse_os_release(text: str) -> dict[str, str]:
    """Parse os-release assignments without evaluating shell syntax."""

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            continue
        try:
            parts = shlex.split(raw_value, comments=False, posix=True)
        except ValueError:
            continue
        values[key] = " ".join(parts)
    return values


def parse_cpuinfo(text: str) -> dict[str, object]:
    records: list[dict[str, str]] = []
    record: dict[str, str] = {}
    for raw_line in text.splitlines() + [""]:
        if not raw_line.strip():
            if record:
                records.append(record)
                record = {}
            continue
        if ":" in raw_line:
            key, value = raw_line.split(":", 1)
            record[key.strip()] = value.strip()

    def first(*keys: str) -> str | None:
        return next(
            (entry[key] for entry in records for key in keys if entry.get(key)),
            None,
        )

    processors = {entry["processor"] for entry in records if "processor" in entry}
    revision = first("Revision")
    return {
        "model": first("model name", "Processor", "cpu model", "Model"),
        "hardware": first("Hardware"),
        "revision": revision.removeprefix("0x").lower() if revision else None,
        "logical_cpus": len(processors) or None,
    }


def parse_meminfo(text: str) -> dict[str, int | None]:
    match = re.search(r"^MemTotal:\s*(\d+)\s*kB\s*$", text, re.MULTILINE | re.IGNORECASE)
    total_kib = int(match.group(1)) if match else None
    return {"total_bytes": total_kib * 1024 if total_kib is not None else None}


def parse_arecord_devices(text: str) -> list[dict[str, object]]:
    pattern = re.compile(
        r"^card\s+(?P<card>\d+):\s*(?P<card_id>[^\[]+?)\s*"
        r"\[(?P<card_name>[^\]]+)\],\s*device\s+(?P<device>\d+):\s*"
        r"(?P<device_id>[^\[]+?)\s*\[(?P<device_name>[^\]]+)\]\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    devices = []
    for match in pattern.finditer(text):
        card = int(match.group("card"))
        device = int(match.group("device"))
        devices.append(
            {
                "alsa_id": f"hw:{card},{device}",
                "card": card,
                "card_id": match.group("card_id").strip(),
                "card_name": match.group("card_name").strip(),
                "device": device,
                "device_id": match.group("device_id").strip(),
                "device_name": match.group("device_name").strip(),
            }
        )
    return devices


def parse_apt_policy(
    text: str, packages: Iterable[str] = APT_PACKAGES
) -> dict[str, dict[str, str | None]]:
    package_names = tuple(packages)
    parsed = {name: {"installed_version": None, "candidate_version": None} for name in package_names}
    current: str | None = None
    for raw_line in text.splitlines():
        if raw_line and not raw_line[0].isspace() and raw_line.endswith(":"):
            heading = raw_line[:-1]
            current = next(
                (name for name in package_names if heading == name or heading.startswith(name + ":")),
                None,
            )
            continue
        if current is None:
            continue
        match = re.match(r"\s*(Installed|Candidate):\s*(\S+)", raw_line)
        if match:
            key = "installed_version" if match.group(1) == "Installed" else "candidate_version"
            value = match.group(2)
            parsed[current][key] = None if value == "(none)" else value
    return parsed


def parse_gst_element_list(text: str) -> dict[str, str]:
    """Return element factory -> plugin from gst-inspect's compact listing."""

    elements: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*([^\s:]+):\s+([^\s:]+):", line)
        if match:
            elements[match.group(2)] = match.group(1)
    return elements


def parse_gst_plugin_details(text: str) -> dict[str, str | None]:
    """Parse the plugin name/version block emitted by gst-inspect."""

    match = re.search(
        r"^Plugin Details:\s*$\n(?P<body>(?:^[ \t]+.*(?:\n|$))+)",
        text,
        re.MULTILINE,
    )
    body = match.group("body") if match else ""

    def field(name: str) -> str | None:
        value = re.search(rf"^\s+{name}\s{{2,}}(.+?)\s*$", body, re.MULTILINE)
        return value.group(1) if value else None

    return {"name": field("Name"), "version": field("Version")}


def collect_probe(
    *,
    runner: Runner = run_command,
    reader: Reader = read_host_file,
    disk_usage_fn: Callable[[str], object] = shutil.disk_usage,
    uname_fn: Callable[[], object] = platform.uname,
    cpu_count_fn: Callable[[], int | None] = os.cpu_count,
    pointer_bits: int | None = None,
) -> dict[str, object]:
    """Collect a schema-v1 probe result without changing host state."""

    os_release = _probe_os_release(reader)
    kernel, uname_value = _probe_kernel(uname_fn)
    userland = _probe_userland_bitness(runner, pointer_bits)
    dpkg = _probe_dpkg_architecture(runner)
    cpuinfo_raw = _safe_read(reader, "/proc/cpuinfo")
    cpuinfo = parse_cpuinfo(_decode(cpuinfo_raw)) if cpuinfo_raw is not None else {}
    board = _probe_board(reader, cpuinfo)
    baseline = _probe_os_architecture_support(os_release, kernel, userland, dpkg)
    platform_result = {
        "status": _combine_status(os_release, kernel, userland, dpkg, baseline),
        "os_architecture_baseline": baseline,
        "os_release": os_release,
        "kernel": kernel,
        "userland_bitness": userland,
        "dpkg_architecture": dpkg,
    }

    cpu = _probe_cpu(cpuinfo, uname_value, cpu_count_fn)
    memory = _probe_memory(reader)
    hardware = {
        "status": _combine_status(board, cpu, memory),
        "board": board,
        "cpu": cpu,
        "memory": memory,
    }

    storage = _probe_storage(disk_usage_fn)
    audio = _probe_audio(runner)
    packages = _probe_packages(runner)
    if runner is run_command and not any(
        name in os.environ for name in ("GST_REGISTRY", "GST_REGISTRY_1_0")
    ):
        try:
            with tempfile.TemporaryDirectory(prefix="tinypirelay-gst-") as directory:
                registry = str(Path(directory) / "registry.bin")
                gstreamer = _probe_gstreamer(
                    lambda argv: run_command(argv, gst_registry=registry),
                    registry_update_disabled=False,
                )
        except OSError as exc:
            reason = f"temporary GStreamer registry unavailable: {exc}"
            gstreamer = _probe_gstreamer(
                lambda _argv: CommandResult(None, error=reason),
                registry_update_disabled=False,
            )
    else:
        gstreamer = _probe_gstreamer(runner)

    result: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "status": _combine_status(
            platform_result, hardware, storage, audio, packages, gstreamer
        ),
        "platform": platform_result,
        "hardware": hardware,
        "storage": storage,
        "audio": audio,
        "packages": packages,
        "gstreamer": gstreamer,
    }
    result["actions"] = _actions(result)
    return result


def render_json(result: Mapping[str, object]) -> str:
    return json.dumps(result, indent=2, sort_keys=True, allow_nan=False)


def render_human(result: Mapping[str, object]) -> str:
    platform_result = result["platform"]
    hardware = result["hardware"]
    storage = result["storage"]
    audio = result["audio"]
    packages = result["packages"]
    gstreamer = result["gstreamer"]
    assert isinstance(platform_result, Mapping)
    assert isinstance(hardware, Mapping)
    assert isinstance(storage, Mapping)
    assert isinstance(audio, Mapping)
    assert isinstance(packages, Mapping)
    assert isinstance(gstreamer, Mapping)

    os_release = platform_result["os_release"]
    kernel = platform_result["kernel"]
    userland = platform_result["userland_bitness"]
    dpkg = platform_result["dpkg_architecture"]
    board = hardware["board"]
    cpu = hardware["cpu"]
    memory = hardware["memory"]
    assert all(
        isinstance(item, Mapping)
        for item in (os_release, kernel, userland, dpkg, board, cpu, memory)
    )
    baseline = platform_result.get("os_architecture_baseline")
    if not isinstance(baseline, Mapping):
        baseline = _probe_os_architecture_support(os_release, kernel, userland, dpkg)

    package_items = packages["items"]
    tools = gstreamer["tools"]
    required = gstreamer["required_elements"]
    spike = gstreamer["spike_elements"]
    assert isinstance(package_items, Mapping)
    assert isinstance(tools, Mapping)
    assert isinstance(required, Mapping)
    assert isinstance(spike, Mapping)

    lines = [
        f"TinyPiRelay read-only platform probe (schema {result['schema_version']})",
        f"Overall: {str(result['status']).upper()}",
        "",
        _human_line(
            "OS/release",
            os_release,
            str(os_release.get("pretty_name") or os_release.get("name") or "unknown"),
        ),
        _human_line(
            "Kernel",
            kernel,
            " ".join(
                str(kernel.get(key) or "unknown") for key in ("system", "release", "machine")
            ),
        ),
        _human_line("Userland", userland, f"{userland.get('bits') or 'unknown'}-bit"),
        _human_line("dpkg architecture", dpkg, str(dpkg.get("value") or "unknown")),
        _human_line("Supported OS/architecture baseline", baseline, str(baseline["target"])),
        _human_line(
            "Board",
            board,
            f"{board.get('model') or 'unknown'}; revision {board.get('revision') or 'unknown'}",
        ),
        _human_line(
            "CPU",
            cpu,
            f"{cpu.get('model') or 'unknown'}; {cpu.get('logical_cpus') or 'unknown'} logical CPU(s)",
        ),
        _human_line("RAM", memory, _format_bytes(memory.get("total_bytes"))),
        _human_line(
            "Storage /",
            storage,
            f"{storage.get('used_percent', 'unknown')}% used; {_format_bytes(storage.get('free_bytes'))} free",
        ),
        _human_line(
            "ALSA capture",
            audio,
            f"{len(audio.get('capture_devices', []))} hardware device(s)",
        ),
        _human_count_line("APT packages", packages, package_items),
        _human_count_line("GStreamer tools", gstreamer["tools_status"], tools),
        _human_count_line("Required GStreamer elements", gstreamer["required_status"], required),
        _human_count_line("Spike GStreamer elements", gstreamer["spike_status"], spike),
        "",
        "Actions:",
    ]
    actions = result.get("actions", [])
    if actions:
        lines.extend(f"- {action}" for action in actions)
    else:
        lines.append("- None. All probed requirements are present.")
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None,
    *,
    collect: Callable[[], dict[str, object]] = collect_probe,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("human", "json"), default="human")
    args = parser.parse_args(argv)
    result = collect()
    print(render_json(result) if args.format == "json" else render_human(result))
    return 0 if result.get("status") == PRESENT else 1


def _probe_os_release(reader: Reader) -> dict[str, object]:
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        raw = _safe_read(reader, path)
        if raw is None:
            continue
        values = parse_os_release(_decode(raw))
        if values:
            rpi_issue = _decode(_safe_read(reader, "/etc/rpi-issue"))
            os_id = values.get("ID", "").casefold()
            return {
                "status": PRESENT,
                "id": values.get("ID"),
                "name": values.get("NAME"),
                "pretty_name": values.get("PRETTY_NAME"),
                "version_id": values.get("VERSION_ID"),
                "version_codename": values.get("VERSION_CODENAME"),
                "raspberry_pi_os": os_id == "raspbian"
                or "raspberry pi" in rpi_issue.casefold(),
                "source": path,
                "reason": None,
            }
    return {
        "status": UNVERIFIED,
        "id": None,
        "name": None,
        "pretty_name": None,
        "version_id": None,
        "version_codename": None,
        "raspberry_pi_os": None,
        "source": None,
        "reason": "os-release was not readable",
    }


def _probe_kernel(uname_fn: Callable[[], object]) -> tuple[dict[str, object], object | None]:
    try:
        value = uname_fn()
    except OSError as exc:
        return {
            "status": UNVERIFIED,
            "system": None,
            "release": None,
            "machine": None,
            "reason": str(exc),
        }, None
    system = getattr(value, "system", None)
    release = getattr(value, "release", None)
    machine = getattr(value, "machine", None)
    complete = bool(system and release and machine)
    return {
        "status": PRESENT if complete else UNVERIFIED,
        "system": system or None,
        "release": release or None,
        "machine": machine or None,
        "reason": None if complete else "uname returned incomplete data",
    }, value


def _probe_dpkg_architecture(runner: Runner) -> dict[str, object]:
    result = runner(("dpkg", "--print-architecture"))
    value = result.stdout.strip().splitlines()[0] if result.stdout.strip() else None
    if result.returncode == 0 and value:
        return {"status": PRESENT, "value": value, "reason": None}
    return {
        "status": UNVERIFIED,
        "value": None,
        "reason": result.error or _command_error(result) or "dpkg returned no architecture",
    }


def _probe_userland_bitness(runner: Runner, pointer_bits: int | None) -> dict[str, object]:
    result = runner(("getconf", "LONG_BIT"))
    value = result.stdout.strip()
    if result.returncode == 0 and value in {"32", "64"}:
        return {
            "status": PRESENT,
            "bits": int(value),
            "method": "getconf_LONG_BIT",
            "reason": None,
        }
    fallback = pointer_bits if pointer_bits is not None else struct.calcsize("P") * 8
    return {
        "status": UNVERIFIED,
        "bits": fallback,
        "method": "python_pointer_size_fallback",
        "reason": (
            "getconf LONG_BIT was unavailable; this is interpreter bitness, not verified "
            "userland bitness"
        ),
    }


def _probe_os_architecture_support(
    os_release: Mapping[str, object],
    kernel: Mapping[str, object],
    userland: Mapping[str, object],
    dpkg: Mapping[str, object],
) -> dict[str, object]:
    """Fail closed unless the observed OS and architecture match the v1 baseline."""

    target = (
        "Raspberry Pi OS Bookworm/Trixie on Linux "
        "(32-bit armhf or 64-bit arm64)"
    )
    os_id = str(os_release.get("id") or "").casefold()
    raspberry_pi_os = os_release.get("raspberry_pi_os")
    if raspberry_pi_os is None and os_id == "raspbian":
        raspberry_pi_os = True
    codename = str(os_release.get("version_codename") or "").casefold()
    system = str(kernel.get("system") or "").casefold()
    machine = str(kernel.get("machine") or "").casefold()
    bits = userland.get("bits")
    architecture = str(dpkg.get("value") or "").casefold()

    unsupported: list[str] = []
    if os_id and os_id not in {"raspbian", "debian"}:
        unsupported.append(f"OS ID {os_id!r} is not Raspberry Pi OS")
    if os_id and raspberry_pi_os is False:
        unsupported.append("the OS has no Raspberry Pi OS identity marker")
    if codename and codename not in {"bookworm", "trixie"}:
        unsupported.append(f"release {codename!r} is outside the v1 support window")
    if system and system != "linux":
        unsupported.append(f"kernel {system!r} is not Linux")
    architecture_tuple = (architecture, bits)
    if architecture and bits is not None and architecture_tuple not in {
        ("armhf", 32),
        ("arm64", 64),
    }:
        unsupported.append(
            f"dpkg architecture/userland {architecture or 'unknown'}/{bits}-bit is unsupported"
        )
    supported_machines = {"armv6l", "armv7l", "armv8l", "aarch64", "arm64"}
    if machine and machine not in supported_machines:
        unsupported.append(f"kernel machine {machine!r} is not a supported ARM architecture")
    elif machine:
        valid_machines = (
            {"armv6l", "armv7l", "armv8l", "aarch64"}
            if architecture_tuple == ("armhf", 32)
            else {"aarch64", "arm64"}
            if architecture_tuple == ("arm64", 64)
            else set()
        )
        if architecture and bits is not None and machine not in valid_machines:
            unsupported.append(
                f"kernel machine {machine!r} does not match {architecture}/{bits}-bit"
            )
    if os_id and architecture_tuple in {("armhf", 32), ("arm64", 64)}:
        expected_id = "raspbian" if architecture == "armhf" else "debian"
        if os_id != expected_id:
            unsupported.append(
                f"OS ID {os_id!r} does not match Raspberry Pi OS {architecture}"
            )

    if unsupported:
        return {
            "status": MISSING,
            "target": target,
            "reason": "; ".join(unsupported),
        }
    if not all(
        (
            os_id,
            raspberry_pi_os,
            codename,
            system,
            machine,
            bits,
            architecture,
        )
    ):
        return {
            "status": UNVERIFIED,
            "target": target,
            "reason": "OS identity or architecture was not fully verified",
        }
    return {"status": PRESENT, "target": target, "reason": None}


def _probe_board(reader: Reader, cpuinfo: Mapping[str, object]) -> dict[str, object]:
    model = None
    model_source = None
    for path in (
        "/proc/device-tree/model",
        "/sys/firmware/devicetree/base/model",
        "/sys/devices/virtual/dmi/id/product_name",
    ):
        raw = _safe_read(reader, path)
        if raw:
            model = _decode(raw).strip("\x00\r\n ") or None
            if model:
                model_source = path
                break

    revision = cpuinfo.get("revision")
    revision_source = "/proc/cpuinfo" if revision else None
    if not revision:
        raw_revision = _safe_read(
            reader, "/sys/firmware/devicetree/base/system/linux,revision"
        )
        if raw_revision and len(raw_revision) >= 4:
            number = int.from_bytes(raw_revision[-4:], "big")
            if number:
                revision = format(number, "x")
                revision_source = "/sys/firmware/devicetree/base/system/linux,revision"

    status = PRESENT if model and revision else UNVERIFIED
    return {
        "status": status,
        "model": model,
        "model_source": model_source,
        "revision": revision,
        "revision_source": revision_source,
        "reason": None if status == PRESENT else "board model or revision was not readable",
    }


def _probe_cpu(
    cpuinfo: Mapping[str, object], uname_value: object | None, cpu_count_fn: Callable[[], int | None]
) -> dict[str, object]:
    model = cpuinfo.get("model") or (
        getattr(uname_value, "processor", None) if uname_value is not None else None
    )
    logical_cpus = cpuinfo.get("logical_cpus")
    if not logical_cpus:
        try:
            logical_cpus = cpu_count_fn()
        except OSError:
            logical_cpus = None
    status = PRESENT if model and logical_cpus else UNVERIFIED
    return {
        "status": status,
        "model": model,
        "hardware": cpuinfo.get("hardware"),
        "logical_cpus": logical_cpus,
        "reason": None if status == PRESENT else "CPU model or logical CPU count was unavailable",
    }


def _probe_memory(reader: Reader) -> dict[str, object]:
    raw = _safe_read(reader, "/proc/meminfo")
    total = parse_meminfo(_decode(raw))["total_bytes"] if raw is not None else None
    return {
        "status": PRESENT if total else UNVERIFIED,
        "total_bytes": total,
        "reason": None if total else "MemTotal was not readable from /proc/meminfo",
    }


def _probe_storage(disk_usage_fn: Callable[[str], object]) -> dict[str, object]:
    try:
        usage = disk_usage_fn("/")
        total, used, free = int(usage[0]), int(usage[1]), int(usage[2])  # type: ignore[index]
        if total <= 0:
            raise ValueError("storage total is zero")
    except (OSError, TypeError, ValueError) as exc:
        return {
            "status": UNVERIFIED,
            "path": "/",
            "total_bytes": None,
            "used_bytes": None,
            "free_bytes": None,
            "used_percent": None,
            "reason": str(exc),
        }
    return {
        "status": PRESENT,
        "path": "/",
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "used_percent": round(used * 100 / total, 1),
        "reason": None,
    }


def _probe_audio(runner: Runner) -> dict[str, object]:
    result = runner(("arecord", "-l"))
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    devices = parse_arecord_devices(combined)
    if devices:
        status = PRESENT
        reason = None
    elif "no soundcards found" in combined.lower() or result.returncode == 0:
        status = MISSING
        reason = "arecord found no ALSA hardware capture devices"
    else:
        status = UNVERIFIED
        reason = result.error or _command_error(result) or "arecord output was not understood"
    return {
        "status": status,
        "tool": "arecord",
        "capture_devices": devices,
        "reason": reason,
    }


def _probe_packages(runner: Runner) -> dict[str, object]:
    result = runner(("apt-cache", "policy", *APT_PACKAGES))
    items: dict[str, dict[str, object]] = {}
    if result.returncode == 0:
        policy = parse_apt_policy(result.stdout)
        for name, versions in policy.items():
            installed = versions["installed_version"]
            candidate = versions["candidate_version"]
            available = bool(installed or candidate)
            items[name] = {
                "status": PRESENT if available else MISSING,
                "installed": installed is not None,
                "installed_version": installed,
                "candidate_version": candidate,
                "reason": None if available else "APT reports no installed or candidate version",
            }
    else:
        reason = result.error or _command_error(result) or "apt-cache policy failed"
        for name in APT_PACKAGES:
            items[name] = {
                "status": UNVERIFIED,
                "installed": None,
                "installed_version": None,
                "candidate_version": None,
                "reason": reason,
            }
    return {
        "status": _combine_status(*items.values()),
        "manager": "apt",
        "items": items,
        "reason": None if result.returncode == 0 else result.error or _command_error(result),
    }


def _probe_gstreamer(
    runner: Runner, *, registry_update_disabled: bool = True
) -> dict[str, object]:
    launch = _probe_version_tool(runner, "gst-launch-1.0")
    inspect = _probe_version_tool(runner, "gst-inspect-1.0")
    tools = {"gst-launch-1.0": launch, "gst-inspect-1.0": inspect}
    tools_status = {"status": _combine_status(launch, inspect)}

    all_elements = REQUIRED_GSTREAMER_ELEMENTS + SPIKE_GSTREAMER_ELEMENTS
    elements: dict[str, dict[str, object]] = {}
    plugins: dict[str, dict[str, object]] = {}
    if inspect["status"] == PRESENT:
        listing = runner(("gst-inspect-1.0",))
        if listing.returncode == 0:
            factories = parse_gst_element_list(listing.stdout)
            plugin_names = sorted(
                {factories[name] for name in all_elements if name in factories}
            )
            for plugin_name in plugin_names:
                plugin_result = runner(("gst-inspect-1.0", plugin_name))
                details = (
                    parse_gst_plugin_details(plugin_result.stdout)
                    if plugin_result.returncode == 0
                    else {"name": None, "version": None}
                )
                version = details["version"]
                verified = bool(version)
                plugins[plugin_name] = {
                    "status": PRESENT if verified else UNVERIFIED,
                    "version": version,
                    "reason": (
                        None
                        if verified
                        else plugin_result.error
                        or _command_error(plugin_result)
                        or "gst-inspect returned no plugin version"
                    ),
                }
            for name in all_elements:
                plugin = factories.get(name)
                plugin_data = plugins.get(plugin) if plugin else None
                if plugin is None:
                    elements[name] = {
                        "status": MISSING,
                        "availability_status": MISSING,
                        "plugin": None,
                        "plugin_version": None,
                        "reason": "element factory not listed by gst-inspect-1.0",
                    }
                else:
                    assert plugin_data is not None
                    elements[name] = {
                        "status": plugin_data["status"],
                        "availability_status": PRESENT,
                        "plugin": plugin,
                        "plugin_version": plugin_data["version"],
                        "reason": plugin_data["reason"],
                    }
        else:
            reason = listing.error or _command_error(listing) or "gst-inspect listing failed"
            for name in all_elements:
                elements[name] = {
                    "status": UNVERIFIED,
                    "availability_status": UNVERIFIED,
                    "plugin": None,
                    "plugin_version": None,
                    "reason": reason,
                }
    else:
        for name in all_elements:
            elements[name] = {
                "status": UNVERIFIED,
                "availability_status": UNVERIFIED,
                "plugin": None,
                "plugin_version": None,
                "reason": "gst-inspect-1.0 is unavailable",
            }

    required = {name: elements[name] for name in REQUIRED_GSTREAMER_ELEMENTS}
    spike = {name: elements[name] for name in SPIKE_GSTREAMER_ELEMENTS}
    required_status = {"status": _combine_status(*required.values())}
    spike_status = {"status": _combine_status(*spike.values())}
    return {
        "status": _combine_status(tools_status, required_status, spike_status),
        "registry_update_disabled": registry_update_disabled,
        "tools_status": tools_status,
        "tools": tools,
        "plugins": plugins,
        "required_status": required_status,
        "required_elements": required,
        "spike_status": spike_status,
        "spike_elements": spike,
    }


def _probe_version_tool(runner: Runner, name: str) -> dict[str, object]:
    result = runner((name, "--version"))
    match = re.search(r"(?:version|GStreamer)\s+(\d+(?:\.\d+)+)", result.stdout)
    if result.returncode == 0:
        return {
            "status": PRESENT,
            "version": match.group(1) if match else None,
            "reason": None,
        }
    if result.returncode is None and result.error == "command not found":
        return {"status": MISSING, "version": None, "reason": result.error}
    return {
        "status": UNVERIFIED,
        "version": None,
        "reason": result.error or _command_error(result) or "version command failed",
    }


def _actions(result: Mapping[str, object]) -> list[str]:
    actions: list[str] = []
    platform_result = result["platform"]
    hardware = result["hardware"]
    storage = result["storage"]
    audio = result["audio"]
    packages = result["packages"]
    gstreamer = result["gstreamer"]
    assert all(
        isinstance(item, Mapping)
        for item in (platform_result, hardware, storage, audio, packages, gstreamer)
    )

    if platform_result["os_release"]["status"] != PRESENT:  # type: ignore[index]
        actions.append("Run on Raspberry Pi OS/Linux where /etc/os-release is readable.")
    if platform_result["kernel"]["status"] != PRESENT:  # type: ignore[index]
        actions.append("Run `uname -srm` successfully to verify the kernel and architecture.")
    if platform_result["userland_bitness"]["status"] != PRESENT:  # type: ignore[index]
        actions.append("Run `getconf LONG_BIT` to verify userland bitness.")
    if platform_result["dpkg_architecture"]["status"] != PRESENT:  # type: ignore[index]
        actions.append("Run on a Debian-family host with dpkg to verify userland architecture.")
    baseline = platform_result.get("os_architecture_baseline")
    if isinstance(baseline, Mapping):
        if baseline["status"] == MISSING:
            actions.append(f"Use the supported OS/architecture baseline: {baseline['target']}.")
        elif baseline["status"] == UNVERIFIED:
            actions.append(f"Verify the OS/architecture baseline is {baseline['target']}.")
    if hardware["board"]["status"] != PRESENT:  # type: ignore[index]
        actions.append("Run on the target Pi to record its board model and revision.")
    if hardware["cpu"]["status"] != PRESENT:  # type: ignore[index]
        actions.append("Make `/proc/cpuinfo` readable on the target Linux host to verify CPU details.")
    if hardware["memory"]["status"] != PRESENT:  # type: ignore[index]
        actions.append("Make `/proc/meminfo` readable on the target Linux host to verify RAM.")
    if storage["status"] != PRESENT:
        actions.append("Make the root filesystem readable to verify storage capacity.")
    if audio["status"] == MISSING:
        actions.append("Connect or enable an ALSA capture device, then verify it with `arecord -l`.")
    elif audio["status"] == UNVERIFIED:
        actions.append("Install/verify `alsa-utils`, then rerun `arecord -l`.")

    package_items = packages["items"]
    assert isinstance(package_items, Mapping)
    missing_packages = [
        name for name, item in package_items.items() if item["status"] == MISSING  # type: ignore[index]
    ]
    if missing_packages:
        actions.append(
            "Enable the appropriate Raspberry Pi OS APT repositories or resolve unavailable packages: "
            + ", ".join(missing_packages)
            + "."
        )
    elif packages["status"] == UNVERIFIED:
        actions.append("Run `apt-cache policy` on Raspberry Pi OS to verify required package candidates.")

    tools = gstreamer["tools"]
    assert isinstance(tools, Mapping)
    missing_tools = sorted(
        name for name, item in tools.items() if item["status"] == MISSING  # type: ignore[index]
    )
    if missing_tools:
        actions.append(
            "Install `gstreamer1.0-tools` to provide: " + ", ".join(missing_tools) + "."
        )
    elif gstreamer["tools_status"]["status"] == UNVERIFIED:  # type: ignore[index]
        actions.append("Repair the GStreamer command-line tools and rerun the probe.")

    element_items: dict[str, object] = {}
    element_items.update(gstreamer["required_elements"])  # type: ignore[arg-type]
    element_items.update(gstreamer["spike_elements"])  # type: ignore[arg-type]
    missing_elements = [
        name for name, item in element_items.items() if item["status"] == MISSING  # type: ignore[index]
    ]
    if missing_elements:
        grouped: dict[str, list[str]] = {}
        for element in missing_elements:
            grouped.setdefault(ELEMENT_PACKAGES[element], []).append(element)
        details = "; ".join(
            f"{package} ({', '.join(names)})" for package, names in grouped.items()
        )
        actions.append("Install/repair GStreamer plugin packages for missing elements: " + details + ".")
    elif gstreamer["required_status"]["status"] == UNVERIFIED or gstreamer[  # type: ignore[index]
        "spike_status"
    ]["status"] == UNVERIFIED:  # type: ignore[index]
        actions.append("Rerun `gst-inspect-1.0` successfully to verify required element factories.")
    return actions


def _combine_status(*items: Mapping[str, object]) -> str:
    statuses = {str(item.get("status")) for item in items}
    if MISSING in statuses:
        return MISSING
    if UNVERIFIED in statuses:
        return UNVERIFIED
    return PRESENT


def _safe_read(reader: Reader, path: str) -> bytes | None:
    try:
        return reader(path)
    except OSError:
        return None


def _decode(value: bytes | None) -> str:
    return value.decode("utf-8", errors="replace") if value is not None else ""


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _command_error(result: CommandResult) -> str | None:
    return (result.stderr or result.stdout).strip() or None


def _human_line(label: str, item: Mapping[str, object], detail: str) -> str:
    reason = f" ({item['reason']})" if item.get("reason") else ""
    return f"{label}: {str(item['status']).upper()} - {detail}{reason}"


def _human_count_line(
    label: str, status_item: Mapping[str, object] | object, items: Mapping[str, object]
) -> str:
    assert isinstance(status_item, Mapping)
    counts = {state: 0 for state in (PRESENT, MISSING, UNVERIFIED)}
    for item in items.values():
        assert isinstance(item, Mapping)
        counts[str(item["status"])] += 1
    return (
        f"{label}: {str(status_item['status']).upper()} - "
        f"{counts[PRESENT]} present, {counts[MISSING]} missing, {counts[UNVERIFIED]} unverified"
    )


def _format_bytes(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "unknown"
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(number) < 1024 or unit == "TiB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
