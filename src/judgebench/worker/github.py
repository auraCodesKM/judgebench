"""GitHub access for the worker: resolve a commit, download its tarball (§14.1 rule 1).

No `git clone` (no hooks, no submodules, no LFS). Only fixed GitHub hosts are contacted, with
our own token, so this is not a participant-controlled URL and does not go through the probe:
the participant only supplies owner/repo, which is validated by regex. Redirects are followed
only to codeload.github.com.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

API = "https://api.github.com"
ALLOWED_HOSTS = {"api.github.com", "codeload.github.com"}
MAX_TARBALL_BYTES = 200 * 1024 * 1024
_REPO = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<repo>[A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?(?:/(?:tree|commit)/(?P<ref>[A-Za-z0-9._/-]{1,200}))?/?$"
)


class RepoError(Exception):
    def __init__(self, category: str, retryable: bool, detail: str = "") -> None:
        super().__init__(f"{category}: {detail}")
        self.category, self.retryable = category, retryable


@dataclass(frozen=True)
class RepoRef:
    owner: str
    repo: str
    ref: str | None = None


@dataclass(frozen=True)
class Tarball:
    path: Path
    sha256: str
    size: int
    truncated: bool


def parse_repo_url(url: str) -> RepoRef | None:
    url = url.strip()
    if url.startswith("github.com/"):
        url = "https://" + url
    url = url.replace("http://", "https://", 1).replace("://www.github.com/", "://github.com/", 1)
    m = _REPO.match(url)
    if not m or m["repo"] in {".", ".."}:
        return None
    return RepoRef(m["owner"], m["repo"], m["ref"])


class GitHub:
    def __init__(self, token: str | None, client: httpx.Client | None = None) -> None:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = client or httpx.Client(timeout=30, follow_redirects=False)
        self._http.headers.update(headers)

    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            resp = self._http.get(url, params=params)
        except httpx.TransportError as exc:
            raise RepoError("github_unavailable", True, type(exc).__name__) from exc
        if resp.status_code == 404:
            raise RepoError("repo_not_found", False, "repository missing or private")
        if resp.status_code in (403, 429) or resp.status_code >= 500:
            raise RepoError("github_unavailable", True, f"HTTP {resp.status_code}")
        return resp

    def resolve_commit(self, ref: RepoRef, until: datetime) -> str:
        """Latest commit on the submitted ref (default branch if none) at or before `until`,
        the Form submission time. Pushes after submitting are therefore never evaluated."""
        params = {"until": until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "per_page": "1"}
        if ref.ref:
            params["sha"] = ref.ref
        resp = self._get(f"{API}/repos/{ref.owner}/{ref.repo}/commits", params)
        if resp.status_code == 409:
            raise RepoError("repo_empty", False, "repository has no commits")
        if resp.status_code != 200:
            raise RepoError("repo_not_found", False, f"HTTP {resp.status_code}")
        commits = resp.json()
        if not isinstance(commits, list) or not commits:
            raise RepoError("no_commit_before_submission", False, f"no commit at or before {params['until']}")
        sha = commits[0].get("sha", "") if isinstance(commits[0], dict) else ""
        if not re.fullmatch(r"[0-9a-f]{40}", str(sha)):
            raise RepoError("repo_not_found", False, "no commit sha")
        return str(sha)

    def download_tarball(self, ref: RepoRef, sha: str, dest: Path) -> Tarball:
        url = f"{API}/repos/{ref.owner}/{ref.repo}/tarball/{sha}"
        for _ in range(3):  # api.github.com -> codeload.github.com
            try:
                with self._http.stream("GET", url) as resp:
                    if resp.status_code in (301, 302, 307, 308):
                        url = resp.headers.get("location", "")
                        if httpx.URL(url).host not in ALLOWED_HOSTS or httpx.URL(url).scheme != "https":
                            raise RepoError("github_redirect_refused", False, "unexpected host")
                        continue
                    if resp.status_code == 404:
                        raise RepoError("repo_not_found", False, "tarball 404")
                    if resp.status_code != 200:
                        raise RepoError("github_unavailable", True, f"tarball HTTP {resp.status_code}")
                    digest, size, truncated = hashlib.sha256(), 0, False
                    with dest.open("wb") as out:
                        for chunk in resp.iter_bytes(1024 * 1024):
                            if size + len(chunk) > MAX_TARBALL_BYTES:
                                truncated = True  # extraction records the limitation
                                break
                            out.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    return Tarball(dest, digest.hexdigest(), size, truncated)
            except httpx.TransportError as exc:
                raise RepoError("github_unavailable", True, type(exc).__name__) from exc
        raise RepoError("github_redirect_refused", False, "too many redirects")
