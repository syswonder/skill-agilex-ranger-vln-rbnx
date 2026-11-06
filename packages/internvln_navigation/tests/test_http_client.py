from __future__ import annotations

import json
import unittest

from internvln_navigation.core import (
    EncodedFrame,
    ModelUnavailableError,
    ProtocolError,
    RuntimeConfig,
    VelocityCommand,
)
from internvln_navigation.http_client import (
    IMAGE_FIELD,
    InternVLAClient,
    build_multipart_body,
)


JPEG = b"\xff\xd8\xff\xe0not-a-real-jpeg-but-it-has-the-marker\xff\xd9"


def frame(sequence: int = 1) -> EncodedFrame:
    return EncodedFrame(
        sequence=sequence,
        jpeg=JPEG,
        source_timestamp=1.0,
        received_monotonic=1.0,
    )


def json_part(body: bytes) -> dict:
    """Pull the ``json`` form field back out of a multipart body."""

    head, _, rest = body.partition(b"\r\n\r\n")
    assert b'name="json"' in head, head
    value, _, _tail = rest.partition(b"\r\n--")
    return json.loads(value.decode("utf-8"))


class FakeTransport:
    """Scripted transport that records every request it is asked to make."""

    def __init__(self, responses) -> None:
        # responses: list of (status, body_bytes)
        self.responses = list(responses)
        self.calls: list[tuple[str, str, bytes | None, dict]] = []
        self.closed = False

    def __call__(self, method, url, body, headers, timeout_s):
        self.calls.append((method, url, body, dict(headers)))
        if not self.responses:
            raise AssertionError(f"unexpected request {method} {url}")
        status, payload = self.responses.pop(0)[:2]
        return status, payload

    def close(self) -> None:
        self.closed = True

    @property
    def paths(self) -> list[str]:
        return [call[1] for call in self.calls]

    @property
    def eval_bodies(self) -> list[bytes]:
        bodies = [
            call[2] for call in self.calls if call[1].endswith("/eval_dual")
        ]
        assert all(body is not None for body in bodies)
        return bodies  # type: ignore[return-value]


def json_response(payload) -> tuple[int, bytes]:
    return 200, json.dumps(payload).encode("utf-8")


class MultipartBodyTests(unittest.TestCase):
    def test_the_json_metadata_is_a_form_field_not_the_body(self) -> None:
        body, boundary = build_multipart_body(
            {"reset": True, "instruction": "walk to the door"}, JPEG
        )
        self.assertIn(f"--{boundary}\r\n".encode(), body)
        self.assertIn(b'name="json"\r\n', body)
        self.assertEqual(
            json_part(body), {"reset": True, "instruction": "walk to the door"}
        )
        # The one documented image field name, and the reference client's
        # filename, not the "rgb.jpg" an older revision of this package sent.
        self.assertIn(b'name="image"; filename="rgb"', body)
        self.assertNotIn(b"rgb.jpg", body)
        self.assertIn(JPEG, body)
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))

    def test_the_default_image_field_is_the_documented_one(self) -> None:
        self.assertEqual(IMAGE_FIELD, "image")

    def test_non_jpeg_payloads_are_rejected_before_they_leave_the_robot(self) -> None:
        with self.assertRaises(ValueError):
            build_multipart_body({}, b"not a jpeg")
        with self.assertRaises(ValueError):
            build_multipart_body({}, b"")


class EvaluateTests(unittest.TestCase):
    def _client(self, responses, **overrides):
        # These tests are about the request path, so they opt out of the
        # instruction gate and start with a task in force, exactly as a client
        # does once an operator has handed one over.  `require_instruction`
        # itself is covered by AwaitingInstructionTests below.
        overrides.setdefault("require_instruction", False)
        transport = FakeTransport(responses)
        client = InternVLAClient(
            RuntimeConfig.from_mapping(overrides), transport=transport
        )
        return client, transport

    def test_the_first_frame_resets_the_history_and_the_next_one_does_not(self) -> None:
        client, transport = self._client(
            [
                json_response({"discrete_action": [1]}),
                json_response({"discrete_action": [2]}),
            ]
        )
        first = client.evaluate(frame(1))
        second = client.evaluate(frame(2))

        self.assertGreater(first.command.vx, 0.0)
        self.assertGreater(second.command.wz, 0.0)
        # Exactly one request: the server has no task registry to call first.
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            [json_part(body) for body in transport.eval_bodies],
            [
                {"reset": True, "instruction": "Go to the chair and stop."},
                {"reset": False, "instruction": "Go to the chair and stop."},
            ],
        )

    def test_every_frame_carries_the_instruction(self) -> None:
        # The server is stateless: drop the instruction and it either falls back
        # to its own built-in default or fails the request outright.
        client, transport = self._client(
            [
                json_response({"discrete_action": [0]}),
                json_response({"discrete_action": [0]}),
                json_response({"discrete_action": [0]}),
            ]
        )
        for index in range(3):
            client.evaluate(frame(index + 1))
        for body in transport.eval_bodies:
            self.assertEqual(
                json_part(body)["instruction"], "Go to the chair and stop."
            )

    def test_a_new_instruction_resets_the_history_again(self) -> None:
        client, transport = self._client(
            [
                json_response({"discrete_action": [1]}),
                json_response({"discrete_action": [0]}),
            ]
        )
        client.evaluate(frame(1))
        client.set_instruction("go to the door")
        client.evaluate(frame(2))

        self.assertEqual(
            [json_part(body) for body in transport.eval_bodies],
            [
                {"reset": True, "instruction": "Go to the chair and stop."},
                {"reset": True, "instruction": "go to the door"},
            ],
        )
        self.assertEqual(client.episode, 2)

    def test_a_plan_that_lands_after_a_new_instruction_is_discarded(self) -> None:
        """The answer to a task nobody asked for must never reach the chassis."""

        class MidFlight(FakeTransport):
            def __call__(self, method, url, body, headers, timeout_s):
                result = super().__call__(method, url, body, headers, timeout_s)
                # The operator changes their mind while the answer is in flight.
                client.set_instruction("something else")
                return result

        transport = MidFlight([json_response({"discrete_action": [1]})])
        client = InternVLAClient(
            RuntimeConfig.from_mapping({"require_instruction": False}),
            transport=transport,
        )
        plan = client.evaluate(frame(1))

        self.assertEqual(plan.velocity_source, "superseded")
        self.assertEqual((plan.command.vx, plan.command.wz), (0.0, 0.0))

    def test_set_instruction_never_blocks_on_the_network(self) -> None:
        client, transport = self._client([])
        client.set_instruction("walk to the door")
        # Nothing may have been sent: the blocking call belongs to the worker.
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.instruction, "walk to the door")

    def test_setting_the_same_instruction_again_does_not_reset_the_episode(self) -> None:
        client, transport = self._client(
            [
                json_response({"discrete_action": [0]}),
                json_response({"discrete_action": [0]}),
            ]
        )
        client.evaluate(frame(1))
        client.set_instruction("Go to the chair and stop.")  # the default
        client.evaluate(frame(2))
        self.assertEqual(client.episode, 1)
        self.assertEqual(json_part(transport.eval_bodies[1])["reset"], False)

    def test_the_history_is_flushed_every_n_frames_of_one_instruction(self) -> None:
        # The server samples its 8 context images evenly from the whole episode,
        # so without a flush the frames around the robot get stale as one
        # instruction runs on.  With a window of 3: reset on frames 1, 4, 7.
        client, transport = self._client(
            [json_response({"discrete_action": [1]}) for _ in range(7)],
            history_reset_frames=3,
        )
        for index in range(7):
            client.evaluate(frame(index + 1))

        self.assertEqual(
            [json_part(body)["reset"] for body in transport.eval_bodies],
            [True, False, False, True, False, False, True],
        )
        self.assertEqual(client.memory_flushes, 2)

    def test_a_flush_keeps_the_instruction_and_does_not_start_an_episode(self) -> None:
        client, transport = self._client(
            [json_response({"discrete_action": [1]}) for _ in range(3)],
            history_reset_frames=2,
        )
        client.evaluate(frame(1))
        client.evaluate(frame(2))  # two frames of history now
        client.evaluate(frame(3))  # so this one flushes first

        self.assertEqual(client.episode, 1)
        self.assertEqual(
            [json_part(body)["instruction"] for body in transport.eval_bodies],
            ["Go to the chair and stop."] * 3,
        )
        self.assertEqual(client.memory_flushes, 1)
        # The status has to say so, or an operator watching the stream sees a
        # reset and concludes the instruction was dropped.
        status = client.snapshot()
        self.assertEqual(status["memory_flushes"], 1)
        self.assertEqual(status["episode"], 1)
        self.assertIn("memory flushed", status["last_note"])

    def test_a_flush_window_of_zero_disables_the_flush(self) -> None:
        client, transport = self._client(
            [json_response({"discrete_action": [1]}) for _ in range(6)],
            history_reset_frames=0,
        )
        for index in range(6):
            client.evaluate(frame(index + 1))

        self.assertEqual(
            [json_part(body)["reset"] for body in transport.eval_bodies],
            [True] + [False] * 5,
        )
        self.assertEqual(client.memory_flushes, 0)

    def test_the_memory_is_kept_by_default(self) -> None:
        # 0 = never flush mid-episode, which is the official client's behaviour
        # and the model's training distribution: the server samples 8 frames
        # evenly over the whole episode, from frame 0.  Flushing every 8 frames
        # (the 2026-10-29 default) wiped the model's only record of where it
        # first saw the target, so it could not lock on or notice it had passed.
        self.assertEqual(RuntimeConfig().history_reset_frames, 0)

    def test_consecutive_failures_escalate_to_model_unavailable(self) -> None:
        client, _ = self._client(
            [(503, b"down"), (503, b"down"), (503, b"down")],
            fault_after_failures=3,
            failure_retries=0,  # one frame, one request: count frames, not tries
        )
        with self.assertRaises(ProtocolError):
            client.evaluate(frame(1))
        with self.assertRaises(ProtocolError):
            client.evaluate(frame(2))
        with self.assertRaises(ModelUnavailableError):
            client.evaluate(frame(3))

    def test_a_success_resets_the_failure_streak(self) -> None:
        client, _ = self._client(
            [
                (503, b"down"),
                json_response({"discrete_action": [0]}),
            ],
            fault_after_failures=3,
            failure_retries=0,  # one frame, one request
        )
        with self.assertRaises(ProtocolError):
            client.evaluate(frame(1))
        client.evaluate(frame(2))
        self.assertEqual(client.consecutive_failures, 0)

    def test_a_transient_error_is_retried_before_the_frame_is_lost(self) -> None:
        """One 500 is a hiccup, not the end of the episode.

        Giving up on the first 500 costs the frame *and* re-arms
        ``reset: true``, so the server's eight-frame history -- the thing the
        model plans against -- is thrown away over a hiccup.  A robot whose
        history keeps resetting has nothing to move on.
        """

        client, transport = self._client(
            [
                json_response({"discrete_action": [1]}),
                (500, b"transient"),
                json_response({"discrete_action": [1]}),
            ]
        )
        client.evaluate(frame(1))
        second = client.evaluate(frame(2))

        self.assertGreater(second.command.vx, 0.0)
        self.assertEqual(client.consecutive_failures, 0)
        self.assertEqual(client.snapshot()["retries"], 1)
        self.assertEqual(
            [json_part(body)["reset"] for body in transport.eval_bodies],
            [True, False, False],
        )
        self.assertIn("recovered on retry 1", client.snapshot()["last_note"])

    def test_a_frame_that_fails_every_retry_still_resets_the_history(self) -> None:
        """After real doubt about the server's history, clear it."""

        client, transport = self._client(
            [
                json_response({"discrete_action": [1]}),
                (500, b"first"), (500, b"second"),
                json_response({"discrete_action": [1]}),
            ],
            failure_retries=1,
        )
        client.evaluate(frame(1))
        with self.assertRaises(ProtocolError):
            client.evaluate(frame(2))
        self.assertEqual(client.consecutive_failures, 1)
        client.evaluate(frame(3))
        self.assertEqual(
            [json_part(body)["reset"] for body in transport.eval_bodies],
            [True, False, False, True],
        )

    def test_retries_can_be_turned_off(self) -> None:
        client, transport = self._client(
            [(500, b"transient"), json_response({"discrete_action": [1]})],
            failure_retries=0,
        )
        with self.assertRaises(ProtocolError):
            client.evaluate(frame(1))
        self.assertEqual(len(transport.eval_bodies), 1)
        # The unspent answer is left for the next frame, which succeeds.
        client.evaluate(frame(2))
        self.assertEqual(client.snapshot()["retries"], 0)

    def test_a_malformed_answer_costs_one_plan_and_is_counted(self) -> None:
        client, _ = self._client(
            [json_response({"pixel_goal": [1, 2]})], failure_retries=0
        )
        with self.assertRaises(ProtocolError):
            client.evaluate(frame(1))
        self.assertEqual(client.consecutive_failures, 1)

    def test_an_unreachable_server_is_reported_not_raised_raw(self) -> None:
        client = InternVLAClient(
            RuntimeConfig.from_mapping({"require_instruction": False}),
            transport=lambda *args, **kwargs: (_ for _ in ()).throw(
                ConnectionRefusedError("synthetic refusal")
            ),
        )
        with self.assertRaises(ProtocolError) as caught:
            client.evaluate(frame(1))
        self.assertIn("synthetic refusal", str(caught.exception))

    def test_set_executed_command_is_recorded_but_not_sent(self) -> None:
        client, transport = self._client([])
        client.set_executed_command(VelocityCommand(0.1, 0.2))
        self.assertEqual(transport.calls, [])
        snapshot = client.snapshot()
        self.assertAlmostEqual(snapshot["executed_vx"], 0.1)
        self.assertAlmostEqual(snapshot["executed_wz"], 0.2)


class TeardownTests(unittest.TestCase):
    def test_close_closes_the_socket_and_tells_the_server_nothing(self) -> None:
        # There is no task to report finished: the server holds no per-client
        # session, so closing must not invent a route to say so on.
        transport = FakeTransport([json_response({"discrete_action": [0]})])
        client = InternVLAClient(
            RuntimeConfig.from_mapping({"require_instruction": False}),
            transport=transport,
        )
        client.evaluate(frame(1))
        sent = len(transport.calls)
        client.close()

        self.assertTrue(transport.closed)
        self.assertEqual(len(transport.calls), sent)
        self.assertEqual(client.snapshot()["state"], "done")

    def test_close_is_safe_without_an_episode(self) -> None:
        transport = FakeTransport([])
        client = InternVLAClient(RuntimeConfig.from_mapping({}), transport=transport)
        client.close()
        self.assertTrue(transport.closed)


class AwaitingInstructionTests(unittest.TestCase):
    """The gate: no operator instruction, no request, no motion."""

    def _idle_client(self, responses=()):
        transport = FakeTransport(list(responses))
        client = InternVLAClient(RuntimeConfig.from_mapping({}), transport=transport)
        return client, transport

    def test_an_unasked_stack_sends_nothing_at_all(self) -> None:
        client, transport = self._idle_client()

        for sequence in range(1, 6):
            plan = client.evaluate(frame(sequence))

        # The whole point: not one byte leaves the robot, however many frames
        # the camera produces.
        self.assertEqual(transport.calls, [])
        self.assertEqual(client.request_index, 0)
        self.assertEqual(client.episode, 0)
        self.assertFalse(client.armed)
        self.assertEqual(plan.velocity_source, "awaiting_instruction")
        self.assertEqual((plan.command.vx, plan.command.wz), (0.0, 0.0))
        self.assertIn("awaiting an operator instruction", client.last_note)

    def test_the_configured_instruction_does_not_arm_the_stack(self) -> None:
        # It is a reference text, not a task: booting must not spend a request
        # on it, or the default instruction becomes an unasked-for task again.
        client, transport = self._idle_client()
        client.evaluate(frame(1))
        self.assertEqual(client.instruction, "")
        self.assertEqual(transport.calls, [])

    def test_an_instruction_starts_the_episode_on_the_next_frame(self) -> None:
        client, transport = self._idle_client([json_response({"discrete_action": [1]})])
        client.evaluate(frame(1))
        self.assertEqual(transport.calls, [])

        client.set_instruction("go to the chair and stop")
        plan = client.evaluate(frame(2))

        self.assertTrue(client.armed)
        self.assertGreater(plan.command.vx, 0.0)
        self.assertEqual(len(transport.calls), 1)
        # First request of the episode carries reset: true, as always.
        self.assertEqual(
            json_part(transport.eval_bodies[0]),
            {"reset": True, "instruction": "go to the chair and stop"},
        )

    def test_clearing_the_instruction_stops_the_requests_again(self) -> None:
        client, transport = self._idle_client(
            [
                json_response({"discrete_action": [1]}),
                json_response({"discrete_action": [1]}),
            ]
        )
        client.set_instruction("go to the chair and stop")
        client.evaluate(frame(1))
        self.assertEqual(len(transport.calls), 1)

        client.clear_instruction()
        plan = client.evaluate(frame(2))

        self.assertFalse(client.armed)
        # Still one call: the stop retired the task, so frame 2 asked nothing.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual((plan.command.vx, plan.command.wz), (0.0, 0.0))
        self.assertEqual(plan.velocity_source, "awaiting_instruction")
        self.assertEqual(client.instruction, "")

    def test_a_plan_in_flight_when_the_task_is_cleared_is_discarded(self) -> None:
        class ClearedMidFlight(FakeTransport):
            def __call__(self, method, url, body, headers, timeout_s):
                result = super().__call__(method, url, body, headers, timeout_s)
                client.clear_instruction()
                return result

        transport = ClearedMidFlight([json_response({"discrete_action": [1]})])
        client = InternVLAClient(RuntimeConfig.from_mapping({}), transport=transport)
        client.set_instruction("go to the chair and stop")
        plan = client.evaluate(frame(1))

        self.assertEqual(plan.velocity_source, "superseded")
        self.assertEqual((plan.command.vx, plan.command.wz), (0.0, 0.0))

    def test_the_gate_can_be_switched_off_for_a_bench_start(self) -> None:
        # require_instruction: false restores the old behaviour, so a bench
        # session can still boot straight into the configured instruction.
        transport = FakeTransport([json_response({"discrete_action": [1]})])
        client = InternVLAClient(
            RuntimeConfig.from_mapping({"require_instruction": False}),
            transport=transport,
        )
        plan = client.evaluate(frame(1))

        self.assertTrue(client.armed)
        self.assertGreater(plan.command.vx, 0.0)
        self.assertEqual(client.instruction, "Go to the chair and stop.")

    def test_the_snapshot_reports_both_sides_of_the_gate(self) -> None:
        client, _ = self._idle_client()
        snapshot = client.snapshot()
        self.assertIs(snapshot["armed"], False)
        self.assertIs(snapshot["require_instruction"], True)
        client.set_instruction("go to the chair and stop")
        self.assertIs(client.snapshot()["armed"], True)

    def test_require_instruction_must_be_a_bool(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeConfig.from_mapping({"require_instruction": "yes"})


if __name__ == "__main__":
    unittest.main()