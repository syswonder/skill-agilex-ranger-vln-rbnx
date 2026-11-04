"""Depth-based proximity stop gate.

Deliberately dumb, and deliberately independent of the model.

It does not know what the obstacle is, what the instruction says, or where the
robot is going.  It looks at one forward-only region of the depth image and,
when the nearest surface inside it is closer than ``stop_distance_m``, it says
"blocked"; the node then publishes zero velocity.  Nothing the model answers --
an arrow, a pixel goal, a trajectory -- can override it.

That is the whole point.  DualVLN is a vision policy, not a safety system: it
sees one RGB frame, has no depth, and can only react at the model's answer
cadence.  This module is the reflex that catches the case the policy misses.

The gate is fail-closed by default: no depth frame, a stale frame or a frame
with no usable pixels all mean "blocked".  A robot that refuses to move is a
deployment problem; a robot that drives into a wall is a safety incident.

Pure Python + numpy, no ROS, so the decision logic is unit-testable offline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import threading
import time
from typing import Any

import numpy as np


# The ROI is a horizontal band across the middle of the image.  It has to cover
# a wall or a person standing in front of the robot while excluding the floor
# immediately below the camera, which is always within a metre and would keep
# the gate permanently closed.  The defaults were chosen for a forward-facing
# camera at roughly 0.3-0.5 m: the horizon sits near the middle of the frame,
# so 25%-80% of the height is the obstacle band.
DEFAULT_ROI_TOP_FRACTION = 0.25
DEFAULT_ROI_BOTTOM_FRACTION = 0.80
DEFAULT_ROI_WIDTH_FRACTION = 0.60

# Depth below this is treated as sensor noise / a too-close return, not a
# surface.  It is far inside any chassis envelope worth commanding around.
MIN_VALID_DEPTH_M = 0.05
# Beyond this the reading is unusable anyway, and it keeps the percentile from
# being dragged by the odd flying pixel in a distant wall.
MAX_VALID_DEPTH_M = 5.0


@dataclass(frozen=True)
class SafetyGateConfig:
    """Tunables for the proximity gate.

    ``release_distance_m`` is deliberately larger than ``stop_distance_m``.
    Without that gap the gate chatters: it opens at 0.50 m, the robot creeps
    forward, the surface is instantly at 0.49 m again, and the base pulses.
    """

    enabled: bool = True
    require_depth: bool = True
    stop_distance_m: float = 0.30
    release_distance_m: float = 0.50
    depth_timeout_s: float = 0.30
    # 0 means "infer from the dtype": uint16 depth is millimetres, float depth
    # (Orbbec's own default and most ROS drivers) is metres.
    depth_scale_m: float = 0.0
    roi_top_fraction: float = DEFAULT_ROI_TOP_FRACTION
    roi_bottom_fraction: float = DEFAULT_ROI_BOTTOM_FRACTION
    roi_width_fraction: float = DEFAULT_ROI_WIDTH_FRACTION
    min_valid_pixels: int = 200
    # Which depth in the ROI counts as "the nearest surface".  The minimum is
    # too easy to trip with a single flying pixel; the 1st percentile keeps the
    # gate blind to a handful of outliers while still seeing a thin chair leg.
    percentile: float = 1.0

    def validate(self) -> "SafetyGateConfig":
        if not math.isfinite(self.stop_distance_m) or self.stop_distance_m <= 0.0:
            raise ValueError("safety_gate_stop_m must be positive")
        if not math.isfinite(self.release_distance_m):
            raise ValueError("safety_gate_release_m must be finite")
        if self.release_distance_m <= self.stop_distance_m:
            raise ValueError(
                "safety_gate_release_m must exceed safety_gate_stop_m, or the "
                "gate chatters at the threshold"
            )
        if not math.isfinite(self.depth_timeout_s) or self.depth_timeout_s <= 0.0:
            raise ValueError("safety_gate_depth_timeout_s must be positive")
        if not math.isfinite(self.depth_scale_m) or self.depth_scale_m < 0.0:
            raise ValueError("safety_gate_depth_scale_m must be >= 0")
        if not 0.0 < self.roi_width_fraction <= 1.0:
            raise ValueError("safety_gate_roi_width_fraction must be in (0, 1]")
        if not 0.0 <= self.roi_top_fraction < self.roi_bottom_fraction <= 1.0:
            raise ValueError(
                "safety_gate ROI must satisfy 0 <= top < bottom <= 1"
            )
        if self.min_valid_pixels < 1:
            raise ValueError("safety_gate_min_valid_pixels must be >= 1")
        if not 0.0 <= self.percentile <= 50.0:
            raise ValueError("safety_gate_percentile must be in [0, 50]")
        return self


@dataclass(frozen=True)
class SafetyGateDecision:
    """What the gate says right now."""

    blocked: bool
    reason: str
    nearest_m: float | None
    age_s: float | None
    valid_pixels: int
    stop_distance_m: float
    release_distance_m: float

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class _Observation:
    nearest_m: float | None
    valid_pixels: int
    monotonic: float


class DepthSafetyGate:
    """Latest depth observation plus a hysteretic blocked/unblocked state.

    ``observe`` runs on the depth callback and only reduces the frame to one
    number, so the 50 Hz dispatcher never touches a 640x480 array.

    The lock is cheap and makes the class safe if the node is ever moved to a
    multi-threaded executor; with the default single-threaded executor it is
    uncontended.
    """

    def __init__(self, config: SafetyGateConfig | None = None) -> None:
        self._config = (config or SafetyGateConfig()).validate()
        self._lock = threading.Lock()
        self._observation: _Observation | None = None
        self._blocked = False
        # True only when the closed state came from an actual near surface.
        # A fail-closed state caused by *missing* depth must not latch: the
        # first usable frame has to be judged on its own distance.  Otherwise a
        # surface at 0.98 m -- above the 0.80 m stop, below the 1.00 m release --
        # keeps the robot stopped forever after a single dropped frame.
        self._obstacle_latched = False
        self._reason = "no_depth" if self._config.require_depth else "clear"

    @property
    def config(self) -> SafetyGateConfig:
        return self._config

    def observe(
        self,
        depth: np.ndarray,
        *,
        now: float | None = None,
    ) -> SafetyGateDecision:
        """Reduce one depth frame to the nearest surface in the forward band.

        Raises ``ValueError`` for something that is not a 2-D depth image; the
        caller logs it and leaves the previous observation in place, which the
        age check will expire.
        """

        nearest, valid = _nearest_in_roi(depth, self._config)
        stamp = time.monotonic() if now is None else float(now)
        with self._lock:
            self._observation = _Observation(nearest, valid, stamp)
        return self.check(now=stamp)

    def invalidate(self) -> None:
        """Forget the observation; the next ``check`` fails closed."""

        with self._lock:
            self._observation = None

    def check(self, *, now: float | None = None) -> SafetyGateDecision:
        """Evaluate the gate without consuming a new frame."""

        current = time.monotonic() if now is None else float(now)
        cfg = self._config
        with self._lock:
            if not cfg.enabled:
                return self._decision(False, "disabled", None, 0)

            observation = self._observation
            if observation is None:
                self._blocked = cfg.require_depth
                self._reason = "no_depth"
                return self._decision(
                    self._blocked, self._reason, None, 0
                )

            age = max(0.0, current - observation.monotonic)
            if age > cfg.depth_timeout_s:
                self._blocked = cfg.require_depth
                self._reason = "stale_depth"
                return self._decision(
                    self._blocked,
                    self._reason,
                    observation.nearest_m,
                    observation.valid_pixels,
                    age,
                )

            if observation.valid_pixels < cfg.min_valid_pixels:
                self._blocked = cfg.require_depth
                self._reason = "invalid_depth"
                return self._decision(
                    self._blocked,
                    self._reason,
                    observation.nearest_m,
                    observation.valid_pixels,
                    age,
                )

            nearest = observation.nearest_m
            assert nearest is not None  # valid_pixels >= 1 guarantees this
            if nearest <= cfg.stop_distance_m:
                self._blocked = True
                self._obstacle_latched = True
            elif self._obstacle_latched:
                # Hysteresis applies only to an obstacle that actually closed
                # the gate.  Between stop and release the gate stays shut.
                if nearest >= cfg.release_distance_m:
                    self._blocked = False
                    self._obstacle_latched = False
                else:
                    self._blocked = True
            else:
                self._blocked = False
            self._reason = "obstacle" if self._blocked else "clear"
            return self._decision(
                self._blocked,
                self._reason,
                nearest,
                observation.valid_pixels,
                age,
            )

    def reset(self) -> None:
        with self._lock:
            self._observation = None
            self._blocked = False
            self._obstacle_latched = False
            self._reason = "no_depth" if self._config.require_depth else "clear"

    # ------------------------------------------------------------------ detail

    def _decision(
        self,
        blocked: bool,
        reason: str,
        nearest: float | None,
        valid_pixels: int,
        age: float | None = None,
    ) -> SafetyGateDecision:
        return SafetyGateDecision(
            blocked=blocked,
            reason=reason,
            nearest_m=nearest,
            age_s=age,
            valid_pixels=valid_pixels,
            stop_distance_m=self._config.stop_distance_m,
            release_distance_m=self._config.release_distance_m,
        )


def _depth_scale(depth: np.ndarray, config: SafetyGateConfig) -> float:
    if config.depth_scale_m > 0.0:
        return config.depth_scale_m
    if depth.dtype == np.uint16:
        return 0.001  # millimetres, the ROS/Orbbec convention
    return 1.0  # float depth is metres


def _nearest_in_roi(
    depth: np.ndarray, config: SafetyGateConfig
) -> tuple[float | None, int]:
    """Return (percentile depth in metres, valid pixel count)."""

    if not isinstance(depth, np.ndarray):
        raise ValueError("depth must be a numpy array")
    if depth.ndim != 2:
        raise ValueError(f"depth must be 2-D, got shape {depth.shape!r}")
    if depth.dtype not in (np.uint16, np.float32, np.float64):
        raise ValueError(
            f"depth dtype {depth.dtype} is not a supported depth encoding"
        )
    if depth.size == 0:
        return None, 0

    height, width = depth.shape
    top = int(height * config.roi_top_fraction)
    bottom = int(height * config.roi_bottom_fraction)
    half = config.roi_width_fraction / 2.0
    left = int(width * (0.5 - half))
    right = int(width * (0.5 + half))
    if bottom <= top or right <= left:
        return None, 0

    band = depth[top:bottom, left:right]
    values = band.astype(np.float64) * _depth_scale(depth, config)
    values = values[np.isfinite(values)]
    values = values[(values >= MIN_VALID_DEPTH_M) & (values <= MAX_VALID_DEPTH_M)]
    valid = int(values.size)
    if valid == 0:
        return None, 0
    nearest = float(np.percentile(values, config.percentile))
    return nearest, valid


def gate_config_from_runtime(config: Any) -> SafetyGateConfig:
    """Build the gate config from the node's ``RuntimeConfig``.

    Kept here rather than in ``core`` so the gate can be tested without the
    whole runtime config, and kept as an explicit mapping so a renamed field
    fails loudly instead of silently falling back to a default.
    """

    return SafetyGateConfig(
        enabled=bool(config.safety_gate_enabled),
        require_depth=bool(config.safety_gate_require_depth),
        stop_distance_m=float(config.safety_gate_stop_m),
        release_distance_m=float(config.safety_gate_release_m),
        depth_timeout_s=float(config.safety_gate_depth_timeout_s),
        depth_scale_m=float(config.safety_gate_depth_scale_m),
        roi_top_fraction=float(config.safety_gate_roi_top_fraction),
        roi_bottom_fraction=float(config.safety_gate_roi_bottom_fraction),
        roi_width_fraction=float(config.safety_gate_roi_width_fraction),
        min_valid_pixels=int(config.safety_gate_min_valid_pixels),
        percentile=float(config.safety_gate_percentile),
    ).validate()
