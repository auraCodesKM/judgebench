"""doctor: every check reports PASS/FAIL/SKIP with a fix, never crashes, never prints secrets."""

import pytest

from judgebench.cli import doctor


def test_missing_credentials_fail_with_fixes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from judgebench import config

    real = config.settings()
    fake = config.Settings(
        **{
            **real.__dict__,
            "telegram_bot_token": None,
            "github_token": None,
            "google_cloud_project": None,
            "google_token_file": str(tmp_path / "none.json"),
            "google_client_secrets_file": str(tmp_path / "none2.json"),
            "probe_url": "inprocess",
            "pubsub_topic": None,
            "llm_mode": "vertex",
        }
    )
    monkeypatch.setattr(doctor, "settings", lambda: fake)
    results = doctor.run(
        [doctor.check_google, doctor.check_telegram, doctor.check_vertex, doctor.check_probe, doctor.check_pubsub]
    )
    by = {r.name: r for r in results}
    assert by["google_oauth"].status == "FAIL" and "M0 item 4" in by["google_oauth"].fix
    assert by["telegram_bot"].status == "FAIL" and "BotFather" in by["telegram_bot"].fix
    assert by["vertex"].status == "FAIL" and "GOOGLE_CLOUD_PROJECT" in by["vertex"].fix
    assert by["probe"].status == "PASS" and by["pubsub"].status == "SKIP"


def test_offline_mode_never_calls_vertex(monkeypatch: pytest.MonkeyPatch) -> None:
    from judgebench import config

    monkeypatch.setattr(
        doctor, "settings", lambda: config.Settings(**{**config.settings().__dict__, "llm_mode": "offline"})
    )
    monkeypatch.setattr("google.genai.Client", lambda **_: pytest.fail("offline doctor built a Vertex client"))
    assert [(r.name, r.status) for r in doctor.check_vertex()] == [("vertex", "SKIP")]


def test_a_crashing_check_does_not_hide_others() -> None:
    def broken() -> list[doctor.Result]:
        raise RuntimeError("boom")

    results = doctor.run([broken, lambda: [doctor._ok("x")]])
    assert [r.status for r in results] == ["FAIL", "PASS"]
    assert "1 failing" in doctor.render(results)


@pytest.mark.db
def test_postgres_check_against_real_db(monkeypatch: pytest.MonkeyPatch, engine) -> None:  # type: ignore[no-untyped-def]
    from judgebench import config
    from tests.conftest import TEST_DB_URL

    monkeypatch.setattr(
        doctor, "settings", lambda: config.Settings(**{**config.settings().__dict__, "database_url": TEST_DB_URL})
    )
    assert {r.name: r.status for r in doctor.check_postgres()} == {
        "postgres": "PASS",
        "migrations": "PASS",
        "pgvector": "PASS",
    }


def test_cli_never_logs_request_urls(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    import httpx

    from judgebench.cli.main import main

    with pytest.raises(SystemExit):
        main(["--help"])
    with caplog.at_level(logging.DEBUG, logger="root"):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        client.get("https://api.telegram.org/bot123:SECRET/getMe")
    assert "SECRET" not in caplog.text
