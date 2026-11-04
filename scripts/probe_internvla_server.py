#!/usr/bin/env python3
"""Exercise the DualVLN model server against the real model.

This is the tool to reach for before the robot ever moves, and the one to reach
for when it moves wrongly.  It drives the SAME client the robot uses
(``internvln_navigation.http_client``), so a protocol problem, a wrong image
field name or an unexpected response shape shows up here rather than as a robot
that sits still.

    python3 scripts/probe_internvla_server.py \
        --url http://10.0.105.11:5801 \
        --instruction "Go to the chair and stop." \
        --image /tmp/frame.jpg

The server publishes exactly one route, ``POST /eval_dual`` -- there is no
liveness route to check first, which is why this probe's first frame *is* the
liveness check.  The request it builds is the one ``internVLN/vln_query.py``
uses: a ``json`` form field carrying ``{"reset": bool, "instruction": str}``
plus one JPEG part named ``image``.

Without ``--image`` it renders a synthetic frame with cv2 (or PIL) -- the model
will answer nonsense for it, but the point of that run is to prove the wire
protocol, not the policy.  Nothing here touches ROS, the CAN bus or the wheels,
so it is safe to run while the stack is live and even while the robot is armed.

Exit status is non-zero if any step fails.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "packages" / "internvln_navigation"))

from internvln_navigation.core import (  # noqa: E402
    EncodedFrame,
    ModelUnavailableError,
    ProtocolError,
    RuntimeConfig,
)
from internvln_navigation.http_client import (  # noqa: E402
    IMAGE_FIELD,
    HttpTransport,
    InternVLAClient,
)

# fault_after_failures is pinned to 1 below so a probe surfaces the very first
# problem instead of retrying three times, which is what makes a dead server
# obvious in one frame rather than three.  It also means the escalation error is
# the normal one here, hence ModelUnavailableError alongside ProtocolError.
PROBE_ERRORS = (ProtocolError, ModelUnavailableError)


class RecordingTransport:
    """The real socket, plus a transcript of what crossed it."""

    def __init__(self) -> None:
        self._inner = HttpTransport()
        self.exchanges: list[tuple[str, str, int, bytes]] = []

    def __call__(self, method, url, body, headers, timeout_s):
        started = time.monotonic()
        try:
            status, payload = self._inner(method, url, body, headers, timeout_s)
        except Exception as error:
            # A refused connection is the single most common result here, and it
            # is exactly the case where the transcript matters most, so record
            # it too (status 0 == nothing came back).
            elapsed = time.monotonic() - started
            self.exchanges.append(
                (method, url, 0, f"{type(error).__name__}: {error}".encode("utf-8"))
            )
            print(
                f"    {method:<4} {url}  -> no response: {type(error).__name__}: "
                f"{error}  ({elapsed * 1000:.0f} ms)"
            )
            raise
        elapsed = time.monotonic() - started
        self.exchanges.append((method, url, status, payload))
        print(
            f"    {method:<4} {url}  -> HTTP {status}  "
            f"({len(payload)} B, {elapsed * 1000:.0f} ms)"
        )
        return status, payload

    def close(self) -> None:
        self._inner.close()


def _encode_jpeg(path: str | None, size: int, quality: int) -> bytes:
    if path:
        data = Path(path).read_bytes()
        if not data.startswith(b"\xff\xd8"):
            raise SystemExit(f"{path} is not a JPEG (no SOI marker)")
        return data

    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        try:
            from PIL import Image, ImageDraw  # type: ignore
        except ImportError:
            raise SystemExit(
                "no --image given and neither cv2 nor PIL is available to make "
                "one; pass --image with a real camera frame"
            ) from None
        image = Image.new("RGB", (size, size), (60, 90, 120))
        draw = ImageDraw.Draw(image)
        for step in range(0, size, 32):
            draw.line((0, step, size, step), fill=(200, 200, 200), width=2)
            draw.line((step, 0, step, size), fill=(200, 200, 200), width=2)
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
        return buffer.getvalue()

    canvas = np.zeros((size, size, 3), dtype=np.uint8)
    canvas[:, :] = (120, 90, 60)
    for step in range(0, size, 32):
        canvas[step : step + 2, :] = (200, 200, 200)
        canvas[:, step : step + 2] = (200, 200, 200)
    ok, encoded = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise SystemExit("cv2 failed to encode the synthetic frame")
    return encoded.tobytes()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="server BASE url, e.g. http://host:5801")
    parser.add_argument("--instruction", default="Go to the chair and stop.")
    parser.add_argument("--image", default=None, help="a real JPEG frame to send")
    parser.add_argument("--size", type=int, default=640, help="synthetic frame side")
    parser.add_argument("--quality", type=int, default=85)
    parser.add_argument("--frames", type=int, default=3, help="how many /eval_dual calls")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    transport = RecordingTransport()
    config = RuntimeConfig.from_mapping(
        {
            "mode": "dry-run",
            "server_url": args.url,
            "instruction": args.instruction,
            "jpeg_quality": args.quality,
            "http_timeout_s": args.timeout,
            "fault_after_failures": 1,
            # The probe is about the server's protocol, so keep the client's
            # extra behaviour out of it: no mid-run memory flush.  Otherwise a
            # run longer than 20 frames would send a reset of its own, the
            # printed "frame 0 carries reset=true, the rest false" would be a
            # lie, and "episodes opened" would count flushes as episodes.
            "history_reset_frames": 0,
        }
    )
    client = InternVLAClient(config, transport=transport)
    # `config.instruction` is only the fallback text; the client asks the model
    # for nothing until an operator instruction is *set* (that is what arms it,
    # and what the ROS node does when it receives one).  Without this call every
    # frame answers `awaiting_instruction` with vx=0 -- which still proves the
    # socket, and used to be mistaken for "the model refused us".
    client.set_instruction(args.instruction)

    jpeg = _encode_jpeg(args.image, args.size, args.quality)
    print(
        f"POST /eval_dual  x{args.frames}  instruction={args.instruction!r}  "
        f"({len(jpeg)} B JPEG, field {IMAGE_FIELD!r})"
    )
    print(
        "    frame 0 carries reset=true (start the episode); the rest carry "
        "reset=false (append to the history)"
    )

    evaluated = 0
    for index in range(args.frames):
        frame = EncodedFrame(
            sequence=index + 1,
            jpeg=jpeg,
            source_timestamp=time.time(),
            received_monotonic=time.monotonic(),
        )
        # Read the client's own flag rather than assuming: it is exactly what
        # this frame's `reset` field will carry, and it is the whole state
        # machine (first frame, instruction change, and the frame after a
        # failure all arm it).
        resets = bool(client.snapshot()["reset_pending"])
        started = time.monotonic()
        try:
            plan = client.evaluate(frame)
        except PROBE_ERRORS as error:
            print(f"    frame {index}: FAILED after {time.monotonic() - started:.2f}s: {error}")
            break
        elapsed = time.monotonic() - started
        evaluated += 1
        waypoints = plan.waypoints or ()
        print(
            f"    frame {index}: vx={plan.command.vx:+.4f} wz={plan.command.wz:+.4f} "
            f"source={plan.velocity_source} waypoints={len(waypoints)} "
            f"reset={resets} ({elapsed * 1000:.0f} ms)"
        )
        if waypoints:
            print(f"      first point {tuple(round(v, 3) for v in waypoints[0])}")

    print()
    if not evaluated:
        print(f"PROBE FAILED: /eval_dual produced no action in {args.frames} frame(s)")
        print("transcript:")
        for method, url, status, payload in transport.exchanges:
            outcome = f"HTTP {status}" if status else "no response"
            print(f"  {method} {url} -> {outcome} {payload[:300]!r}")
        client.close()
        return 1

    snapshot = client.snapshot()
    print("PROBE OK: the server speaks the protocol this client expects")
    print(f"  episodes opened:    {snapshot['episode']} (one per reset)")
    print(f"  frames evaluated:   {snapshot['requests']}")
    print(f"  instruction:        {snapshot['active_instruction']!r}")
    print(f"  last note:          {snapshot['last_note']}")

    # The raw body of the last answer, so an unexpected-but-accepted shape is
    # visible rather than absorbed by the adapter.
    payload = transport.exchanges[-1][3]
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        parsed = payload[:200].decode("utf-8", "replace")
    print(f"  last response body: {json.dumps(parsed, ensure_ascii=False)[:300]}")

    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())