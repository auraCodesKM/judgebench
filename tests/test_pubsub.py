"""Pub/Sub wake-ups between ingestion and the worker; the in-process probe for local dev."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from judgebench.pubsub import message, parse_push, rest_publisher


def test_message_roundtrip() -> None:
    sid = uuid.uuid4()
    assert parse_push({"message": message(sid), "subscription": "projects/p/subscriptions/s"}) == sid


@pytest.mark.parametrize(
    "envelope",
    [{}, {"message": {}}, {"message": {"data": "!!notb64"}}, {"message": {"data": "eyJ4IjogMX0="}}, None, "x"],
)
def test_malformed_envelopes_are_dropped(envelope: Any) -> None:
    assert parse_push(envelope) is None


def test_push_endpoint_advances_work_and_acks(monkeypatch: pytest.MonkeyPatch) -> None:
    import judgebench.worker.app as worker

    calls: list[int] = []
    monkeypatch.setattr(worker, "advance", lambda limit=12: calls.append(limit) or 3)
    client = TestClient(worker.app)
    assert client.post("/internal/pubsub", json={"message": message(uuid.uuid4())}).status_code == 204
    assert client.post("/internal/pubsub", json={"message": {"data": "bad"}}).status_code == 204  # acked, dropped
    assert calls == [12]


def test_publish_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    import google.auth

    def boom(**_: Any) -> Any:
        raise RuntimeError("no ADC")

    monkeypatch.setattr(google.auth, "default", boom)
    rest_publisher("projects/p/topics/t")([uuid.uuid4()])  # must not raise: rows are already committed


def test_in_process_probe_uses_the_guard() -> None:
    from judgebench.worker.live_check import probe_client

    [out] = probe_client("inprocess").probe([{"method": "GET", "url": "https://169.254.169.254/computeMetadata/v1/"}])
    assert out["blocked"] and out["status"] is None
