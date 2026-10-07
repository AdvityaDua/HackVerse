# HackVerse Frontend Phased Task List

This task list is the implementation track for the HackVerse frontend. It mirrors `backend-phased-task-list.md` and is sequenced so each phase consumes backend capabilities that already exist, or names the backend phase it is gated on.

Architecture decisions live in `frontend-implementation-plan.md`.

## Phase 1: Frontend Foundation

Goal: A runnable Vite + React + TypeScript app that can authenticate-free call the backend, with lint, tests, and container in place.

Tasks:
- Create `frontend/` Vite + React + TS project with strict mode.
- Add Tailwind and the HackVerse token set (campus dark palette, dashboard neutral palette).
- Add typed env module for `VITE_API_BASE_URL`, `VITE_WS_BASE_URL`, `VITE_SENTRY_DSN`, `VITE_ENABLE_GAMIFICATION`.
- Add Vite dev proxy for `/api` and `/uploads` to `localhost:8000` so dev is same-origin.
- Add API client: `credentials: 'include'`, request IDs, `{ error: { code, message } }` parsing into typed `ApiError`.
- Add React Router skeleton, providers (TanStack Query, error boundary), and a global error screen.
- Add `ui/` primitives: Button, Panel, Modal, Toast host, Spinner, Table, Badge.
- Add Vitest + React Testing Library + Playwright configs.
- Add ESLint + Prettier.
- Add `Dockerfile` (node build → nginx) and `make frontend-*` targets in the root `Makefile`.
- Add `frontend/README.md` with local commands.
- Add OpenAPI type generation script (`make frontend-types`).

Done when:
- `make frontend-dev` serves the app and reaches `/api/v1/health` through the proxy.
- Build, lint, typecheck, and the smoke test all pass.

## Phase 2: Auth and Onboarding

Goal: Invite link to spawned-ready participant in under 60 seconds, Google only.

Tasks:
- Add `/invite/:inviteToken` landing page with event identity, dates, and invited role.
- Add resolve-invite call and distinct states for expired, revoked, already-claimed, and email-mismatch.
- Add redirect into `/api/v1/auth/google/start?invite_token=...` as a full navigation.
- Add `/auth/callback` route handling `eventId`, invite claim, and session hydration.
- Add pending-invite persistence in `sessionStorage` to survive a mid-OAuth refresh.
- Add session bootstrap query against `GET /auth/me` with no login flash on refresh.
- Add onboarding screen: display name (prefilled), optional college, optional role, avatar picker.
- Add route guards: `RequireSession`, `RequireEventMember`, `RequireRole`.
- Add logout.
- Add unit tests for guards and the claim-then-hydrate sequence.

Done when:
- A new Google user reaches onboarding from an invite link with no manual steps.
- Invalid and expired invites show specific, actionable errors.
- Refreshing an authenticated route restores the session cleanly.

## Phase 3: Campus Map and Local Movement

Goal: Render the campus and move the local avatar, no multiplayer yet.

Tasks:
- Add `GameRoot` React wrapper that mounts Phaser once and never remounts on React updates.
- Add `BootScene` loading map JSON from `GET /events/{id}/config` plus tileset and spritesheet assets.
- Add asset load progress UI and a hard load timeout.
- Add `CampusScene` with tilemap layers, collision layer, camera follow, world bounds.
- Add `LocalPlayer` with WASD + arrow movement, 4-direction animation, and Arcade collision.
- Add spawn point resolution: team spawn, then role spawn, then default.
- Add zone parsing into `ZoneSystem` for rooms, portals, interaction zones, audio zones.
- Add campus HUD shell: bottom control bar, area label, minimap/area directory, event links drawer.
- Add Lobby fallback view (event links, announcements, chat, retry) for map/asset failure.
- Add canvas resize handling and pause-on-tab-hidden.
- Add the campus route as a lazy chunk with a dynamic Phaser import.

Done when:
- A user walks the map at a stable frame rate and cannot cross blocked tiles.
- A broken map URL renders the lobby fallback, not a blank screen.
- Zone enter/exit emits correct bridge events.

## Phase 4: Realtime Multiplayer Presence

Goal: See other participants move, and survive disconnects.

Tasks:
- Add `realtime/types.ts` discriminated unions for client and server messages.
- Add `realtime/schemas.ts` Zod validation for every inbound message type.
- Add the single WebSocket client for `/api/v1/ws/events/{eventId}`.
- Add exponential backoff with jitter, heartbeat/idle detection, and an offline send queue.
- Add `dispatcher.ts` routing positions to the game bridge and everything else to stores.
- Add throttled movement sender (12Hz, change-only, with `seq`).
- Add `presence:snapshot` hydration into pooled remote sprites.
- Add `player:joined` and `player:update` handling.
- Add a stale-player sweep to cover the missing server-side `player:left`.
- Add `InterpolationSystem` for smooth remote movement.
- Add `InterestManager`: viewport culling, rendered-avatar cap, "+N more here" HUD counter.
- Add connection status banner for Connecting / Reconnecting / Degraded.
- Add reconnect restore of last room and position.
- Add tests for backoff, send queue, and inbound validation.

Done when:
- Two browsers see each other move smoothly.
- A backend restart recovers presence without a page reload.
- Remote movement causes zero React renders under the profiler.

Gated on: backend Phase 5. `player:left` and `nearby:update` are not yet emitted.

## Phase 5: Proximity and Interaction Zones

Goal: The spatial interaction layer.

Tasks:
- Add `ProximitySystem` with the PRD tiers: >6 tiles none, ≤6 tiles nearby, ≤3 tiles interactable.
- Add nearby panel with avatar, name, role badge, and status, debounced to 4 updates/second.
- Add in-canvas "Press E / click to interact" prompt.
- Add zone router mapping zone types to React panels, with stubs for sponsor, stage, and mentor desk.
- Add local block list with exclusion from nearby interaction prompts.
- Add `player:interact` emission for interaction analytics.
- Add area directory with live per-area counts.
- Add tests for the distance-tier state machine, including high-density behaviour.

Done when:
- Approaching a user adds them to nearby at 6 tiles and shows a prompt at 3.
- Walking away removes them within one debounce window.
- Blocked users never produce an interaction prompt.

## Phase 6: Rooms, Team Rooms, and Chat

Goal: Private collaboration and the text fallback everything degrades to.

Tasks:
- Add `room:enter` and `room:leave` on zone transitions and quick-link navigation.
- Add `room:entered` rendering: room header, member list, room chat.
- Add `room:denied` handling with a specific reason message.
- Add chat panel with room-scoped channels and optimistic send with rollback.
- Add a client-side chat rate guard matching the server limit, and a 2000-char cap.
- Add strict text escaping and safe link rendering (`noopener noreferrer nofollow`); no raw HTML in chat.
- Add chat history load from REST on room entry, merged with the live stream.
- Add team room UI: team name, project title, member presence, quick links, invite-mentor action.
- Add "My Team Room" quick link via `GET /events/{id}/teams/me`, with a no-team empty state.
- Add unread badges and mention highlighting.
- Add component tests for hostile chat input and denial messaging.

Done when:
- Team members can enter their room; non-members are denied via both UI and hand-crafted WS messages.
- Room chat is visible only to room members.
- XSS payloads render as inert text.

Gated on: backend Phase 6. A chat-history endpoint may need to be added.

## Phase 7: Announcements and Notifications

Goal: Organizer reach within 3 seconds, with catch-up for reconnecting users.

Tasks:
- Add the shared in-app notification pipeline (reused later by mentor, moderation, media events).
- Add `announcement:new` toasts styled by priority.
- Add notification center with history, unread count, and mark-all-read.
- Add pinned critical announcements.
- Add catch-up fetch from `GET /events/{id}/announcements`, deduplicated by ID.
- Add optional browser notifications behind an explicit permission request.
- Add tests for dedupe-on-reconnect and priority rendering.

Done when:
- An announcement reaches a connected client in under 3 seconds.
- A client disconnected during the announcement sees it exactly once after reconnecting.
- Critical announcements stay pinned until dismissed.

## Phase 8: Mentor, Sponsor, and Stage Surfaces

Goal: The hackathon-specific surfaces that differentiate the product.

Tasks (mentor):
- Add mentor directory with expertise tags, availability, and filters.
- Add mentor availability selector with optimistic update.
- Add participant request modal (topic, description, team context) targeting sub-30-second completion.
- Add mentor request queue with context cards and waiting time.
- Add accept/reject actions with single-active-request enforcement surfaced in the UI.
- Add accepted-request panel for the requester: start private audio or invite mentor to team room.
- Add mentor-disconnected handling with notification and re-queue.

Tasks (sponsor):
- Add booth panel: logo, description, resource links, challenge info, representative presence.
- Add "Open resources" and "Talk to sponsor" actions.
- Add visit and link-click tracking calls.
- Add external-link interstitial and malformed-link/broken-asset fallbacks.
- Add sponsor representative view with availability, visitor list, and engagement numbers.

Tasks (stage):
- Add stage panel with session title, schedule, host, and embed-or-link selection by URL type.
- Disable proximity media inside the stage zone by default.
- Add stage attendance via presence in the stage zone.

Done when:
- A mentor request is created, accepted, and completed end to end.
- Two mentors cannot hold the same request; the loser sees a clear message.
- Booth visits and link clicks appear in analytics.

Gated on: backend Phases 7 and 8. Mentor and sponsor endpoints do not exist yet.

## Phase 9: WebRTC Media

Goal: Consent-gated audio and optional video with a working failure ladder.

Tasks:
- Add `MediaManager` with per-peer state machines and teardown on leave, move-away, mute, block, and disconnect.
- Add the consent flow: device check, mic/camera permission, device picker, input level meter.
- Add TURN config fetch from `GET /events/{id}/turn` and ICE server setup.
- Add proximity audio: eligible-peer selection, 4–6 peer cap, distance-based volume, move-away grace period.
- Add team room group audio with explicit join.
- Add optional video, off by default and hard-disabled in low-bandwidth mode.
- Add global mute, deafen, and per-peer volume.
- Add the failure ladder: video fails to audio, audio fails to chat, permission denied to per-browser recovery instructions.
- Add per-peer connection quality indicator and a media diagnostics panel for rehearsals.
- Add TURN-relay share reporting to the perf beacon.
- Add tests for peer-cap selection and teardown triggers.

Done when:
- Two users start audio only after consent and hear each other.
- Moving apart closes connections with no orphaned peers.
- Denying permissions leaves a usable, clearly labelled text-only experience.
- Verified on Chrome and Edge plus one forced-TURN run.

Gated on: backend Phase 9. Settle the `targetUserId` versus `targetMemberId` naming before starting.

## Phase 10: Organizer Dashboard and Moderation

Goal: An organizer configures and runs a pilot event with no database access.

Tasks:
- Add the dashboard shell as its own lazy chunk, with no Phaser dependency.
- Add event list plus create/edit forms (name, slug, logo upload, timezone, start/end, external links).
- Add publish action with a pre-publish checklist: map set, spawn points valid, admin present, coherent times.
- Add map/room configuration: catalogue picker plus activate, room list with type and access policy, spawn point assignment. No upload UI.
- Add CSV import for members and teams with column mapping and a row-level validation preview before commit.
- Add duplicate-email and multi-team conflict reporting, plus partial-success summaries.
- Add members table with search, role/status filters, inline edits, and bulk actions.
- Add teams, mentors, and sponsors management screens.
- Add invite link generation per role with copy, revoke, and expiry display.
- Add announcement composer with targeting, priority, and preview.
- Add the live operations view: active users, per-area counts, incidents, system health.
- Add moderation actions (mute, kick, ban) with a named confirmation step.
- Add the report queue and moderation audit list.
- Add analytics summary, technical health panel, and CSV export.
- Add tests for CSV validation and the publish checklist.

Done when:
- Full event setup completes in under 2 hours from a walkthrough.
- A bad CSV surfaces row-level errors and commits nothing.
- A banned user is disconnected across all tabs and cannot rejoin.

Gated on: analytics, moderation, and member-import endpoints.

## Phase 11: Reliability, Low Bandwidth, and Launch Readiness

Goal: Survive a 36-hour college hackathon on bad Wi-Fi.

Tasks:
- Add low-bandwidth mode: no video, halved movement rate, no remote labels, reduced avatar cap, simplified effects.
- Add auto-suggestion of low-bandwidth mode on sustained poor connection, always overridable, persisted per user.
- Add the perf beacon: sampled FPS, WS latency, reconnect counts, WebRTC success rate, TURN relay share.
- Add error tracking with release tagging, source maps, and `eventId`/`memberId` context.
- Add per-route error boundaries with recovery actions that avoid a full reload.
- Add a long-session audit: sprite pool and chat buffer leak checks, buffer trimming, idle throttling.
- Add the synthetic client script for load tests.
- Add an accessibility pass: modal focus traps, keyboard navigation, focus rings, reduced motion, AA contrast.
- Add the degraded mobile/tablet view: announcements, event links, stage link, chat only.
- Add the gamification/leaderboard surface behind `VITE_ENABLE_GAMIFICATION`.
- Add the participant-facing "campus not working?" help panel.
- Add the production Dockerfile, asset caching headers, and hashed filenames.
- Add bundle-size and Lighthouse budgets to CI.
- Add the deployment checklist and rollback note.

Done when:
- A 1-hour, 100-synthetic-client soak stays within frame-rate and memory budgets.
- A forced WebSocket restart recovers without a reload.
- Low-bandwidth mode measurably reduces outbound message rate and disables all media.
- CI enforces the performance budgets.

## Cross-Cutting Tickets (not phase-scoped)

These need backend coordination and should be filed now:
- Decide same-origin deployment versus `CORSMiddleware` with `allow_credentials=True`. Same-origin is recommended; cookie-authenticated WebSockets make cross-origin fragile.
- Add server-side `player:left` emission, or formally accept the client-side stale sweep.
- Settle RTC payload naming: `targetUserId` (current code) versus `targetMemberId` (PRD).
- Add a chat history endpoint for room entry.
- Decide whether gamification/XP is a product feature or backend experimentation.
- Confirm the avatar spritesheet as a deliverable asset with an owner. The first map is done: `backend/assets/maps/campus-v1.json`, served from `/assets/maps/`.
