"""Google Forms / Sheets / Drive over REST as the operator's own account (§6.2, §13.4).

Only the API component holds the refresh token. Scopes are the narrowest that work:
drive.file (only files this app created; also covers Sheets and sharing) plus Forms.
Sheet writes use valueInputOption=RAW so participant text like "=IMPORTXML(...)" is stored as
text, never evaluated as a formula.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/forms.body",
    "https://www.googleapis.com/auth/forms.responses.readonly",
]
FORMS = "https://forms.googleapis.com/v1/forms"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE = "https://www.googleapis.com/drive/v3/files"
TIMEOUT = 30


# --- auth ------------------------------------------------------------------------------


def run_oauth_flow(client_secrets: Path, token_file: Path) -> None:
    """One-time, interactive: opens a browser, stores the refresh token locally (0600)."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES).run_local_server(port=0)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.touch(mode=0o600, exist_ok=True)
    token_file.write_text(creds.to_json())


def session(token_file: Path) -> AuthorizedSession:
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if not creds.valid:
        creds.refresh(Request())
    return AuthorizedSession(creds)


def _call(s: AuthorizedSession, method: str, url: str, **kw: Any) -> Any:
    resp = s.request(method, url, timeout=TIMEOUT, **kw)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


# --- Forms -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    key: str
    title: str
    required: bool = False
    paragraph: bool = False
    choices: tuple[str, ...] = ()
    help: str = ""


CATEGORIES = ("Traditional ML", "LLM / RAG", "Agentic AI", "Computer vision / multimodal", "AI infrastructure")
CONSENT = (
    "I consent to automated analysis of our submitted artefacts, a small number of live "
    "requests to our deployed app, and anonymised use in benchmark results."
)

QUESTIONS: tuple[Question, ...] = (
    Question("team_name", "Team name", required=True),
    Question("team_members", "Team members", required=True, paragraph=True),
    Question("project_title", "Project title", required=True),
    Question("problem_statement", "Problem statement", required=True, paragraph=True),
    Question("categories", "AI project category", required=True, choices=CATEGORIES),
    Question(
        "repo_url",
        "GitHub repository URL",
        required=True,
        help="Public repository, e.g. https://github.com/team/project",
    ),
    Question("setup_instructions", "README or setup instructions", paragraph=True),
    Question("demo_url", "Demo video URL", help="YouTube; unlisted is fine"),
    Question("deck_url", "Presentation / deck URL"),
    Question("deployed_url", "Deployed application URL (if any)", help="https:// only"),
    Question("tech_claims", "Languages, frameworks, models and tooling", paragraph=True),
    Question("dataset_eval", "Dataset and evaluation information", paragraph=True),
    Question(
        "special_requirements",
        "Special execution requirements",
        help="GPU, API credentials, hardware, paid services",
        paragraph=True,
    ),
    Question(
        "sample_request",
        "Sample request for the deployed app (optional)",
        paragraph=True,
        help="One example request and what a correct response looks like",
    ),
    Question("consent", "Consent and submission declaration", required=True, choices=(CONSENT,)),
)
FORM_NOTICE = (
    "Do not submit private credentials, API keys or confidential datasets. "
    "Submitted code is read and analysed, never executed."
)


def _item(q: Question) -> dict[str, Any]:
    question: dict[str, Any] = {"required": q.required}
    if q.choices:
        question["choiceQuestion"] = {"type": "CHECKBOX", "options": [{"value": c} for c in q.choices]}
    else:
        question["textQuestion"] = {"paragraph": q.paragraph}
    return {"title": q.title, "description": q.help, "questionItem": {"question": question}}


def create_form(s: AuthorizedSession, title: str) -> dict[str, Any]:
    """Returns {"form_id", "responder_uri", "questions": {question_id: key}, "published"}."""
    form = _call(s, "POST", FORMS, json={"info": {"title": title, "documentTitle": title}})
    form_id = form["formId"]
    requests: list[dict[str, Any]] = [
        {"updateFormInfo": {"info": {"description": FORM_NOTICE}, "updateMask": "description"}}
    ]
    requests += [{"createItem": {"item": _item(q), "location": {"index": i}}} for i, q in enumerate(QUESTIONS)]
    replies = _call(s, "POST", f"{FORMS}/{form_id}:batchUpdate", json={"requests": requests})["replies"]
    qids = {r["createItem"]["questionId"][0]: q.key for r, q in zip(replies[1:], QUESTIONS, strict=True)}
    published = True
    try:  # API-created forms start unpublished
        _call(
            s,
            "POST",
            f"{FORMS}/{form_id}:setPublishSettings",
            json={
                "publishSettings": {"publishState": {"isPublished": True, "isAcceptingResponses": True}},
                "updateMask": "publishState",
            },
        )
    except Exception:  # noqa: BLE001 - surfaced to the operator as a manual step
        published = False
    return {
        "form_id": form_id,
        "responder_uri": form["responderUri"],
        "questions": qids,
        "published": published,
        "edit_url": f"https://docs.google.com/forms/d/{form_id}/edit",
    }


def list_responses(s: AuthorizedSession, form_id: str, since: str | None) -> list[dict[str, Any]]:
    params: dict[str, str] = {"pageSize": "500"}
    if since:
        params["filter"] = f"timestamp >= {since}"
    out: list[dict[str, Any]] = []
    while True:
        page = _call(s, "GET", f"{FORMS}/{form_id}/responses", params=params)
        out += page.get("responses", [])
        if not (token := page.get("nextPageToken")):
            return out
        params["pageToken"] = token


# --- Sheets / Drive -------------------------------------------------------------------

KEY = "Row key (system)"  # stable row identity; lets judge edits survive re-sorting and refreshes
TABS: dict[str, list[str]] = {
    "Leaderboard": [
        "Team",
        "Version",
        "Commit",
        "Status",
        "Detected categories",
        "Score",
        "Confidence",
        "Updated (UTC)",
        KEY,
    ],
    "Evaluations": [
        "Team",
        "Criterion",
        "Score",
        "Max",
        "Rationale",
        "Evidence IDs",
        "Confidence",
        "Score before critic",
        "Critic note",
        KEY,
    ],
    "Evidence": ["Team", "Evidence ID", "Claim / finding", "Observation", "Source", "Status", "Confidence", KEY],
    "Judge Reviews": ["Team", "Judge", "Criterion", "Human score", "Comment", "Override reason"],
    "Suggested Questions": ["Team", "Question", "From", KEY],
    "Audit Log": ["Time (UTC)", "Team", "Action", "Detail", KEY],
}
JUDGE_EDITABLE = {"Judge Reviews"}


def create_judging_workbook(s: AuthorizedSession, title: str) -> dict[str, Any]:
    book = _call(
        s,
        "POST",
        SHEETS,
        json={
            "properties": {"title": title},
            "sheets": [{"properties": {"title": t, "gridProperties": {"frozenRowCount": 1}}} for t in TABS],
        },
    )
    sid = book["spreadsheetId"]
    tab_ids = {sh["properties"]["title"]: sh["properties"]["sheetId"] for sh in book["sheets"]}
    _call(
        s,
        "POST",
        f"{SHEETS}/{sid}/values:batchUpdate",
        json={
            "valueInputOption": "RAW",
            "data": [{"range": f"'{t}'!A1", "values": [hdr]} for t, hdr in TABS.items()],
        },
    )
    # Protection is a guard against accidental edits, not a security boundary (§6.3).
    _call(
        s,
        "POST",
        f"{SHEETS}/{sid}:batchUpdate",
        json={
            "requests": [
                {
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {"sheetId": tab_ids[t]},
                            "warningOnly": True,
                            "description": "System-managed: regenerated automatically",
                        }
                    }
                }
                for t in TABS
                if t not in JUDGE_EDITABLE
            ]
        },
    )
    return {"spreadsheet_id": sid, "url": book["spreadsheetUrl"], "tabs": tab_ids}


def share_with(s: AuthorizedSession, file_id: str, email: str) -> None:
    _call(
        s,
        "POST",
        f"{DRIVE}/{file_id}/permissions",
        params={"sendNotificationEmail": "true"},
        json={"role": "writer", "type": "user", "emailAddress": email},
    )


def read_tab(s: AuthorizedSession, spreadsheet_id: str, tab: str) -> list[list[Any]]:
    """Unformatted values, so numbers come back as numbers and compare cleanly."""
    data = _call(
        s, "GET", f"{SHEETS}/{spreadsheet_id}/values/'{tab}'!A1:Z", params={"valueRenderOption": "UNFORMATTED_VALUE"}
    )
    return list(data.get("values", []))


def write_tab(s: AuthorizedSession, spreadsheet_id: str, tab: str, rows: Sequence[Sequence[str | int | float]]) -> None:
    """Rewrite the header (so new columns reach existing workbooks) and replace everything under it.
    RAW: values are never parsed as formulas."""
    _call(
        s,
        "PUT",
        f"{SHEETS}/{spreadsheet_id}/values/'{tab}'!A1",
        params={"valueInputOption": "RAW"},
        json={"values": [TABS[tab]]},
    )
    _call(s, "POST", f"{SHEETS}/{spreadsheet_id}/values/'{tab}'!A2:Z:clear", json={})
    if rows:
        _call(
            s,
            "PUT",
            f"{SHEETS}/{spreadsheet_id}/values/'{tab}'!A2",
            params={"valueInputOption": "RAW"},
            json={"values": [list(r) for r in rows]},
        )
