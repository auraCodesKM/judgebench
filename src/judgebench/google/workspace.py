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
    "requests to our deployed app, and anonymised use in benchmark results. "
    "Submissions may later be re-scored by Google AI models (Gemini) under the organisers' account; "
    "your code is never executed. Deleted 90 days after the event, except anonymised results."
)

QUESTIONS: tuple[Question, ...] = (
    Question("team_name", "Team name", required=True),
    Question("team_code", "Team code", required=True, help="The code the organisers gave your team, e.g. ABCD-2345"),
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


def update_form(s: AuthorizedSession, form_id: str, questions: dict[str, str]) -> dict[str, str]:
    """Bring a live Form up to QUESTIONS without recreating it (responses and links stay valid):
    missing questions are inserted in order, changed choice texts (the consent line) are updated.
    Returns the new {question_id: key} map."""
    form = _call(s, "GET", f"{FORMS}/{form_id}")
    items = form.get("items", [])
    key_at = {
        questions[it["questionItem"]["question"]["questionId"]]: (n, it)
        for n, it in enumerate(items)
        if it.get("questionItem", {}).get("question", {}).get("questionId") in questions
    }
    requests: list[dict[str, Any]] = []
    for q in QUESTIONS:  # 1) updates, at the items' current positions
        if q.key not in key_at or not q.choices:
            continue
        n, it = key_at[q.key]
        have = [o.get("value") for o in it["questionItem"]["question"].get("choiceQuestion", {}).get("options", [])]
        if have != list(q.choices):
            item = _item(q) | {"itemId": it["itemId"]}
            item["questionItem"]["question"]["questionId"] = it["questionItem"]["question"]["questionId"]
            requests.append(
                {
                    "updateItem": {
                        "item": item,
                        "location": {"index": n},
                        "updateMask": "title,description,questionItem.question.choiceQuestion",
                    }
                }
            )
    inserted: list[tuple[int, str]] = []
    for i, q in enumerate(QUESTIONS):  # 2) inserts, ascending, at their position in QUESTIONS order
        if q.key not in key_at:
            requests.append(
                {"createItem": {"item": _item(q), "location": {"index": min(i, len(items) + len(inserted))}}}
            )
            inserted.append((len(requests) - 1, q.key))
    if form.get("info", {}).get("description") != FORM_NOTICE:
        requests.append({"updateFormInfo": {"info": {"description": FORM_NOTICE}, "updateMask": "description"}})
    if not requests:
        return dict(questions)
    replies = _call(s, "POST", f"{FORMS}/{form_id}:batchUpdate", json={"requests": requests})["replies"]
    out = dict(questions)
    for idx, key in inserted:
        out[replies[idx]["createItem"]["questionId"][0]] = key
    return out


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

KEY = "Row key (system)"  # stable row identity (hidden column); lets judge edits survive re-sorting
TABS: dict[str, list[str]] = {
    # Judge-facing, in this order. Five minutes per team: decide from the Leaderboard, drill into Team Detail.
    "Leaderboard": [
        "Rank",
        "Team",
        "Project",
        "Score /100",
        "Confidence",
        "Flags",
        "Biggest concern",
        "Question to ask",
        "Repo",
        "Demo",
        "Live app",
        "Evaluated by",
        KEY,
    ],
    "Team Detail": ["Team", "Criterion", "Score / Max", "Reason", "Evidence", "Critic note", "Question to ask", KEY],
    "Evidence": ["Team", "Finding", "What we saw", "Status", "Source", KEY],
    "Judge Reviews": ["Team", "Judge", "Criterion", "Score", "Comment"],
    # Hidden, protected.
    "System": ["Team", "Version", "Commit", "Status", "Detected categories", "Updated (UTC)", KEY],
    "Audit Log": ["Time (UTC)", "Team", "Action", "Detail", KEY],
    "Lists": ["Teams", "Criteria", "Max", "Judges", KEY],  # dropdown sources for Judge Reviews
}
JUDGE_EDITABLE = {"Judge Reviews"}
HIDDEN = {"System", "Audit Log", "Lists"}
LEGACY_TABS = ("Evaluations", "Suggested Questions")  # pre-2026-11-18 layout: hidden, never deleted


@dataclass(frozen=True)
class Link:
    """A cell whose text carries one or more links (Sheets rich text, never a formula)."""

    text: str
    spans: tuple[tuple[int, int, str], ...] = ()  # (start, end, https uri)

    def __str__(self) -> str:
        return self.text


def safe_uri(uri: str | None) -> str | None:
    """Only plain https links reach the Sheet (no javascript:, data:, or http to odd hosts)."""
    return uri if uri and uri.startswith("https://") and " " not in uri and len(uri) < 2000 else None


def link(text: str, uri: str | None) -> Link | str:
    return Link(text, ((0, len(text), u),)) if (u := safe_uri(uri)) and text else text


def links(items: Sequence[tuple[str, str | None]], sep: str = "\n") -> Link | str:
    text, spans = "", []
    for label, uri in items:
        if text:
            text += sep
        if u := safe_uri(uri):
            spans.append((len(text), len(text) + len(label), u))
        text += label
    return Link(text, tuple(spans)) if spans else text


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
    _call(
        s,
        "POST",
        f"{SHEETS}/{sid}/values:batchUpdate",
        json={
            "valueInputOption": "RAW",
            "data": [{"range": f"'{t}'!A1", "values": [hdr]} for t, hdr in TABS.items()],
        },
    )
    from judgebench.google.layout import apply_layout

    apply_layout(s, sid)
    return {"spreadsheet_id": sid, "url": book["spreadsheetUrl"]}


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


def padded(header: list[str]) -> list[str]:
    """Header row to column Z: blanks out cells left over from a wider, older header."""
    return [*header, *[""] * (26 - len(header))]


def set_title(s: AuthorizedSession, spreadsheet_id: str, title: str) -> None:
    _call(
        s,
        "POST",
        f"{SHEETS}/{spreadsheet_id}:batchUpdate",
        json={"requests": [{"updateSpreadsheetProperties": {"properties": {"title": title[:250]}, "fields": "title"}}]},
    )


def sheet_ids(s: AuthorizedSession, spreadsheet_id: str) -> dict[str, int]:
    meta = _call(s, "GET", f"{SHEETS}/{spreadsheet_id}", params={"fields": "sheets(properties(sheetId,title))"})
    return {sh["properties"]["title"]: sh["properties"]["sheetId"] for sh in meta.get("sheets", [])}


def write_tab(s: AuthorizedSession, spreadsheet_id: str, tab: str, rows: Sequence[Sequence[Any]]) -> None:
    """Rewrite the header (so new columns reach existing workbooks) and replace everything under it.
    RAW: values are never parsed as formulas. Link cells then get rich-text links on top of the same text."""
    _call(
        s,
        "PUT",
        f"{SHEETS}/{spreadsheet_id}/values/'{tab}'!A1",
        params={"valueInputOption": "RAW"},
        json={"values": [padded(TABS[tab])]},
    )
    _call(s, "POST", f"{SHEETS}/{spreadsheet_id}/values/'{tab}'!A2:Z:clear", json={})
    if not rows:
        return
    _call(
        s,
        "PUT",
        f"{SHEETS}/{spreadsheet_id}/values/'{tab}'!A2",
        params={"valueInputOption": "RAW"},
        json={"values": [[str(v) if isinstance(v, Link) else v for v in r] for r in rows]},
    )
    requests = link_requests(sheet_ids(s, spreadsheet_id)[tab], rows)
    if requests:
        _call(s, "POST", f"{SHEETS}/{spreadsheet_id}:batchUpdate", json={"requests": requests})


def link_requests(sheet_id: int, rows: Sequence[Sequence[Any]]) -> list[dict[str, Any]]:
    out = []
    for r, row in enumerate(rows, start=1):
        for c, v in enumerate(row):
            if not isinstance(v, Link) or not v.spans:
                continue
            runs: list[dict[str, Any]] = []
            for start, end, uri in v.spans:
                runs.append({"startIndex": start, "format": {"link": {"uri": uri}}})
                if end < len(v.text):
                    runs.append({"startIndex": end, "format": {}})
            out.append(
                {
                    "updateCells": {
                        "start": {"sheetId": sheet_id, "rowIndex": r, "columnIndex": c},
                        "rows": [{"values": [{"userEnteredValue": {"stringValue": v.text}, "textFormatRuns": runs}]}],
                        "fields": "userEnteredValue,textFormatRuns",
                    }
                }
            )
    return out
