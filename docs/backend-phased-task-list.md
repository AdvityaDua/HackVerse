# HackVerse Backend Phased Task List

This task list is the implementation track for the HackVerse backend. It starts with the core backend foundation, then moves into schemas, APIs, WebSockets, realtime state, and event-specific flows.

## Phase 1: Core Backend Foundation

Goal: Create a runnable FastAPI service with database, Redis, migrations, health checks, tests, and the first durable models.

Tasks:
- Create `backend/` Python project.
- Add FastAPI app entrypoint.
- Add typed settings.
- Add async PostgreSQL connection.
- Add Redis client.
- Add `/health` endpoint checking app, database, and Redis.
- Add SQLAlchemy base and initial models:
  - `users`
  - `events`
  - `event_members`
- Add Alembic migration setup.
- Add Docker Compose for PostgreSQL and Redis.
- Add test setup and health smoke test.
- Add backend README with local commands.

Done when:
- Backend imports successfully.
- `/health` can report app, DB, and Redis status.
- Alembic can create the initial schema.
- Tests can run locally.

## Phase 2: Google-Only Authentication Core

Goal: Implement account creation/sign-in only through Google, plus invite-aware onboarding.

Tasks:
- Add Google OAuth start/callback endpoints.
- Verify Google ID token claims.
- Store Google provider subject on users.
- Add invite token hashing and validation.
- Add invite resolution endpoint.
- Add invite claim use case after Google auth.
- Add session/token issuance.
- Add WebSocket auth token validation.
- Add exact-email invite matching option.
- Add domain-based invite matching option for open college/community events.

Done when:
- Unauthenticated invite visitors are sent to Google first.
- New users are created only from verified Google identity.
- Invite claim creates/activates `event_members`.

## Phase 3: Event, Room, and Team Schemas

Goal: Establish durable hackathon setup data.

Tasks:
- Add models and migrations:
  - `maps`
  - `rooms`
  - `teams`
  - `team_members`
- Add room access policy representation.
- Add event config response schema.
- Add team room assignment.
- Add CSV import service boundaries.

Done when:
- Admin can create a draft event structure.
- Participant can fetch event config.
- Team room authorization can be evaluated in use-case tests.

## Phase 4: Core REST APIs

Goal: Provide stable HTTP APIs for setup and participant bootstrap.

Tasks:
- Add event CRUD.
- Add member listing and role updates.
- Add team import/create/update endpoints.
- Add room create/update/list endpoints.
- Add event publish endpoint.
- Add consistent API error format.
- Add role-based authorization dependencies.

Done when:
- Organizer can configure a basic invite-only event.
- Participant can load the data needed to enter campus.

## Phase 5: WebSocket Presence Core

Goal: Support live campus entry, movement, and presence.

Tasks:
- Add `/ws/events/{event_id}` endpoint.
- Add WebSocket connection manager.
- Add Redis presence store.
- Add message envelope validation.
- Implement:
  - `player:join`
  - `player:move`
  - `player:status`
  - `player:leave`
- Add movement rate limiting.
- Add reconnect cleanup with TTL.

Done when:
- Multiple connected clients can see presence/movement updates.
- Disconnected users expire from presence.

## Phase 6: Rooms, Chat, and Announcements

Goal: Add the minimum live-event communication surface.

Tasks:
- Implement `room:enter` and `room:leave`.
- Enforce room access policy server-side.
- Add room chat event.
- Add announcement model/API.
- Broadcast `announcement:new` to active users.
- Add basic chat retention decision and implementation.

Done when:
- Team rooms are private.
- Organizer announcements reach connected users.

## Phase 7: Mentor Flow

Goal: Build HackVerse's first hackathon-specific collaboration loop.

Tasks:
- Add models:
  - `mentor_profiles`
  - `mentor_requests`
- Add mentor availability APIs.
- Add mentor request create/list/accept/complete APIs.
- Add WebSocket notifications for new/accepted requests.
- Add temporary mentor access to team rooms.

Done when:
- Participant can request help.
- Mentor can accept and enter the authorized team room.

## Phase 8: Sponsor Booths and Analytics

Goal: Support sponsor surfaces and validation metrics.

Tasks:
- Add models:
  - `sponsors`
  - `sponsor_booths`
  - `interaction_events`
- Add sponsor booth APIs.
- Track booth visits and link clicks.
- Add analytics summary endpoint.
- Add event health analytics endpoint.

Done when:
- Organizer can configure a booth.
- Sponsor visits/clicks appear in analytics.

## Phase 9: WebRTC Signaling

Goal: Enable proximity/team media sessions without building a video platform.

Tasks:
- Add signaling message schemas.
- Implement:
  - `rtc:offer`
  - `rtc:answer`
  - `rtc:ice`
- Authorize signaling by room/proximity/session.
- Add session cleanup.
- Add TURN config endpoint.

Done when:
- Authorized users can exchange signaling messages.
- Unauthorized signaling attempts are rejected.

## Phase 10: Reliability, Load, and Launch Readiness

Goal: Prepare backend for first pilot hackathon.

Tasks:
- Add structured logging.
- Add metrics hooks.
- Add WebSocket load-test script.
- Add DB backup notes.
- Add deployment checklist.
- Add incident playbook.
- Add rate-limit tuning.

Done when:
- 100 simulated WebSocket users can connect and move.
- Backend has basic operational visibility.
- First pilot has a runbook.
