"""Team codes: a valid code binds to the first team name it is used with; only that code can add
versions to that team; anything else is rejected once (Telegram + audit), never evaluated or stored."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from judgebench.api.ingest import ingest_responses
from judgebench.db import models as m
from judgebench.event_config import load_event_config
from judgebench.team_codes import ALPHABET, add_codes, new_code
from tests.conftest import make_event

Q = {"q1": "team_name", "q2": "repo_url", "q5": "team_code"}
A, B = "ABCD-2345", "WXYZ-6789"


def resp(rid: str, team: str, code: str) -> dict[str, Any]:
    def txt(v: str) -> dict[str, Any]:
        return {"textAnswers": {"answers": [{"value": v}]}}

    ans = {"q1": txt(team), "q2": txt("https://github.com/acme/demo"), "q5": txt(code)}
    return {"responseId": rid, "createTime": f"2026-11-22T10:00:0{rid[-1]}Z", "answers": ans}


@pytest.mark.db
def test_team_code_rules(session: Session) -> None:
    event = make_event(session, "codes")
    event.config = {"team_codes": [A, B]}
    created = ingest_responses(
        session,
        event,
        [
            resp("r1", "Team One", A.lower()),  # binds A to Team One (case-insensitive code)
            resp("r2", "team one", A),  # same team, same code: v2
            resp("r3", "Team One", B),  # B is valid but Team One is bound to A
            resp("r4", "Team Two", A),  # A belongs to Team One
            resp("r5", "Team Two", "NOPE-0000"),  # unknown code
            resp("r6", "Team Two", B),  # binds B to Team Two
        ],
        Q,
    )
    session.commit()
    subs = session.query(m.Submission).order_by(m.Submission.form_response_id).all()
    assert [(s.form_response_id, s.version) for s in subs] == [("r1", 1), ("r2", 2), ("r6", 1)]
    assert len(created) == 3
    assert all("team_code" not in s.form_answers for s in subs)  # the code itself is never stored
    rejected = {
        a.target: a.detail["reason"] for a in session.query(m.AuditEvent).filter_by(action="submission_rejected")
    }
    assert rejected == {
        "r3": "the team code doesn't match this team",
        "r4": "that team code belongs to another team",
        "r5": "the team code is missing or wrong",
    }
    texts = [n.text for n in session.query(m.Notification).filter_by(stage="REJECTED")]
    assert (
        "I couldn't accept a submission from \"Team Two\": the team code is missing or wrong. I didn't evaluate it."
        in texts
    )

    # polled again: nothing new, no repeated rejection messages
    assert ingest_responses(session, event, [resp("r3", "Team One", B), resp("r5", "Team Two", "x")], Q) == []
    session.flush()
    assert session.query(m.Notification).filter_by(stage="REJECTED").count() == 3


@pytest.mark.db
def test_no_codes_configured_means_no_check(session: Session) -> None:
    event = make_event(session, "open")
    assert len(ingest_responses(session, event, [resp("r1", "Any Team", "")], Q)) == 1


def test_codes_are_readable_and_appended_to_the_event_file(tmp_path: Path) -> None:
    assert all(c in ALPHABET + "-" for c in new_code()) and len(new_code()) == 9
    f = tmp_path / "e.yaml"
    src = Path("events/example.yaml").read_text()
    f.write_text(src)
    first = add_codes(f, 3, [])
    second = add_codes(f, 2, first)
    cfg = load_event_config(f)
    assert cfg.team_codes == first + second and len(set(cfg.team_codes)) == 5
    assert f.read_text().startswith(src.rstrip("\n"))  # the rest of the file, comments included, untouched


def test_update_form_adds_team_code_and_new_consent(monkeypatch: pytest.MonkeyPatch) -> None:
    from judgebench.google import workspace as w

    old_qs = [q for q in w.QUESTIONS if q.key != "team_code"]
    items = []
    for n, q in enumerate(old_qs):
        item = w._item(q)
        if q.key == "consent":
            item["questionItem"]["question"]["choiceQuestion"]["options"] = [{"value": "old consent"}]
        item["itemId"] = f"i{n}"
        item["questionItem"]["question"]["questionId"] = f"q{n}"
        items.append(item)
    qmap = {f"q{n}": q.key for n, q in enumerate(old_qs)}
    sent: list[dict[str, Any]] = []

    def fake(s: Any, method: str, url: str, **kw: Any) -> Any:
        if method == "GET":
            return {"items": items, "info": {"description": w.FORM_NOTICE}}
        sent.extend(kw["json"]["requests"])
        return {"replies": [{"createItem": {"questionId": ["new"]}} if "createItem" in r else {} for r in sent]}

    monkeypatch.setattr(w, "_call", fake)
    out = w.update_form(None, "F", qmap)  # type: ignore[arg-type]
    [upd] = [r["updateItem"] for r in sent if "updateItem" in r]
    [new] = [r["createItem"] for r in sent if "createItem" in r]
    assert upd["item"]["questionItem"]["question"]["choiceQuestion"]["options"] == [{"value": w.CONSENT}]
    assert upd["item"]["itemId"] == f"i{len(old_qs) - 1}" and "Gemini" in w.CONSENT and "90 days" in w.CONSENT
    assert new["item"]["title"] == "Team code" and new["location"]["index"] == 1
    assert new["item"]["questionItem"]["question"]["required"] is True
    assert out["new"] == "team_code" and {k: v for k, v in out.items() if k != "new"} == qmap
    sent.clear()
    items.insert(1, {**w._item(w.QUESTIONS[1]), "itemId": "ix"})
    items[1]["questionItem"]["question"]["questionId"] = "new"
    items[-1]["questionItem"]["question"]["choiceQuestion"]["options"] = [{"value": w.CONSENT}]
    assert w.update_form(None, "F", out) == out and sent == []  # idempotent  # type: ignore[arg-type]
