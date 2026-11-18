"""`judgebench doctor`: check every credential and external dependency, print PASS/FAIL/SKIP and
the fix for each failure. Read-only. Only with LLM_MODE=vertex does it make a few tiny, billed Gemini
calls (<1k tokens in total); in the default offline mode it never calls a model.
Never prints secrets: tokens are shown as their last 4 characters at most."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from judgebench.config import settings

PUBLIC_TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo", 19 s, public since 2005


@dataclass
class Result:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""
    fix: str = ""


def _ok(name: str, detail: str = "") -> Result:
    return Result(name, "PASS", detail)


def _fail(name: str, detail: str, fix: str) -> Result:
    return Result(name, "FAIL", detail, fix)


def _skip(name: str, detail: str) -> Result:
    return Result(name, "SKIP", detail)


# --- checks ------------------------------------------------------------------------------


def check_postgres() -> list[Result]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import create_engine, text

    try:
        with create_engine(settings().database_url).connect() as c:
            current = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            vector = c.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'")).scalar()
    except Exception as exc:
        return [_fail("postgres", type(exc).__name__, "docker compose up -d --wait db && uv run alembic upgrade head")]
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    out = [_ok("postgres", "connected")]
    out.append(
        _ok("migrations", f"at {current}")
        if current == head
        else _fail("migrations", f"at {current}, head is {head}", "uv run alembic upgrade head")
    )
    out.append(
        _ok("pgvector", f"v{vector}")
        if vector
        else _fail("pgvector", "extension missing", "use the pgvector/pgvector image or Cloud SQL with vector enabled")
    )
    return out


def check_google() -> list[Result]:
    from judgebench.google import workspace

    cfg = settings()
    token = Path(cfg.google_token_file)
    if not token.exists():
        fix = (
            f"save the OAuth client JSON to {cfg.google_client_secrets_file} (M0 item 4)"
            if not Path(cfg.google_client_secrets_file).exists()
            else "uv run --env-file .env judgebench auth-google"
        )
        return [_fail("google_oauth", f"no token at {token}", fix)]
    try:
        s = workspace.session(token)
    except Exception as exc:
        return [
            _fail(
                "google_oauth",
                f"refresh failed: {type(exc).__name__}",
                "uv run --env-file .env judgebench auth-google (Testing-mode tokens expire after 7 days)",
            )
        ]
    granted = set(s.credentials.scopes or [])
    missing = set(workspace.SCOPES) - granted
    out = [_ok("google_oauth", "token refreshes")]
    out.append(
        _fail(
            "google_scopes",
            f"missing {sorted(missing)}",
            "add them under Google Auth Platform → Data access, then re-run auth-google",
        )
        if missing
        else _ok("google_scopes", "drive.file, forms.body, forms.responses.readonly")
    )
    out += _check_event_files(s)
    return out


def _check_event_files(s: Any) -> list[Result]:
    from sqlalchemy import select

    from judgebench.db.models import EventIntegration
    from judgebench.db.session import sessions
    from judgebench.google.workspace import FORMS, SHEETS

    try:
        with sessions()() as db:
            rows = db.scalars(select(EventIntegration)).all()
    except Exception:
        return [_skip("forms_sheets_access", "database unavailable")]
    if not rows:
        return [_skip("forms_sheets_access", "no event set up yet (judgebench setup events/<slug>.yaml)")]
    out = []
    for r in rows:
        url = (
            f"{FORMS}/{r.external_ids['form_id']}/responses?pageSize=1"
            if r.provider == "google_forms"
            else f"{SHEETS}/{r.external_ids['spreadsheet_id']}?fields=spreadsheetId"
        )
        resp = s.get(url, timeout=20)
        name = f"{r.provider}:{str(r.event_id)[:8]}"
        out.append(
            _ok(name, "readable")
            if resp.ok
            else _fail(name, f"HTTP {resp.status_code}", "file deleted or created by another account; re-run setup")
        )
    return out


def check_telegram() -> list[Result]:
    token = settings().telegram_bot_token
    if not token:
        return [_fail("telegram_bot", "TELEGRAM_BOT_TOKEN not set", "M0 item 5: @BotFather /newbot, put token in .env")]

    def call(method: str, **params: Any) -> dict[str, Any]:
        try:
            return dict(httpx.get(f"https://api.telegram.org/bot{token}/{method}", params=params, timeout=15).json())
        except Exception as exc:  # never echo the URL: it contains the token
            return {"ok": False, "description": type(exc).__name__}

    me = call("getMe")
    if not me.get("ok"):
        return [
            _fail(
                "telegram_bot",
                str(me.get("description", "invalid token")),
                "BotFather → /mybots → API Token; update .env",
            )
        ]
    out = [_ok("telegram_bot", f"@{me['result']['username']}")]
    chats = _event_chats()
    if not chats:
        return [*out, _skip("telegram_channel", "no active event with telegram_chat_id")]
    for slug, chat_id in chats:
        member = call("getChatMember", chat_id=chat_id, user_id=me["result"]["id"])
        res = member.get("result", {})
        if not member.get("ok"):
            out.append(
                _fail(
                    f"telegram_channel:{slug}",
                    str(member.get("description")),
                    "add the bot to the channel as admin; check telegram_chat_id in the event file",
                )
            )
        elif res.get("status") != "administrator" or res.get("can_post_messages") is False:
            out.append(
                _fail(
                    f"telegram_channel:{slug}",
                    f"bot is {res.get('status')}",
                    "Channel → Administrators → bot → enable Post Messages",
                )
            )
        else:
            out.append(_ok(f"telegram_channel:{slug}", "bot can post"))
    return out


def _event_chats() -> list[tuple[str, int]]:
    from sqlalchemy import select

    from judgebench.db.models import Event
    from judgebench.db.session import sessions

    try:
        with sessions()() as db:
            return [
                (e.slug, int(e.telegram_chat_id))
                for e in db.scalars(select(Event).where(Event.status == "active"))
                if e.telegram_chat_id
            ]
    except Exception:
        return []


def check_github() -> list[Result]:
    token = settings().github_token
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        data = httpx.get("https://api.github.com/rate_limit", headers=headers, timeout=15).json()
        core = data["resources"]["core"]
    except Exception as exc:
        return [_fail("github", type(exc).__name__, "check network access to api.github.com")]
    if not token:
        return [
            _fail(
                "github",
                f"unauthenticated ({core['limit']}/h)",
                "M0 item 6: fine-grained token, Public repositories, no permissions",
            )
        ]
    if core["limit"] < 5000:
        return [
            _fail(
                "github",
                f"token rejected (limit {core['limit']}/h)",
                "token expired or revoked: create a new one (M0 item 6)",
            )
        ]
    return [_ok("github", f"{core['remaining']}/{core['limit']} requests left, token …{token[-4:]}")]


def check_vertex() -> list[Result]:
    cfg = settings()
    if cfg.llm_mode == "offline":
        return [_skip("vertex", "LLM_MODE=offline: mock agents + lexical embedder, no model calls, $0")]
    if cfg.llm_mode == "gemini_free":
        try:
            return _check_models(_free_client(), "gemini_free", embeddings=False)
        except _Missing as exc:
            return [_fail("gemini_free", str(exc), exc.fix)]
    if not cfg.google_cloud_project:
        return [
            _fail(
                "vertex",
                "GOOGLE_CLOUD_PROJECT not set",
                "M0 item 7: set GOOGLE_CLOUD_PROJECT and GOOGLE_GENAI_USE_VERTEXAI=true",
            )
        ]
    try:
        from google import genai

        client = genai.Client(
            vertexai=True, project=cfg.google_cloud_project, location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
        )
    except Exception as exc:
        return [
            _fail(
                "vertex",
                type(exc).__name__,
                "gcloud auth application-default login && gcloud auth application-default set-quota-project <project>",
            )
        ]
    return _check_models(client, "vertex", embeddings=True)


def _free_client() -> Any:
    from google import genai

    key = settings().gemini_api_key
    if not key:
        raise _Missing("GEMINI_API_KEY not set", "M0 item 9: create an AI Studio key and put it in .env")
    return genai.Client(api_key=key, vertexai=False)


class _Missing(Exception):
    def __init__(self, detail: str, fix: str) -> None:
        super().__init__(detail)
        self.fix = fix


def _check_models(client: Any, prefix: str, embeddings: bool) -> list[Result]:
    from google.genai import types

    cfg = settings()
    out = []
    for label, model in {"model": cfg.gemini_model, "model_strong": cfg.gemini_model_strong}.items():
        if label == "model_strong" and model == cfg.gemini_model:
            continue
        try:
            r = client.models.generate_content(model=model, contents="Reply with the single word: ok")
            out.append(_ok(f"{prefix}_{label}", f"{model} answered {(r.text or '').strip()[:20]!r}"))
        except Exception as exc:
            out.append(
                _fail(
                    f"{prefix}_{label}",
                    f"{model}: {str(exc)[:120]}",
                    BILLING_FIX
                    if _billing(exc)
                    else f"set GEMINI_MODEL{'_STRONG' if label == 'model_strong' else ''} to an available model: "
                    + ", ".join(_available_models(client)[:8]),
                )
            )
    model = cfg.gemini_model
    try:
        schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
        r = client.models.generate_content(
            model=model,
            contents="Return answer=ok",
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema),
        )
        ok = json.loads(r.text or "{}").get("answer") is not None
        out.append(
            _ok(f"{prefix}_structured_output", "JSON schema respected")
            if ok
            else _fail(f"{prefix}_structured_output", f"unexpected: {(r.text or '')[:80]}", "try a newer Flash model")
        )
    except Exception as exc:
        out.append(
            _fail(
                f"{prefix}_structured_output",
                str(exc)[:120],
                BILLING_FIX if _billing(exc) else "try a newer Flash model",
            )
        )
    try:
        video = types.Part(
            file_data=types.FileData(file_uri=PUBLIC_TEST_VIDEO, mime_type="video/*"),
            video_metadata=types.VideoMetadata(start_offset="0s", end_offset="10s", fps=0.5),
        )
        r = client.models.generate_content(model=model, contents=[video, "In three words, what animal is shown?"])
        out.append(_ok(f"{prefix}_youtube_input", f"video read: {(r.text or '').strip()[:40]!r}"))
    except Exception as exc:
        out.append(
            _fail(
                f"{prefix}_youtube_input",
                str(exc)[:120],
                BILLING_FIX
                if _billing(exc)
                else "the model/region may not accept YouTube URLs; try GOOGLE_CLOUD_LOCATION=global or another model",
            )
        )
    if not embeddings:
        return [
            *out,
            _skip(f"{prefix}_embeddings", "lexical hashing embedder (no participant text sent for embedding)"),
        ]
    try:
        from judgebench.db.models import EMBED_DIM
        from judgebench.retrieval import VertexEmbedder

        [vec] = VertexEmbedder(
            cfg.google_cloud_project or "", cfg.google_cloud_location, cfg.embedding_model
        ).embed(["retrieval check"], query=True)
        out.append(
            _ok("vertex_embeddings", f"{cfg.embedding_model}: {len(vec)} dims")
            if len(vec) == EMBED_DIM
            else _fail(
                "vertex_embeddings",
                f"{cfg.embedding_model} returned {len(vec)} dims, need {EMBED_DIM}",
                "use a model that supports output_dimensionality=768 (EMBEDDING_MODEL)",
            )
        )
    except Exception as exc:
        out.append(
            _fail(
                "vertex_embeddings",
                f"{cfg.embedding_model}: {str(exc)[:120]}",
                BILLING_FIX
                if _billing(exc)
                else "set EMBEDDING_MODEL to an available Vertex embedding model (e.g. gemini-embedding-001)",
            )
        )
    return out


def _billing(exc: Exception) -> bool:
    return "billing" in str(exc).lower() or "resource_exhausted" in str(exc).lower()


BILLING_FIX = (
    "billing not enabled (Vertex) or free-tier quota used up (gemini_free: wait a minute, or until midnight "
    "Pacific for daily limits). Never enable billing for this; LLM_MODE=offline always works at $0"
)


def _available_models(client: Any) -> list[str]:
    try:
        names = [m.name.split("/")[-1] for m in client.models.list(config={"query_base": True})]
        return sorted({n for n in names if n.startswith("gemini")}, reverse=True) or ["(none listed)"]
    except Exception:
        return ["(could not list models; see Vertex AI → Model Garden)"]


def check_probe() -> list[Result]:
    url = settings().probe_url
    if url == "inprocess":
        from judgebench.worker.live_check import probe_client

        [out] = probe_client(url).probe([{"method": "GET", "url": "https://169.254.169.254/"}])
        return [
            _ok("probe", "in-process guard blocks metadata")
            if out.get("blocked")
            else _fail("probe", "guard did not block metadata", "do not deploy: SSRF guard broken")
        ]
    try:
        httpx.get(f"{url.rstrip('/')}/healthz", timeout=10).raise_for_status()
        return [_ok("probe", url)]
    except Exception as exc:
        return [
            _fail(
                "probe",
                f"{url}: {type(exc).__name__}",
                "start it: uv run uvicorn judgebench.probe.app:app --port 8081 or set PROBE_URL=inprocess",
            )
        ]


def check_pubsub() -> list[Result]:
    topic = settings().pubsub_topic
    if not topic:
        return [_skip("pubsub", "PUBSUB_TOPIC empty: local mode (worker polls the DB)")]
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/pubsub"])
        r = AuthorizedSession(creds).get(f"https://pubsub.googleapis.com/v1/{topic}", timeout=15)
        return [
            _ok("pubsub", topic)
            if r.ok
            else _fail("pubsub", f"HTTP {r.status_code}", "create the topic (deploy/README.md)")
        ]
    except Exception as exc:
        return [_fail("pubsub", type(exc).__name__, "gcloud auth application-default login")]


CHECKS: list[Callable[[], list[Result]]] = [
    check_postgres,
    check_google,
    check_telegram,
    check_github,
    check_vertex,
    check_probe,
    check_pubsub,
]


def run(checks: list[Callable[[], list[Result]]] = CHECKS) -> list[Result]:
    results: list[Result] = []
    for check in checks:
        try:
            results += check()
        except Exception as exc:  # a broken check must not hide the others
            results.append(
                _fail(check.__name__.removeprefix("check_"), f"check crashed: {type(exc).__name__}", "report this")
            )
    return results


def render(results: list[Result]) -> str:
    lines = [f"{r.status:4}  {r.name:28} {r.detail}" + (f"\n      fix: {r.fix}" if r.fix else "") for r in results]
    fails = sum(r.status == "FAIL" for r in results)
    lines.append(f"\n{len(results) - fails} ok/skipped, {fails} failing")
    return "\n".join(lines)
