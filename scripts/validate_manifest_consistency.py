#!/usr/bin/env python3
"""Static consistency checks for the InternVLA-N1 deployment manifest.

These are the failure modes that are invisible until the stack is already
booting -- or worse, until the robot is armed:

  * a command-chain topic that nothing consumes, so the wheels never move;
  * a camera topic that no camera publishes, so dry-run looks like a policy
    failure instead of a wiring one;
  * a guard looser (or tighter) than the layer above it, which silently
    re-clips every command at a number nobody chose;
  * a provider the body description never declares;
  * a config key the package silently drops.

All of them are cheap to catch offline.  The checker needs no ROS graph, no
camera, no network and no .env, so it is safe to run while the navigation stack
is live.  Exit status is non-zero if any check fails.
"""

from __future__ import annotations

from dataclasses import fields
import math
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml


DEPLOY_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DEPLOY_DIR / "packages" / "internvln_navigation"))

from internvln_navigation.core import (  # noqa: E402
    GUARD_MIN_TURN_RADIUS_M,
    MAX_CONFIGURED_VX,
    MAX_CONFIGURED_WZ,
    RuntimeConfig,
)
from internvln_navigation.http_client import (  # noqa: E402
    IMAGE_FIELD,
    IMAGE_FILENAME,
)

PRIMITIVE = "internvln_navigation"

# Values the manifest may legitimately reference before anything is configured.
# A missing INTERNVLA_SERVER_URL is a start.sh failure, not a manifest defect,
# so the checker substitutes a valid placeholder and reports it as a note.
_EXPANSION_DEFAULTS = {
    "ROBONIX_DEPLOY_DIR": str(DEPLOY_DIR),
    "INTERNVLA_SERVER_URL": "http://127.0.0.1:5801",
}

# The server's whole surface, plus the three route names an older revision of
# this package invented and the deployment documents never had.  A base url
# carrying any of them would be requested as e.g. /eval_dual/eval_dual.
_ENDPOINT_SUFFIXES = (
    "/eval_dual",
    "/health",
    "/task/start",
    "/task/report",
)


class Failure(Exception):
    pass


def _load(name: str):
    with open(DEPLOY_DIR / name, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _expand(value, env: dict):
    if not isinstance(value, str):
        return value
    for key, replacement in env.items():
        value = value.replace(f"${key}", str(replacement))
        value = value.replace(f"${{{key}}}", str(replacement))
    return value


def _expansion_env() -> dict:
    env = dict(_EXPANSION_DEFAULTS)
    env.update({key: value for key, value in os.environ.items() if value})
    return env


def _primitive_config(manifest) -> dict:
    return next(
        p for p in manifest["primitive"] if p["name"] == PRIMITIVE
    )["config"]


def _providers(document) -> set[str]:
    return {
        entry["name"] for entry in (document.get("primitive") or [])
    } | {entry["name"] for entry in (document.get("service") or [])}


def _soma_providers(soma) -> set[str]:
    found: set[str] = set()

    def walk(components):
        for component in components or []:
            for export in component.get("exports") or []:
                found.add(export["provider_id"])
            walk(component.get("components"))

    for export in soma["robot"].get("exports") or []:
        found.add(export["provider_id"])
    walk(soma["robot"].get("components"))
    return found


def check_command_chain(manifest, config) -> list[str]:
    """Every hop of the velocity chain must join up.

    The chain has one fewer stage than the reference profile: the VLN primitive
    already paces its own output, so there is no Nav2 velocity smoother between
    the mux and the guard.
    """

    notes: list[str] = []
    block = _primitive_config(manifest)

    if config.command_topic != config.vln_raw_topic:
        raise Failure(
            f"the client writes {config.command_topic!r} but the source mux "
            f"reads {config.vln_raw_topic!r}"
        )
    notes.append(f"model -> {config.command_topic} -> source mux")

    if config.selected_source not in ("vln", "navigation"):
        raise Failure(f"unsupported selected_source {config.selected_source!r}")

    if config.selected_output_topic == config.guard_output_topic:
        raise Failure(
            "the source mux and the chassis guard would both write "
            f"{config.guard_output_topic!r}"
        )
    if config.guard_input_topic != config.selected_output_topic:
        raise Failure(
            f"the source mux writes {config.selected_output_topic!r} but the "
            f"chassis guard reads {config.guard_input_topic!r}; nothing would "
            "reach the wheels"
        )
    notes.append(
        f"source mux (selected_source={config.selected_source}) -> "
        f"{config.selected_output_topic} -> chassis guard"
    )

    chassis = next(
        p for p in manifest["primitive"] if p["name"] == "ranger_chassis"
    )["config"]
    if chassis["cmd_vel_topic"] != config.guard_output_topic:
        raise Failure(
            f"the chassis subscribes {chassis['cmd_vel_topic']!r} but the guard "
            f"publishes {config.guard_output_topic!r}"
        )
    notes.append(f"chassis guard -> {chassis['cmd_vel_topic']} -> ranger_chassis")
    notes.append(f"raw manifest config carries {len(block)} keys")

    # Nothing else in the stack may drive the wheels.
    others = [
        p["name"]
        for p in manifest["primitive"]
        if p["name"] not in (PRIMITIVE, "ranger_chassis")
        and isinstance(p.get("config"), dict)
        and p["config"].get("cmd_vel_topic") == config.guard_output_topic
    ]
    if others:
        raise Failure(
            f"{others} also publish {config.guard_output_topic!r}; the chassis "
            "guard must be its only writer"
        )
    return notes


def check_config_keys(manifest, config) -> list[str]:
    """The manifest must not carry keys the package silently discards."""

    block = _primitive_config(manifest)
    known = {field.name for field in fields(RuntimeConfig)}
    # Consumed as deployment metadata by the provider rather than by
    # RuntimeConfig, plus the one nested mapping the config does understand.
    metadata = {"asset_manifest", "upstream_root", "source_mux"}
    dropped = sorted(set(block) - known - metadata)
    if dropped:
        raise Failure(f"manifest keys the package would ignore: {dropped}")

    # And the reverse: a key the deploy manifest forgets keeps the package
    # default, which for the live-only knobs is not what an operator expects.
    must_be_set = {
        "mode",
        "rgb_topic",
        "depth_topic",
        "command_topic",
        "server_url",
        "instruction",
        "plan_hold_s",
        "lockstep_enabled",
        "action_step_s",
        "step_settle_s",
        "max_vx",
        "max_wz",
        "safety_gate_enabled",
        "safety_gate_require_depth",
        "safety_gate_stop_m",
        "safety_gate_release_m",
        "safety_gate_depth_timeout_s",
        "guard_input_topic",
        "guard_output_topic",
        "guard_max_vx_mps",
        "guard_max_wz_rps",
    }
    missing = sorted(must_be_set - set(block))
    if missing:
        raise Failure(
            f"the manifest leaves {missing} to the package defaults; set them "
            "explicitly so the deployed behaviour is readable from one file"
        )
    return [f"all {len(block)} manifest config keys are recognized and explicit"]


def check_providers(manifest, soma) -> list[str]:
    """Every manifest provider must be declared by the body description."""

    declared = _soma_providers(soma)
    manifest_providers = _providers(manifest)
    # The VLN primitive registers through Atlas rather than the body
    # description, matching the reference deployments.
    self_registering = {PRIMITIVE}
    missing = sorted(manifest_providers - declared - self_registering)
    if missing:
        raise Failure(f"manifest providers absent from soma.yaml: {missing}")
    extra = sorted(declared - manifest_providers)
    if extra:
        raise Failure(f"soma.yaml declares providers the manifest lacks: {extra}")
    return [f"{len(declared)} providers declared by soma.yaml"]


def check_camera_wiring(manifest, config) -> list[str]:
    """The RGB topic the model reads must be the one the camera publishes.

    The Orbbec primitive namespaces every topic under its ``camera_name``, so
    the two names appear in different files and drift is easy.  A mismatch does
    not raise anywhere: the node subscribes to silence, no frame ever reaches
    the model, and dry-run looks like a broken policy.
    """

    camera = next(
        p for p in manifest["primitive"] if p["name"] == "front_camera"
    )["config"]
    if not camera.get("enable_color", True):
        raise Failure("front_camera has enable_color off; the model needs RGB")
    expected = f"/{camera['camera_name']}/color/image_raw"
    if config.rgb_topic != expected:
        raise Failure(
            f"the VLN primitive subscribes {config.rgb_topic!r} but the camera "
            f"publishes {expected!r} (camera_name={camera['camera_name']!r})"
        )
    notes = [f"front_camera camera_name -> {config.rgb_topic} -> model"]
    depth_enabled = bool(camera.get("enable_depth"))
    if config.safety_gate_enabled:
        if not depth_enabled:
            raise Failure(
                "the proximity gate is enabled but front_camera has "
                "enable_depth off; with require_depth the robot would never "
                "move, and without it there is no proximity reflex at all"
            )
        expected_depth = f"/{camera['camera_name']}/depth/image_raw"
        if config.depth_topic != expected_depth:
            raise Failure(
                f"the proximity gate subscribes {config.depth_topic!r} but the "
                f"camera publishes {expected_depth!r} "
                f"(camera_name={camera['camera_name']!r})"
            )
        notes.append(
            f"front_camera depth -> {config.depth_topic} -> proximity gate "
            f"(stop < {config.safety_gate_stop_m} m, "
            f"release > {config.safety_gate_release_m} m)"
        )
    elif depth_enabled:
        raise Failure(
            "front_camera publishes depth but the proximity gate is disabled; "
            "nothing in this profile would consume it, and the robot would "
            "have no proximity reflex"
        )
    return notes


def check_server_url(manifest, config) -> list[str]:
    """The remote model address must be a base URL, not one endpoint."""

    block = _primitive_config(manifest)
    raw = block.get("server_url", "")
    notes: list[str] = []
    if isinstance(raw, str) and "${" in raw:
        name = raw.strip().strip("${}")
        if os.environ.get(name):
            notes.append(f"server_url from ${name}")
        else:
            notes.append(
                f"${name} is unset here (checked against a placeholder); "
                "start.sh requires it before booting"
            )
    for suffix in _ENDPOINT_SUFFIXES:
        if config.server_url.endswith(suffix):
            raise Failure(
                f"server_url {config.server_url!r} ends with {suffix!r}; the "
                "client appends /eval_dual itself and every call would 404"
            )
    parts = urlsplit(config.server_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise Failure(f"server_url {config.server_url!r} is not an http(s) URL")
    if parts.path not in ("", "/"):
        notes.append(f"server_url carries path {parts.path!r}")
    notes.append(
        f"the one route is POST /eval_dual; frame part is "
        f"{IMAGE_FIELD!r} (filename {IMAGE_FILENAME!r})"
    )
    return notes


def check_vln_profile(config) -> list[str]:
    """Report the armed state and check the horizon that scales every step."""

    problems = []
    if config.mode not in ("dry-run", "live"):
        problems.append(f"unknown mode {config.mode!r}")
    if config.guard_input_topic == config.guard_output_topic:
        problems.append("the chassis guard reads and writes the same topic")

    # plan_hold_s is the watchdog: how long a plan may keep commanding motion.
    # action_step_s is what divides a discrete step into a velocity.  A
    # watchdog shorter than the model's own answer cadence makes the robot
    # pulse; one longer than a step is fine but should stay bounded.
    if config.plan_hold_s < 0.5:
        problems.append(
            f"plan_hold_s is {config.plan_hold_s}; DualVLN answers every ~0.5-3 s, "
            "so anything shorter makes the robot pulse"
        )
    if config.plan_hold_s > 3.0:
        problems.append(
            f"plan_hold_s is {config.plan_hold_s}; one action would be held long "
            "enough to overrun the step the model asked for"
        )
    if config.action_step_s > config.plan_hold_s + 1e-9:
        problems.append(
            f"action_step_s ({config.action_step_s}) exceeds plan_hold_s "
            f"({config.plan_hold_s}); the watchdog would end every step early"
        )
    if config.lockstep_enabled and config.plan_hold_s < config.action_step_s:
        problems.append("lockstep needs plan_hold_s >= action_step_s")
    if problems:
        raise Failure("; ".join(problems))

    forward = config.discrete_forward_m / config.action_step_s
    yaw = math.radians(config.discrete_step_deg) / config.action_step_s
    # action_step_s is the *nominal* duration.  What the robot actually covers
    # is discrete_forward_m / discrete_step_deg; a speed cap stretches the hold
    # instead of shortening the move.
    forward_hold = config.discrete_forward_m / min(forward, config.max_vx)
    yaw_hold = math.radians(config.discrete_step_deg) / min(yaw, config.max_wz)
    notes = [
        f"command mode is {config.mode!r}"
        + (" (no velocity is published)" if config.mode == "dry-run" else " (ARMED)"),
        f"discrete forward step -> {min(forward, config.max_vx):.4f} m/s for "
        f"{forward_hold:.3f} s = {config.discrete_forward_m} m",
        f"discrete turn step    -> {min(yaw, config.max_wz):.4f} rad/s for "
        f"{yaw_hold:.3f} s = {config.discrete_step_deg} deg",
        f"plan expires {config.plan_hold_s} s after its last update (watchdog), "
        "or when the program it carries has finished",
        f"one answer commits at most {config.max_chunk_actions} action(s), "
        "executed in order (the rest of the chunk waits for a fresh frame)",
    ]
    if config.servo_enabled:
        notes.append(
            "servo ON: each answer becomes a world goal measured from the pose "
            f"at the moment the answer lands; the {config.dispatch_hz:.0f} Hz tick "
            f"servos toward it (Kp {config.servo_kp_trans}/{config.servo_kp_yaw}) "
            "while the next request is in flight -- motion is continuous"
        )
    if config.lockstep_enabled:
        notes.append(
            "lockstep ON: the program is driven segment by segment, then zero "
            f"for {config.step_settle_s} s while the chassis stops; no frame is "
            "captured and no request is sent until it has"
        )
    elif not config.servo_enabled:
        notes.append(
            "lockstep OFF: the plan is re-commanded until plan_hold_s expires, "
            "so the robot executes a view that is already stale"
        )
    if config.servo_enabled and config.lockstep_enabled:
        notes.append(
            "NOTE servo and lockstep are both on: lockstep no longer gates the "
            "worker, it only paces the timed fallback (STOP, trajectories)"
        )
    if forward > config.max_vx + 1e-9:
        notes.append(
            f"NOTE forward step ({forward:.4f} m/s) exceeds max_vx "
            f"({config.max_vx}); each forward action is driven slower for "
            f"{forward_hold:.2f} s so it still covers {config.discrete_forward_m} m"
        )
    if yaw > config.max_wz + 1e-9:
        notes.append(
            f"NOTE turn step ({yaw:.4f} rad/s) exceeds max_wz ({config.max_wz}); "
            f"each turn is driven slower for {yaw_hold:.2f} s so it still covers "
            f"{config.discrete_step_deg} deg"
        )

    # The trajectory controller drives the model's own path at a bounded
    # forward speed (``trajectory_cruise_vx``) with the goal no further than
    # ``trajectory_advance_m`` away, commanding the pure-pursuit yaw
    # (wz = 2 vx sin(bearing) / L) whose implied radius is L / (2 sin(bearing)).
    # The bearing at which that radius becomes tighter than the chassis minimum
    # turn radius is where the controller stops arcing and pivots instead, so
    # print it: it is the number that decides whether a curved path gets driven
    # or spun through.
    arc_forward = min(config.trajectory_cruise_vx, config.max_vx)
    pivot_at_deg = math.degrees(
        math.asin(
            min(1.0, config.lookahead_m / (2.0 * config.guard_min_turn_radius_m))
        )
    )
    notes.append(
        f"trajectory arc        -> {arc_forward:.4f} m/s (cruise "
        f"{config.trajectory_cruise_vx}, capped by max_vx) over at most "
        f"{config.trajectory_advance_m} m of advance (pure pursuit, "
        f"radius L/(2 sin a)); past +/-{pivot_at_deg:.1f} deg it pivots in "
        "place instead"
    )
    if config.trajectory_cruise_vx > config.max_vx + 1e-9:
        notes.append(
            f"NOTE trajectory cruise ({config.trajectory_cruise_vx} m/s) "
            f"exceeds max_vx ({config.max_vx}); the forward speed is capped, "
            "which tightens the arc the model asked for rather than slowing it"
        )
    return notes


def check_guard_limits(config) -> list[str]:
    """The chassis guard must be the tightest limit, and must fire first.

    There is no Nav2 velocity smoother above it in this profile -- the package's
    own plan store is what paces the output -- so the guard is compared against
    the package's caps instead.
    """

    problems = []
    for name, guard, packaged, ceiling in (
        ("linear", config.guard_max_vx_mps, config.max_vx, MAX_CONFIGURED_VX),
        ("angular", config.guard_max_wz_rps, config.max_wz, MAX_CONFIGURED_WZ),
    ):
        if guard < packaged - 1e-9:
            problems.append(
                f"guard max {name} {guard} is tighter than the package cap "
                f"{packaged}; commands would be clipped twice, at two numbers"
            )
        if guard > ceiling + 1e-9:
            problems.append(
                f"guard max {name} {guard} exceeds the chassis ceiling {ceiling}"
            )
    # The AgileX firmware watchdog stops the wheels 500 ms after the last CAN
    # frame.  The guard's own timeout must fire first, or going quiet produces a
    # firmware cut instead of a commanded zero.
    if not 0.0 < config.guard_command_timeout_s < 0.5:
        problems.append(
            f"guard_command_timeout_s {config.guard_command_timeout_s} must be "
            "below the 500 ms firmware watchdog"
        )
    if config.guard_publish_hz * config.guard_command_timeout_s < 3.0:
        problems.append(
            f"at {config.guard_publish_hz} Hz only "
            f"{config.guard_publish_hz * config.guard_command_timeout_s:.1f} "
            "frames fit inside guard_command_timeout_s; the guard would time out "
            "between its own publishes"
        )
    if config.guard_min_turn_radius_m != GUARD_MIN_TURN_RADIUS_M:
        problems.append(
            f"guard_min_turn_radius_m is {config.guard_min_turn_radius_m}, "
            f"expected the vendor figure {GUARD_MIN_TURN_RADIUS_M}"
        )
    if problems:
        raise Failure("; ".join(problems))
    return [
        f"guard {config.guard_max_vx_mps} m/s <= chassis ceiling "
        f"{MAX_CONFIGURED_VX} m/s (package cap {config.max_vx})",
        f"guard {config.guard_max_wz_rps} rad/s <= chassis ceiling "
        f"{MAX_CONFIGURED_WZ} rad/s (package cap {config.max_wz})",
        f"guard times out at {config.guard_command_timeout_s} s, before the "
        "500 ms firmware watchdog",
    ]


def check_no_dead_stack(manifest) -> list[str]:
    """Only the voice providers may sit off the DualVLN command path."""

    services = [entry["name"] for entry in manifest.get("service") or []]
    unexpected_services = sorted(set(services) - {SPEECH_SERVICE})
    if unexpected_services:
        raise Failure(
            f"the manifest starts services {unexpected_services}; mapping and "
            "Nav2 are not needed by this profile, and the VLN primitive paces "
            "its own output"
        )
    allowed = {
        "robot_description",
        "front_camera",
        "ranger_chassis",
        PRIMITIVE,
        VOICE_PRIMITIVE,
        SPEECH_SERVICE,
    }
    unexpected = sorted(_providers(manifest) - allowed)
    if unexpected:
        raise Failure(
            f"unexpected providers {unexpected}; this profile is "
            f"{sorted(allowed)} and nothing else"
        )
    return [
        f"services {services or 'none'}; {len(_providers(manifest))} providers, "
        "voice is the only one off the command path"
    ]


VOICE_PRIMITIVE = "audio_client_bridge"
SPEECH_SERVICE = "speech"


def check_voice_wiring(manifest, soma) -> list[str]:
    """Voice is optional, but it must be wired all the way or not at all.

    Half a voice stack fails silently: the microphone opens and nobody listens,
    or speak() is called and there is no speaker provider to render it.
    """

    primitives = {entry["name"]: entry for entry in manifest.get("primitive") or []}
    services = {entry["name"]: entry for entry in manifest.get("service") or []}
    audio = primitives.get(VOICE_PRIMITIVE)
    speech = services.get(SPEECH_SERVICE)
    if (audio is None) != (speech is None):
        raise Failure(
            f"voice is half-wired: {VOICE_PRIMITIVE!r} present="
            f"{audio is not None}, {SPEECH_SERVICE!r} present="
            f"{speech is not None}; add both or neither"
        )
    if audio is None:
        return ["voice: not configured"]

    audio_config = audio.get("config") or {}
    transport = audio_config.get("transport", "reverse")
    if transport != "reverse":
        raise Failure(
            f"{VOICE_PRIMITIVE} transport is {transport!r}; reverse is the "
            "only mode where the robot does not need the client's address"
        )
    try:
        port = int(audio_config.get("listen_port", 60002))
    except (TypeError, ValueError) as error:
        raise Failure(f"{VOICE_PRIMITIVE} listen_port is not a number: {error}")
    if not 1 <= port <= 65535:
        raise Failure(f"{VOICE_PRIMITIVE} listen_port {port} is not a TCP port")

    speaker = (speech.get("config") or {}).get("default_speaker_provider_id")
    if speaker != VOICE_PRIMITIVE:
        raise Failure(
            f"speech default_speaker_provider_id is {speaker!r}; it must name "
            f"{VOICE_PRIMITIVE!r} or speak() has no device to play on"
        )

    declared = _soma_providers(soma)
    missing = sorted({VOICE_PRIMITIVE, SPEECH_SERVICE} - declared)
    if missing:
        raise Failure(
            f"soma.yaml does not declare {missing}; the body description is "
            "how the microphone, speaker and speech service become reachable"
        )
    return [
        f"voice: client audio on 0.0.0.0:{port} (reverse), "
        f"speech -> {speaker}"
    ]


def main() -> int:
    manifest = _load("robonix_manifest.yaml")
    soma = _load("soma.yaml")
    block = _primitive_config(manifest)
    env = _expansion_env()
    expanded = {key: _expand(value, env) for key, value in block.items()}
    config = RuntimeConfig.from_mapping(expanded)

    checks = (
        ("command chain", lambda: check_command_chain(manifest, config)),
        ("config keys", lambda: check_config_keys(manifest, config)),
        ("providers", lambda: check_providers(manifest, soma)),
        ("camera wiring", lambda: check_camera_wiring(manifest, config)),
        ("model server", lambda: check_server_url(manifest, config)),
        ("vln profile", lambda: check_vln_profile(config)),
        ("guard limits", lambda: check_guard_limits(config)),
        ("voice wiring", lambda: check_voice_wiring(manifest, soma)),
        ("no dead stack", lambda: check_no_dead_stack(manifest)),
    )

    failed = 0
    for name, run in checks:
        try:
            notes = run()
        except Failure as error:
            failed += 1
            print(f"[FAIL] {name}: {error}")
            continue
        except (TypeError, ValueError) as error:
            failed += 1
            print(f"[FAIL] {name}: the package rejected this config: {error}")
            continue
        for note in notes:
            print(f"[ OK ] {name}: {note}")

    print()
    if failed:
        print(f"manifest consistency FAILED ({failed} check(s))")
        return 1
    print("manifest consistency OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
