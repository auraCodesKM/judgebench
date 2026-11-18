"""Judging workbook layout: tab order, widths, wrapping, banding, colours, dropdowns, hidden and
protected tabs. Formatting only: values are written by sheets_sync (RAW) and never touched here.

Idempotent: every run deletes the bandings, conditional formats and our protections it added before
and adds them again, so it is safe to re-run whenever LAYOUT_VERSION changes.
"""

from typing import Any

from google.auth.transport.requests import AuthorizedSession

from judgebench.google.workspace import (
    HIDDEN,
    JUDGE_EDITABLE,
    KEY,
    LEGACY_TABS,
    SHEETS,
    TABS,
    _call,
)

LAYOUT_VERSION = "2026-11-18.2"
PROTECT_TAG = "judgebench:"
WIDTHS: dict[str, list[int]] = {  # pixels, per visible column (the hidden key column excluded)
    "Leaderboard": [48, 140, 170, 78, 88, 52, 300, 300, 170, 64, 160, 210],
    "Team Detail": [130, 200, 82, 340, 270, 230, 280],
    "Evidence": [130, 230, 400, 110, 250],
    "Judge Reviews": [160, 150, 240, 70, 440],
    "System": [140, 70, 90, 160, 170, 140],
    "Audit Log": [150, 130, 190, 440],
    "Lists": [160, 240, 60, 160],
}
GREEN, YELLOW, RED, ORANGE = (
    {"red": 0.85, "green": 0.92, "blue": 0.83},
    {"red": 1.0, "green": 0.95, "blue": 0.8},
    {"red": 0.96, "green": 0.8, "blue": 0.8},
    {"red": 0.99, "green": 0.9, "blue": 0.8},
)
HEADER, BAND, GREY = (
    {"red": 0.82, "green": 0.89, "blue": 0.99},
    {"red": 0.97, "green": 0.97, "blue": 0.98},
    {"red": 0.91, "green": 0.92, "blue": 0.93},
)
ROWS = 1000  # validation / formatting depth


def _col(tab: str, name: str) -> int:
    return TABS[tab].index(name)


def _rng(sid: int, col: int | None = None, row0: int = 1) -> dict[str, Any]:
    r: dict[str, Any] = {"sheetId": sid, "startRowIndex": row0, "endRowIndex": ROWS}
    if col is not None:
        r |= {"startColumnIndex": col, "endColumnIndex": col + 1}
    return r


def _text_rule(sid: int, col: int, kind: str, value: str, fmt: dict[str, Any]) -> dict[str, Any]:
    return {
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [_rng(sid, col)],
                "booleanRule": {"condition": {"type": kind, "values": [{"userEnteredValue": value}]}, "format": fmt},
            },
            "index": 0,
        }
    }


def _rules(tab: str, sid: int) -> list[dict[str, Any]]:
    def bg(c: dict[str, float]) -> dict[str, Any]:
        return {"backgroundColor": c}

    if tab == "Leaderboard":
        conf, flags, by = _col(tab, "Confidence"), _col(tab, "Flags"), _col(tab, "Evaluated by")
        return [
            _text_rule(sid, conf, "TEXT_EQ", "high", bg(GREEN)),
            _text_rule(sid, conf, "TEXT_EQ", "med", bg(YELLOW)),
            _text_rule(sid, conf, "TEXT_EQ", "low", bg(RED)),
            _text_rule(sid, flags, "NUMBER_GREATER", "0", bg(ORANGE) | {"textFormat": {"bold": True}}),
            _text_rule(sid, by, "TEXT_CONTAINS", "MOCK", bg(ORANGE) | {"textFormat": {"bold": True}}),
        ]
    if tab == "Evidence":
        st = _col(tab, "Status")
        colour = {"VERIFIED": GREEN, "OBSERVED": YELLOW, "INFERRED": YELLOW, "PARTIAL": YELLOW}
        colour |= {"CONTRADICTED": RED, "MISSING": RED}
        return [_text_rule(sid, st, "TEXT_EQ", v, bg(c)) for v, c in colour.items()]
    if tab == "Team Detail":  # team header rows: bold on grey
        return [
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {**_rng(sid), "startColumnIndex": 0, "endColumnIndex": len(TABS[tab]) - 1},
                        ],
                        "booleanRule": {
                            "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": '=$B2="TEAM"'}]},
                            "format": {"backgroundColor": GREY, "textFormat": {"bold": True}},
                        },
                    },
                    "index": 0,
                }
            }
        ]
    return []


def _validation(sid: int) -> list[dict[str, Any]]:
    """Judge Reviews: dropdowns from the hidden Lists tab, score 0..criterion max."""

    def one_of(col: int, rng: str, strict: bool, msg: str) -> dict[str, Any]:
        return {
            "setDataValidation": {
                "range": _rng(sid, col),
                "rule": {
                    "condition": {"type": "ONE_OF_RANGE", "values": [{"userEnteredValue": rng}]},
                    "strict": strict,
                    "showCustomUi": True,
                    "inputMessage": msg,
                },
            }
        }

    score = TABS["Judge Reviews"].index("Score")
    formula = '=AND(ISNUMBER(D2),D2>=0,D2<=IFERROR(VLOOKUP(C2,INDIRECT("Lists!B2:C100"),2,FALSE),-1))'
    return [
        one_of(0, "=Lists!$A$2:$A$500", True, "Pick the team"),
        one_of(1, "=Lists!$D$2:$D$100", False, "Pick your name (or type it)"),
        one_of(2, "=Lists!$B$2:$B$100", True, "Pick the criterion"),
        {
            "setDataValidation": {
                "range": _rng(sid, score),
                "rule": {
                    "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                    "strict": True,
                    "inputMessage": "A number from 0 to this criterion's max",
                },
            }
        },
    ]


def layout_requests(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """`meta`: spreadsheets.get with sheets(properties,bandedRanges,conditionalFormats,protectedRanges)."""
    sheets = {sh["properties"]["title"]: sh for sh in meta.get("sheets", [])}
    order = [*TABS, *[t for t in LEGACY_TABS if t in sheets]]
    req: list[dict[str, Any]] = []
    for index, tab in enumerate(order):
        sh = sheets[tab]
        sid = sh["properties"]["sheetId"]
        hidden = tab in HIDDEN or tab in LEGACY_TABS
        req.append(
            {
                "updateSheetProperties": {
                    "properties": {
                        "sheetId": sid,
                        "index": index,
                        "hidden": hidden,
                        "gridProperties": {"frozenRowCount": 1},
                    },
                    "fields": "index,hidden,gridProperties.frozenRowCount",
                }
            }
        )
        req += [{"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}} for b in sh.get("bandedRanges", [])]
        req += [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}} for _ in sh.get("conditionalFormats", [])]
        req += [
            {"deleteProtectedRange": {"protectedRangeId": p["protectedRangeId"]}}
            for p in sh.get("protectedRanges", [])
            if str(p.get("description", "")).startswith(PROTECT_TAG)
            or p.get("description") == "System-managed: regenerated automatically"
        ]
        if tab in LEGACY_TABS:
            continue
        header = TABS[tab]
        for c, px in enumerate(WIDTHS[tab]):
            req.append(
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": c, "endIndex": c + 1},
                        "properties": {"pixelSize": px},
                        "fields": "pixelSize",
                    }
                }
            )
        if header[-1] == KEY:  # row identity stays in the sheet, out of sight
            k = len(header) - 1
            req.append(
                {
                    "updateDimensionProperties": {
                        "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": k, "endIndex": k + 1},
                        "properties": {"hiddenByUser": True},
                        "fields": "hiddenByUser",
                    }
                }
            )
        req.append(
            {
                "repeatCell": {
                    "range": {"sheetId": sid},
                    "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                    "fields": "userEnteredFormat.wrapStrategy,userEnteredFormat.verticalAlignment",
                }
            }
        )
        req.append(
            {
                "repeatCell": {
                    "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                    "fields": "userEnteredFormat.textFormat.bold",
                }
            }
        )
        req.append(
            {
                "addBanding": {
                    "bandedRange": {
                        "range": {
                            "sheetId": sid,
                            "startRowIndex": 0,
                            "endRowIndex": ROWS,
                            "startColumnIndex": 0,
                            "endColumnIndex": len(header),
                        },
                        "rowProperties": {
                            "headerColor": HEADER,
                            "firstBandColor": {"red": 1, "green": 1, "blue": 1},
                            "secondBandColor": BAND,
                        },
                    }
                }
            }
        )
        req += _rules(tab, sid)
        if tab == "Judge Reviews":
            req += _validation(sid)
        if tab not in JUDGE_EDITABLE:
            # Hidden system tabs: owner-only. Judge-facing machine tabs: a warning only, because judges
            # may overwrite a cell on purpose (their edit is kept and audited). Not a security boundary.
            locked = tab in HIDDEN
            req.append(
                {
                    "addProtectedRange": {
                        "protectedRange": {
                            "range": {"sheetId": sid},
                            "warningOnly": not locked,
                            "description": PROTECT_TAG
                            + ("system tab" if locked else "generated; your edits are kept and logged"),
                        }
                    }
                }
            )
    return req


def apply_layout(s: AuthorizedSession, spreadsheet_id: str) -> None:
    fields = "sheets(properties(sheetId,title),bandedRanges(bandedRangeId),conditionalFormats,protectedRanges(protectedRangeId,description))"
    meta = _call(s, "GET", f"{SHEETS}/{spreadsheet_id}", params={"fields": fields})
    have = {sh["properties"]["title"] for sh in meta.get("sheets", [])}
    missing = [t for t in TABS if t not in have]
    if missing:
        _call(
            s,
            "POST",
            f"{SHEETS}/{spreadsheet_id}:batchUpdate",
            json={"requests": [{"addSheet": {"properties": {"title": t}}} for t in missing]},
        )
        meta = _call(s, "GET", f"{SHEETS}/{spreadsheet_id}", params={"fields": fields})
    # Headers first (judge-owned Judge Reviews: header row only, its rows are never touched).
    _call(
        s,
        "POST",
        f"{SHEETS}/{spreadsheet_id}/values:batchUpdate",
        json={"valueInputOption": "RAW", "data": [{"range": f"'{t}'!A1", "values": [h]} for t, h in TABS.items()]},
    )
    _call(s, "POST", f"{SHEETS}/{spreadsheet_id}:batchUpdate", json={"requests": layout_requests(meta)})
