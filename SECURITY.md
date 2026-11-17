# Security

Submitted repositories, READMEs, decks, transcripts and URLs are untrusted input. The MVP
**reads participant code and never runs it.** Every outbound request to a participant URL goes
through one guarded client in a separate probe service whose service account has no IAM roles.

## §14.2 boundary tests

No real submission is processed until every row is green in CI (`cloudbuild.yaml` → `pytest`).
`xfail(strict=True)` marks the part of a row that later milestones prove. Strict means the test
fails loudly the moment it starts passing, so the marker must be removed.

| Threat | Status | Proven by | Remaining |
|---|---|---|---|
| Code execution (`setup.py`, `conftest.py`, `__init__.py` write a canary + connect) | **Proven** | `tests/security/test_repository_boundary.py::test_code_execution_never_happens`: canary absent, zero connection attempts, no executable files | — |
| Path traversal (`../../etc/x`, absolute paths) | **Proven** | `::test_path_traversal_rejected`: 4 entries rejected, nothing outside workspace | — |
| Symlink escape (`/etc/passwd`, `..`, write-through-link) | **Proven** | `::test_symlinks_and_hardlinks_skipped`: no links created, all paths inside workspace | — |
| Archive bomb (8 GiB inflated from ~8 MiB) | **Proven** | `::test_archive_bomb_stops_at_cap`: stops at the 1 GiB stream cap, limitation recorded | — |
| Huge files (single 2 GiB file; 100k files) | **Proven** | `::test_single_2gb_file_capped_and_processing_completes`, `::test_100k_small_files_capped_and_processing_completes` | — |
| SSRF: metadata (`169.254.169.254`, `metadata.google.internal`) | **Proven** | `tests/security/test_ssrf.py::test_metadata_blocked_before_connecting`, `::test_probe_service_blocks_metadata_and_private` | Deploy check: probe SA has no roles (M1 deploy) |
| SSRF: private ranges (`10.x`, `127.0.0.1`, `[::1]`, CGNAT, multicast, mapped IPv6, integer IPs) | **Proven** | `::test_private_ranges_blocked`, `::test_hostname_resolving_to_private_blocked` | — |
| SSRF: redirect to private | **Proven** | `::test_redirect_to_private_address_blocked`, `::test_redirect_targets_rechecked`, `::test_redirect_hop_limit` | — |
| SSRF: DNS rebinding | **Proven** | `::test_dns_rebinding_uses_pinned_address` (resolved once, pinned IP used), `::test_dns_answer_with_any_private_address_blocked` | — |
| Prompt injection | **Partially proven** | `tests/security/test_repository_boundary.py::test_prompt_injection_does_not_change_profile`: categories, technologies and adapter selection unchanged | **M2**: scores, tools and scope unchanged through the agents (`::test_prompt_injection_full_pipeline_unchanged`, xfail strict) |
| Cross-event access | **Partially proven** | `tests/security/test_cross_event.py`: scope derived from membership only; event-A lookups by a judge of B return nothing; explicit access denied + `audit_events` row; removed judge loses access immediately; composite FK rejects cross-event rows | **M3**: Telegram Copilot webhook path (`::test_copilot_webhook_denies_cross_event_question`, xfail strict) |

Supporting tests: SSRF scheme/port/credential rules, request caps, no cookies/auth headers,
response-size cap (`test_ssrf.py`); Sheets formula injection stays text
(`tests/test_setup_and_projection.py`); bot token never stored in errors
(`tests/test_telegram_notify.py`).

## Boundary rules (§14.1) and where they live

| Rule | Implementation |
|---|---|
| Fetch the tarball of a pinned commit via the GitHub API; no clone, hooks or submodules | `worker/github.py` |
| Never install, build, import or run participant code; text + `ast.parse` only | `adapters/profiler.py` (notebooks: JSON → `ast.parse`, magics dropped) |
| Extraction limits; reject absolute, `..`, links, devices | `worker/extract.py` |
| One guarded client: HTTPS:443 only, DNS pinned, all answers checked, redirects re-validated, caps, no cookies/credentials | `probe/guard.py`, served by `probe/app.py` |
| Participant text is data | The profiler ignores prose; Telegram is plain text; Sheets is RAW. M2 adds delimited data blocks in prompts |
| One SA per component, never the default compute SA; probe SA has no roles | `cloudbuild.yaml` deploy step; SA setup in `M0_SETUP.md` |
| Google refresh token only in the API component | Mounted only on `jb-api` |

## Reporting

This is a pre-release project. Report issues privately to the maintainer; do not open a public issue.
