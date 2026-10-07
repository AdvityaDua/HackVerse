# HackVerse Backend Audit

Audit date: 2026-10-07. Scope: `backend/` as committed, checked against `backend-implementation-plan.md`, `backend-phased-task-list.md`, and `hackverse-mvp-prd.md`.

> **Remediation status (2026-10-07).** All blockers, all security findings, and
> all architectural items except the three decision-dependent ones have been
> fixed. See [§11 Remediation record](#11-remediation-record) for what changed,
> what is verified, and what is deliberately still open. The findings below are
> kept as written so the reasoning stays reviewable.

## 1. Verdict

**The backend does not run.** `create_app()` raises at import time for two independent reasons, so `uvicorn app.main:app` cannot start and the one integration test errors during collection. The code for roughly plan phases 1–6 plus parts of 8–9 has been *written*, but it has never been *executed* in its current state.

Beyond the boot failure, the implementation has diverged from the plan in ways that matter for a live event:

- The realtime layer is **single-process only**. No Redis pub/sub exists, which breaks the PRD's 500-user deployment shape and makes announcements undeliverable across instances.
- Movement is broadcast to **every user in the event**, not to nearby users. This is O(n²) fanout and contradicts PRD §15 explicitly.
- Presence TTL is applied to the **event-wide hash**, not per player, so disconnected users never expire and a quiet event deletes everyone's presence at once.
- Room access policy and the room schema **use different vocabularies**, so `public`, `mentor`, and `sponsor` rooms are enterable by admins only.
- There is **no endpoint that creates an invite**, so the P0 join flow is unreachable without manual SQL.
- The clean-architecture layering in plan §3 is **not implemented**: there are no repositories, and the two "use cases" that exist reference ports that do not exist and are wired to nothing.

Nothing here is unsalvageable. Most of it is the predictable result of writing eight phases before running one. But the current state should not be treated as "phases 1–8 done".

## 2. How This Was Verified

```text
uv run ruff check .                      # 6 errors
uv run pytest -q                         # 1 error during collection, 0 tests run
uv run python -c "from app.main import create_app; create_app()"   # TypeError
grep -rn "generate_invite_token" app/    # defined, never called
grep -rni "rate_limit|throttle" app/     # no matches
grep -rni "cors" app/                    # no matches
grep -rni "structlog|sentry|getLogger"   # no matches
grep -rn "pool_size|max_overflow" app/   # no matches
```

Plus a full read of all 3,353 lines of `app/`, `migrations/`, and `tests/`.

## 3. Blockers

### B1. `require_event_role` returns `Depends(...)` and is then wrapped in `Depends(...)` again — Blocker

`app/api/deps.py:80-89` returns `Depends(role_dependency)`. Every call site wraps it again:

```python
member: Annotated[EventMemberModel, Depends(require_event_role(["admin"]))]
```

This produces `Depends(Depends(...))`. FastAPI calls `inspect.signature()` on the inner `Depends` object and raises:

```text
TypeError: Depends(dependency=<function require_event_role.<locals>.role_dependency ...>) is not a callable object
```

Affects every admin-gated route: event PATCH/publish, rooms POST/PATCH, members GET/PATCH, teams POST/PATCH/import, announcements POST, maps upload. Because it fires at app build time, **the whole service fails to start**, not just those routes.

Fix: have `require_event_role` return the raw inner function (call sites already wrap it), or keep the `Depends` and change call sites to `member = require_event_role(["admin"])` as a default value. One-line either way.

### B2. `python-multipart` is missing from dependencies — Blocker

`map_routes.py` and `team_routes.py` use `UploadFile`/`Form`. FastAPI raises `RuntimeError: Form data requires "python-multipart" to be installed` at app build time. The package is in neither `pyproject.toml` nor `uv.lock` nor `.venv`.

Fix: add `python-multipart` to `[project].dependencies`.

Both blockers are at app-construction time, so **the README's quickstart has never worked against this code**. That is the single most useful signal in this audit: add a CI job that does nothing but `create_app()` and `pytest`, today.

## 4. Phase Verification Against the Plan

| Plan phase | Claimed | Actual | Status |
|---|---|---|---|
| 1 Project foundation | README says "Phase 1" | Scaffold, settings, async DB, Redis, `/health`, Alembic, Compose all present. App does not boot; `pytest` collects 0 tests; `ruff` fails | **Partial** |
| 2 Auth + membership | Implemented | Google OAuth start/callback, ID-token verification, invite hashing, claim, session cookie, WS cookie auth all present. **No invite creation endpoint**, no domain-match option, no local test-auth override, no profile update | **Partial** |
| 3 Event setup APIs | Implemented | Event create/get/patch/publish/config, map upload. No `/events` list, no publish validation, no `logo_url` in responses, CSV import is a stub | **Partial** |
| 4 Rooms + teams | Implemented | Room and team CRUD, team-room FK, `CanEnterRoomPolicy`. Policy vocabulary is broken (see A4). No `members/import`, no `rooms/{id}/authorize` | **Partial / broken** |
| 5 WebSocket presence | Implemented | `/ws/events/{id}`, in-process manager, Redis presence, `player:move`. No `player:join` handler, no `player:status`, no `nearby:update`, no rate limiting, no movement validation, TTL is wrong, no pub/sub | **Partial / broken** |
| 6 Chat + announcements | Implemented | Room chat with persistence, announcement model + REST + broadcast. Announcement has no title/priority/target. No retention worker. No DM scope | **Partial** |
| 7 Mentor flow | Not started | No `mentor_profiles`, no `mentor_requests`, no routes, no WS events | **Not started** |
| 8 Sponsors + analytics | Migration named "phase 8" | No `sponsors`, no `sponsor_booths`, no analytics endpoints. The migration actually adds an XP `interactions` table and a leaderboard | **Not started / diverged** |
| 9 WebRTC signaling | Partially | `rtc:offer/answer/ice` forwarding and a TURN endpoint exist. Authorization is room-equality only, which blocks proximity audio entirely. TURN endpoint returns a public STUN server and no TURN | **Partial / broken** |
| 10 Reliability + load | Not started | No logging (zero logger calls in the codebase), no metrics, no rate limits, no load script, no deployment checklist | **Not started** |

Undocumented addition: a **gamification layer** (`XPService`, `interactions` table, `/leaderboard`) appears in no plan or PRD phase.

## 5. Architectural Decisions That Need Reversing

Ordered by what will hurt first in a live event.

### A1. In-process `ConnectionManager` with no Redis pub/sub — Critical

`app/api/ws/connection_manager.py` is a module-level singleton holding `dict[event_id][user_id] → WebSocket`. The plan's `infrastructure/redis/pubsub.py` was never written.

Consequences:
- **Announcements silently reach only some users.** `announcement_routes.py:44` calls `manager.broadcast_to_event` directly from an HTTP handler. With two API replicas (PRD's 500-user shape), an announcement created on replica A never reaches clients on replica B. This breaks the PRD's hardest timing requirement ("within 3 seconds") in a way that looks like it works in testing and fails at the pilot.
- **Horizontal scaling is impossible.** PRD §15 and §26 assume 2–3 WebSocket instances. Today you are capped at one process, which also means no rolling deploys without dropping every connection.
- HTTP handlers reaching into WebSocket transport state is a layering violation: `api/http` → `api/ws`. The plan routed both through `application/`.

Risk: you discover this during the first event that needs a second instance, i.e. exactly when you cannot fix it.

Fix: every outbound fanout goes `publish to Redis channel event:{id}` → each instance's subscriber loop → local sockets. Non-negotiable before any multi-instance deploy.

### A2. Movement is broadcast to the whole event — Critical

`event_socket.py:118` sends every `player:move` to all connected users via `broadcast_to_event`. PRD §15: *"Server broadcasts nearby positions, not entire event state, for larger events."*

At 300 users moving at 12 updates/second, that is 300 × 12 × 300 ≈ **1.08M JSON messages/second** out of one Python process. It will fall over well before 100 users.

Three compounding problems in the same loop:
- `model_dump_json()` is called once per broadcast but the `for` loop `await`s each `send_text` **sequentially**. One slow client (college Wi-Fi, the exact target user) stalls delivery to everyone behind it in the dict — head-of-line blocking across the whole event.
- A dead socket raises inside that loop. In `player:move` the caller wraps everything in `except Exception: pass`, so **the broadcast aborts partway** and the remaining users silently miss the update. No per-connection isolation, no send timeout, no outbound queue.
- No interest management at all, so the PRD's stated mitigation for "too many avatars degrade performance" has no server-side half.

Fix: spatial bucketing (grid cell or room) in Redis, broadcast only to the sender's cell plus neighbours, per-connection send task with a bounded queue that drops stale position frames rather than blocking, and `asyncio.gather` with per-socket exception capture.

### A3. Presence TTL is on the event hash, not per player — Critical

`presence_store.py:32-33`:

```python
await self.redis.hset(key, str(user_id), json.dumps(data))
await self.redis.expire(key, PRESENCE_TTL_SECONDS)   # key = event:{id}:presence
```

One Redis hash per event, with a 5-minute TTL on the *whole hash*, refreshed by any player's movement. Two failure modes, both bad:

- **Ghosts never expire.** A client that dies without a clean close (laptop lid, Wi-Fi drop — the common case on the target network) leaves its hash field forever, because only `remove_player` on `WebSocketDisconnect` deletes it, and the TTL never applies to individual fields. Plan Phase 5's acceptance criterion "Disconnected users disappear after TTL" is not met, and the nearby/room-presence lists will fill with phantom avatars over a 36-hour event.
- **Everyone vanishes at once.** If an event has no movement for 5 minutes (overnight, a stage session where people sit still), the entire hash expires and all presence is deleted while users are still connected.

Also: `hset` + `expire` are two round trips per movement message, unpipelined.

Fix: per-player keys with their own TTL (`SETEX event:{id}:player:{uid}`), or keep the hash for data plus a `ZSET` of last-seen timestamps and sweep stale members on a timer. Pipeline the writes.

### A4. Room type vocabulary mismatch — High, and already broken

`domain/rooms/policies.py` grants access when `room.type == "open"`. Nothing else in the system uses `"open"`. The PRD, the schema comment, and plan §6 all specify `public | team | mentor | sponsor | admin`, and `room_routes.py` accepts `type: str` with no validation.

So a room created with the documented type `"public"` falls through every branch and hits `return False`. **Main Stage, Mentor Lounge, Sponsor Booths, and Chill Zone are enterable by admins only.** `mentor` and `sponsor` types are likewise unhandled.

Root cause is worth naming because it will recur: every enum in this codebase is a bare `String(32)` with no shared constant, no Pydantic `Literal`, and no DB constraint — `role`, `status`, `room.type`, `event.status`, `announcement.priority`. `member_routes.py:50` happily writes `role="admn"`, permanently locking a user out of their own event with no error.

Fix: one `domain/common/enums.py` with `StrEnum` definitions, used by Pydantic request models (so bad values are 422s), by the policies, and by PG `CHECK` constraints or native enums in a migration.

### A5. Blocking and unbounded database access inside the WebSocket loop — High

Three separate issues that converge on the same failure:

- `room:enter` opens a fresh session and runs **three queries** (room, team memberships, teams) on every message. `chat:message` opens a session and `INSERT`s synchronously before broadcasting. `player:interact` opens a session and runs a dedupe `SELECT` plus `INSERT`.
- `db/session.py` configures **no pool size**, so SQLAlchemy defaults to `pool_size=5, max_overflow=10` — 15 connections for the entire process, shared between REST and every WebSocket message.
- There is no rate limiting anywhere, so a single client can issue `chat:message` in a tight loop and exhaust the pool.

At a few hundred users this means REST requests queue behind WebSocket chatter and time out. PRD §26 lists "database connection count" as a primary bottleneck; nothing has been done about it.

Fix: cache room/team authorization in Redis per connection (it changes rarely), make chat persistence a fire-and-forget background write or a batched insert, configure the pool explicitly, and add Redis token-bucket limits on move, chat, and interact.

### A6. WebRTC authorization makes proximity audio impossible — High

`event_socket.py:290`:

```python
if not sender_room or sender_room != target_room:
    # reject
```

`room_id` is only ever set by `room:enter`. Two users standing next to each other in the open campus both have `room_id = None`, so `not sender_room` is true and **signaling is rejected**. Media therefore works only inside explicit rooms — but proximity audio in shared space is the PRD's stated core differentiator and "single most valuable feature".

Also missing from the authorization: any distance check (PRD specifies ≤3 tiles), the 4–6 peer cap from PRD §16, block-list enforcement, and session teardown on move-away. And the payload key is `targetUserId` where the PRD contract says `targetMemberId`.

Fix: authorize on `(same room) OR (both in the same audio zone AND within the configured tile radius)`, read from presence; enforce the peer cap server-side; emit a teardown message when proximity is lost.

### A7. `except Exception: pass` as the standard error strategy — High

Nine bare `except Exception` blocks, most of them `pass`, wrapping every WebSocket message handler. Combined with **zero logging calls in the entire codebase** (no `logging`, no `structlog`, no Sentry), this means:

- A malformed or malicious message is indistinguishable from a working one. The client gets no `system:error` and the server records nothing.
- `room:enter` with a nonexistent room ID `continue`s silently — the user sees nothing happen, forever, with no way for support to diagnose it mid-event.
- Real infrastructure failures (Redis down, DB pool exhausted) are swallowed per-message, so the service degrades invisibly instead of failing loudly.

Plan §16 specifies a minimum log set (request ID, event ID, member ID, connect/disconnect, room deny, RTC failure, moderation). None of it exists. This is the difference between a 10-minute incident and a 3-hour one during a pilot.

### A8. The WebSocket handler leaks connections on any non-disconnect exception — High

`event_socket.py:314` catches only `WebSocketDisconnect`. Cleanup (`manager.disconnect`, `presence_store.remove_player`, `player:left`) lives in that single handler, with no `finally`. Any other escaping exception — a Redis timeout in the snapshot send, a `CancelledError` on shutdown — leaves the socket in `active_connections` and the player in Redis presence permanently.

Compounding it, connections are keyed by `user_id`, so **opening a second tab silently overwrites the first** without closing it; when the orphaned first tab eventually disconnects, `disconnect()` deletes the *new* tab's entry. The PRD calls out "user has multiple tabs" as an explicit moderation edge case.

Fix: `try/finally` for cleanup, key connections by a per-connection ID with `user_id → set[connection]`, and close superseded sockets explicitly.

### A9. Local-disk file storage instead of object storage — High

`map_routes.py` writes uploads to a path derived from `__file__` and serves them via `StaticFiles` from `/uploads`. The plan and PRD both specify R2/S3 plus CDN.

Risks: uploads vanish on every container restart or redeploy; with two instances, a map uploaded to A 404s on B; the ephemeral-disk assumption of every container platform makes this a guaranteed data-loss path. The user-visible symptom is the PRD's "if map asset fails, show lobby" degradation firing permanently after a routine deploy.

Fix: write to S3/R2 behind a `storage/object_store.py` port (the plan already specified this file) and store only the resulting URL.

### A10. The clean-architecture layering is decorative — Medium, but compounding

Plan §3's dependency rule is not implemented:

- There is no `infrastructure/db/repositories/` directory. Routes and the WebSocket handler import SQLAlchemy models directly and call `session.scalar()` inline.
- `application/teams/assign_room.py` and `application/events/csv_import.py` define use cases taking `team_repository`, `room_repository`, `unit_of_work` — **none of which exist anywhere**. Both classes are dead code, imported by nothing.
- The only real domain logic is `CanEnterRoomPolicy`; the other seven policies named in plan §12 (`CanSendRoomChatPolicy`, `CanRequestMentorPolicy`, `CanStartMediaSessionPolicy`, `CanModerateMemberPolicy`, …) do not exist. Their logic is inlined in the WebSocket handler or absent.
- `XPService` takes an `AsyncSession` and calls `commit()` itself, so transaction control is inverted.

This is the reason the plan's testing strategy ("unit tests with in-memory repositories") produced four trivial tests instead of the permission and spoofing tests it called for. Either commit to the ports-and-adapters structure or drop it from the plan and test through the API — but the current half-state gives the costs of both and the benefits of neither.

### A11. Analytics storage was repurposed as an XP ledger — Medium

The PRD specifies `interaction_events(actor_member_id, type, target_type, target_id, metadata jsonb)` as the single analytics substrate for §24's metric list. The implemented `interactions` table dropped `metadata jsonb`, dropped `target_type`, replaced `actor_member_id` with `user_id`, and added `points_awarded`.

Consequences: there is nowhere to record a clicked sponsor URL, a WebRTC setup outcome, a TURN-relay flag, or a stage-attendance duration — all PRD-required metrics. Losing `actor_member_id` for `user_id` also loses the event-scoped role, so "sponsor booth visits by role" becomes a join away and "do not show sponsors individual-user granularity" has no structural enforcement.

Fix: restore the PRD's `interaction_events` shape with `metadata jsonb` and treat XP as a derived view over it, not a replacement.

### A12. Session tokens cannot be revoked — Medium

`tokens.py` issues a stateless HMAC token with a 7-day TTL. `/auth/logout` only deletes the cookie — the token stays valid until expiry. There is no `jti`, no session table, no Redis denylist.

Ban still works for REST and new WebSocket connections, because `get_current_event_member` re-checks `status == "active"` per request. But: a leaked or shoulder-surfed cookie is valid for a week with no way to kill it, logout on a shared college lab machine does not actually end the session, and an already-connected banned user is never disconnected (there is no moderation push at all).

Also: one `session_secret` signs sessions, OAuth state, and nothing else is separated; there is no key-rotation path.

### A13. `verify_google_id_token` blocks the event loop — Medium

`google.auth.transport.requests.Request` is the **synchronous** requests-based transport, called directly inside the `async def` callback handler. It performs a network fetch of Google's signing certificates. While it runs, the entire event loop — including every WebSocket connection on that worker — is frozen.

This lands at the worst possible moment: event kickoff, when a few hundred participants hit the callback within the same minute. Wrap it in `run_in_threadpool`, or verify with an async JWKS client and a cached key set.

## 6. Security Findings

| ID | Finding | Severity |
|---|---|---|
| S1 | **No `Origin` validation on the WebSocket handshake.** `get_ws_member` reads the session from `websocket.cookies`. WebSockets are exempt from CORS, so any website can open `ws://…/ws/events/{id}` in a visitor's browser and it will authenticate as that logged-in user (cross-site WebSocket hijacking) — reading all room chat and injecting movement, chat, and RTC messages. Needs an allowlist check on the `Origin` header at handshake. | **Critical** |
| S2 | **`session_secret` defaults to `"change-me"`** with no startup guard for non-local `app_env`. If the env var is missing in production, anyone can forge a session cookie for any `user_id`, including admins. `session_cookie_secure` also defaults to `False`. Add a fail-fast validator: refuse to boot outside `local`/`test` with default secrets. | **Critical** |
| S3 | **`POST /events` requires only an authenticated user**, not an admin — `create_event` takes `CurrentUserDep` and makes the caller an `admin` of the new event. Any participant who joined any pilot can create unlimited events, upload unlimited files, and generate unlimited announcements. It is also the privilege-escalation path that makes S4 reachable by anyone. | **High** |
| S4 | **Unrestricted file upload served from the app origin.** `map_routes.upload_map` accepts any file, any size, any extension, with `filename` interpolated into the stored name, then serves it from `/uploads` via `StaticFiles` — which sets `Content-Type` from the extension. Uploading `payload.html` yields same-origin HTML/JS execution against authenticated users. (Path traversal is *accidentally* blocked by the UUID prefix, but nothing else is.) Needs an extension/content-type allowlist, a size cap, a sanitized generated filename, JSON schema validation, and ideally a separate asset origin. | **High** |
| S5 | **Ban can be silently undone by invite claim.** `invite_claim.py:62-64` sets `existing_member.status = "active"` unconditionally. Any *new or still-pending* invite matching a banned user's email reactivates them, with no check for `banned`/`removed`. It also overwrites `role` unconditionally, so a stray participant invite demotes an admin. | **High** |
| S6 | **No rate limiting anywhere** — not on `player:move`, `chat:message`, `player:interact`, the Google callback, or invite claim. Plan §17 requires all five. One client can saturate the DB pool (A5) or the broadcast loop (A2). | **High** |
| S7 | **No server-side movement validation.** `PlayerMovePayload(x: float, y: float)` accepts any value, including out-of-bounds, `inf`, and `NaN` (which then serializes to invalid JSON and breaks every receiving client). No collision check, no speed check, no bounds check. Plan Phase 5 acceptance explicitly requires rejecting invalid movement. | **Medium** |
| S8 | **Chat content is unbounded.** `ChatMessagePayload.content: str` has no `max_length`, and the column is `Text`. A single 10 MB message is persisted and fanned out to every room member. | **Medium** |
| S9 | **No CSRF protection** on cookie-authenticated state-changing routes. `SameSite=Lax` blocks cross-site POSTs in current browsers, so this is mitigated in practice, but it is load-bearing and undocumented — and it will break the moment someone sets `SameSite=None` to solve a cross-origin problem. | **Medium** |
| S10 | **OAuth `state` is not bound to the browser.** It is server-signed (so unforgeable) but carries no cookie-paired nonce, leaving a login-CSRF window. The raw invite token is also embedded in `state` as readable base64 and passed as a query parameter, so it lands in logs and browser history. No PKCE. | **Low–Medium** |
| S11 | **No moderation primitives at all** — no mute/kick/ban endpoint, no `moderation:action` push, no report handling, and no audit log table. PRD §21 requires an audit log for admin moderation actions; PRD lists abuse in public spaces as a High-impact safety risk. Setting `status="banned"` via `PATCH /members/{id}` does not disconnect the live socket. | **High (as a gap)** |

## 7. PRD Alignment Gaps

Beyond the not-yet-started phases, these are places where what exists will not satisfy the PRD as written.

**Join flow (P0) is not reachable.** `generate_invite_token()` is defined and never called. There is no `POST /events/{id}/invites`, no `members/import` route, and `CsvImportService` returns `{"status": "dry_run_completed"}` without writing anything. `claim_strict_invite` requires an exact email match, and the plan's domain-match option was never built. So onboarding 300 participants currently requires hand-inserting 300 `event_invites` rows with correct emails. This blocks PRD "setup in under 2 hours", "enter in under 60 seconds", and the P0 CSV-import feature simultaneously.

**Participant profile cannot be set.** PRD journey step 3 is name, optional college, optional role, and avatar selection. `users` has `avatar_id` and `college` columns, but there is no `PATCH /users/me` or equivalent — they can only ever be `NULL`. `/auth/me` also omits `avatar_id`, `college`, and event role, so the frontend cannot render onboarding or an avatar at all.

**Announcements cannot satisfy their own acceptance criteria.** The model is `content` only. PRD requires `title`, `priority` (normal/high/critical), `target` (all/participants/mentors/sponsors/team/room), and `target_id`, with "critical announcements can be pinned". None of those fields exist, so targeting and pinning are unimplementable without a migration.

**TURN is the PRD's #1 technical risk and is a stub.** `/turn-config` returns `stun:stun.l.google.com:19302` and nothing else. No TURN server, no credentials, no ephemeral HMAC credential generation, no relay-usage metric. PRD §28: "College Wi-Fi may block peer connections; TURN fallback matters." As built, media will fail on precisely the networks the product targets.

**No event-status enforcement.** `publish_event` sets `status = "live"` with no checks — no map, no spawn points, no `ends_at > starts_at`, no guard against re-publishing or publishing an `ended` event. And nothing anywhere checks `event.status` or `starts_at`/`ends_at` before letting a user join the campus or the WebSocket, so `draft` and `archived` events are fully playable. PRD edge cases "missing map" and "invalid event time" are unhandled.

**Map selection is ambiguous.** The PRD has `events.map_id` (one active map). The implementation inverts it to `maps.event_id` with an `is_active` flag that `upload_map` never sets and `get_event_config` never filters on — it returns *all* maps as a list. The `ix_maps_event_active` index is dead. The client is left to guess which map is the campus.

**Chat retention is unimplemented.** Plan recommended 30 days configurable; `chat_messages` has no retention job, and the plan's `workers/cleanup.py` does not exist (there is no `workers/` package).

**Event isolation is enforced ad hoc.** `room:enter` correctly checks `room.event_id != event_id`, and `CanEnterRoomPolicy` re-checks it — good. But `update_event`, `publish_event`, and `get_event_config` query `EventModel` by ID with no None check (500 instead of 404), `list_announcements`/`list_teams`/`list_rooms` rely on the caller passing a matching `event_id` path param that `CurrentEventMemberDep` happens to validate, and nothing enforces it at the repository layer as plan §17 required. It works today by convention, which is exactly how cross-event leaks appear later.

**Missing from the documented REST surface:** `GET /events` (list), `GET /events/{id}/links`, `POST /events/{id}/members/import`, `POST /events/{id}/rooms/{id}/authorize`, all six mentor routes, all four sponsor routes, all three analytics routes. Present but undocumented: `GET /events/{id}/leaderboard`, `GET /events/{id}/maps`, `POST /events/{id}/maps/upload`.

## 8. Quality Gates

- `pytest` runs **zero tests** (collection error). The suite is 4 trivial unit tests plus 1 health test; there is no `conftest.py`, no fixtures, no test database, and no `tests/e2e/`.
- The health test asserts only `checks["app"] is True`, so it passes with Postgres and Redis both down.
- Plan §13 requires room-access permission tests and WebSocket spoofing tests. Neither exists — nor does any test of the single most security-sensitive function in the codebase, `CanEnterRoomPolicy`, which is currently broken (A4). A ten-line unit test would have caught it.
- `ruff check` fails with 6 errors (3× B008, 3× E501). No type checker is installed despite plan §4 specifying Pyright or mypy.
- No CI workflow, no pre-commit hook, no `make check`.
- `/health` returns HTTP 200 even when `status == "degraded"`, so a load balancer will keep routing to an instance with a dead database. No separate liveness/readiness split, no TURN check.

## 9. What Is Solid

Worth preserving as-is:

- **Google OAuth verification is correct.** It uses the `google-auth` library (so issuer, audience, expiry, and signature are properly validated), enforces `email_verified`, and stores `google_sub` so email changes don't break identity. The Google-only policy from plan §7A is genuinely respected — there is no password or email-link path anywhere.
- **Invite tokens are handled correctly**: 256-bit `secrets.token_urlsafe`, SHA-256 hashed at rest, unique index on the hash, expiry and status checked, email normalized on both sides.
- **Session token implementation is sound** for what it is: HMAC-SHA256, `hmac.compare_digest`, a `typ` claim preventing cross-use between session and OAuth-state tokens, and expiry enforced.
- **The migration chain is clean and linear** (0001→0005, single head) and matches the models.
- **The schema is largely well-built**: correct `ON DELETE` behaviour, the PRD's required unique constraints (`event_members(event_id,user_id)`, `teams(event_id,name)`, `team_members(team_id,event_member_id)`), and sensible composite indexes — including the one the XP dedupe query actually needs.
- **Error envelope handling is consistent** (`{error: {code, message}}` via three exception handlers), which is what the frontend plan assumed.
- Durable-vs-ephemeral separation is respected in principle: positions are in Redis, never Postgres.

## 10. Recommended Remediation Order

**Stop-the-line (hours).** Make the thing run and keep it running.
1. B1 `require_event_role`, B2 `python-multipart`.
2. A CI job: `ruff check` + `create_app()` + `pytest`. Nothing else merges until this is green.
3. S2 fail-fast on default secrets outside local; S1 WebSocket `Origin` allowlist.
4. A4 shared enums for `role`, `status`, `room.type`, `event.status` — plus a unit test for `CanEnterRoomPolicy` covering all five room types.

**Before any second instance or pilot (days).**
5. A1 Redis pub/sub for all fanout; route announcement delivery through `application/`, not through `api/ws`.
6. A3 per-player presence TTL + stale sweep.
7. A8 `try/finally` cleanup and per-connection keying for multi-tab.
8. A7 structured logging with `event_id`/`member_id`, and replace every `except Exception: pass` with a logged `system:error` response.
9. S6/S7/S8 rate limits, movement bounds validation, chat length cap.
10. S3 restrict `POST /events`; S5 refuse to reactivate `banned`/`removed` members on invite claim; S4 upload allowlist and size cap.
11. The invite-creation endpoint plus a real member/team CSV import — without these there is no pilot.

**Before load testing (weeks).**
12. A2 interest-managed movement broadcast with per-connection send queues.
13. A5 explicit pool config, cached room authorization, non-blocking chat persistence.
14. A13 move Google token verification off the event loop.
15. A9 object storage behind the `object_store` port.
16. A11 restore `interaction_events` with `metadata jsonb`; make XP a view over it.

**Decisions to make, not code to write.**
17. A10: commit to repositories/ports or formally drop the layer from the plan. Pick one; the half-state is the expensive option.
18. A6: settle the proximity-audio authorization model (PRD open question) before building more of Phase 9, and settle `targetUserId` vs `targetMemberId` with the frontend at the same time.
19. Gamification: product feature or experiment? It currently owns the analytics table that PRD §24 needs.
20. Announcement migration for `title`/`priority`/`target`/`target_id` — needed before the organizer dashboard is built against the current shape.

## 11. Remediation Record

Completed 2026-10-07, same session as the audit. `make backend-check` passes:
lint clean, app builds, **128 tests passing** (up from 0 — the suite could not
even be collected).

### Blockers

| ID | Fix |
|---|---|
| B1 | `require_event_role` now returns the raw callable and is exposed as a reusable `AdminMemberDep`. All 11 call sites updated. |
| B2 | `python-multipart` added to dependencies. |

Both are now guarded by a CI step that does nothing but `create_app()`, plus
`make backend-build`. This class of failure cannot recur silently.

### Architecture

| ID | Fix |
|---|---|
| A1 | New `infrastructure/redis/pubsub.py` (`RedisEventBus`) with refcounted channel subscriptions and a single reader task per process, started by a FastAPI lifespan. All fanout goes through `application/realtime/publisher.py`; HTTP handlers no longer touch WebSocket state. |
| A2 | Interest management in `domain/realtime/interest.py`: the world is divided into 20-tile cells, movement publishes to the sender's cell, and each connection subscribes to its 3×3 neighbourhood, resubscribing on cell crossing. Fanout cost is now proportional to local density, not event size. |
| A3 | Presence rewritten to per-player hash keys with their own TTL plus a `ZSET` last-seen index that gets swept. Position and status are separate hash fields, so walking no longer clears a mentor's `busy`/`dnd`. |
| A4 | `domain/common/enums.py` defines every vocabulary; `CanEnterRoomPolicy` now returns a typed `RoomAccessDecision` and handles all five room types, including sponsor-only back areas and mentor grants. CHECK constraints added in the migration. 24 tests. |
| A5 | Pool sized explicitly (20+10, 10s timeout); room authorization cached per connection for 30s; chat persistence moved off the broadcast path. |
| A6 | RTC authorization is now `same room OR within proximity radius`, so open-campus proximity audio works at all. Peer cap enforced server-side. Payload renamed to `targetMemberId` per the PRD. |
| A7 | `structlog` with contextvars; `event_id`/`user_id`/`member_id`/`request_id` bound per connection and per request. Every `except Exception: pass` replaced with a logged error plus a `system:error` reply. |
| A8 | Cleanup moved to `finally`. Connections have their own identity, so a user may hold several tabs; `player:left` only fires when the last one closes. |
| A9 | `infrastructure/storage/object_store.py` with `LocalObjectStore` and `S3ObjectStore` behind a port, selected by `STORAGE_BACKEND`. |
| A11 | `interactions` restored to the PRD's shape: `actor_member_id`, `target_type`, `details jsonb`. XP is now derived rather than the point of the table. |
| A12 | Session tokens carry a `jti`; logout revokes it via a Redis denylist with a TTL matching the token's remaining life. |
| A13 | Google ID-token verification moved to `run_in_threadpool`, so the cert fetch no longer freezes the event loop during a login spike. |

### Security

| ID | Fix |
|---|---|
| S1 | `Origin` allowlist on the WebSocket handshake, plus `CORSMiddleware` with an explicit credentialed origin list. 5 tests. |
| S2 | `Settings` refuses to construct outside `local`/`test` with a default or short `SESSION_SECRET`, insecure cookies, or missing Google credentials. 6 tests. |
| S3 | `POST /events` gated on `users.can_create_events`. |
| S4 | Uploads validated by extension *and* content type, size-capped, JSON-parsed, and stored under a generated filename. |
| S5 | Invite claim refuses to reactivate `banned`/`removed` members; claim row locked with `FOR UPDATE`. |
| S6 | Redis token buckets on movement, chat, interact, signalling, the OAuth callback, and invite claim. |
| S7 | Coordinates must be finite and in-bounds; teleports rejected with a `player:resync`. |
| S8 | Chat capped at 2000 characters, trimmed, non-blank. |
| S11 | `moderation_actions` audit log and `user_reports` queue; `POST /members/{id}/moderate` writes the audit row, pushes `moderation:action`, and closes every socket the member holds. Last-admin protection on demote/ban. |

### PRD gaps closed

- **Invites now exist**: `POST/GET /events/{id}/invites`, revoke, and `POST /events/{id}/invites/import`. `generate_invite_token` is wired up.
- **CSV import is real** for both members and teams, defaulting to a dry run with per-row errors, and covering the PRD's named edge cases (duplicate emails, one person in two teams). 17 tests.
- **Profile/onboarding**: `PATCH /auth/me` plus `headline`/`onboarded` columns; `GET /auth/me` returns avatar, college, and memberships.
- **Announcements**: `title`, `priority`, `target`, `target_id`, `pinned`; critical announcements pin automatically; role-targeted history is filtered per caller.
- **TURN**: real coturn REST credentials (HMAC-SHA1, expiring), with `turn_available` so the client can warn instead of failing mysteriously.
- **Event lifecycle**: publish is blocked unless there is an active map with spawn points, a room, and an admin; `GET /publish-checklist` exposes the blockers; the WebSocket refuses non-live events for non-admins.
- **Map selection**: exactly one `is_active` map per event, with `POST /maps/{id}/activate`.
- **Analytics**: `/analytics/summary`, `/health`, `/export` (aggregated, not row-level), `/presence`, `/roles`.
- **Chat history** endpoint, and retention enforced by `app/workers/cleanup.py`.
- **`GET /events`** so the dashboard can list events.
- Latent bug found while fixing: the leaderboard queried `UserModel.name`, which does not exist. It is fixed and now behind `ENABLE_GAMIFICATION`.

Route count went from 24 to 44.

### Still open, deliberately

1. **A10 — repositories/ports.** Routes still use SQLAlchemy directly. The dead `assign_room` use case remains. This is the architectural choice from §10 item 17: commit to the layer or drop it from the plan. Not something to decide mid-remediation.
2. **Mentor and sponsor modules** (plan phases 7–8) are still unimplemented. The analytics action types and `room_grants` are in place for them.
3. **Verification gap**: Docker was not running, so migrations were validated with `alembic upgrade head --sql` (offline SQL generation for the whole chain) and models against migrations by inspection, not by applying to a live Postgres. The CI workflow does apply, downgrade, re-apply and run a schema-drift check — **run it before trusting the migration**.
4. **`chat_message` ordering**: broadcast now precedes persistence, so a client could in principle see a message that failed to store. That is the right trade for latency, but it means the history endpoint is not guaranteed byte-identical to what was shown live.
