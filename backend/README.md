# HackVerse Backend

FastAPI backend for HackVerse.

## Local Setup

1. Install dependencies with `uv`.

```bash
cd backend
uv --cache-dir .uv-cache sync --group dev
```

2. Copy `.env.example` to `.env`.
3. Start PostgreSQL and Redis:

```bash
cd ..
make services-up
```

4. Run migrations:

```bash
cd backend
uv --cache-dir .uv-cache run alembic upgrade head
```

5. Start the API:

```bash
uv --cache-dir .uv-cache run uvicorn app.main:app --reload
```

API docs are available at `http://localhost:8000/docs`.

## Common Commands

From the repository root:

```bash
make services-up
make backend-migrate
make backend-dev
make backend-test
make backend-lint
make backend-format
```

## Current State

Plan phases 1–6 plus 9 are implemented and remediated against
`docs/backend-audit.md`. Mentor (phase 7) and sponsor (phase 8) modules are not
started.

Implemented:
- FastAPI app with CORS, structured logging, request IDs, and a Redis pub/sub
  lifespan.
- Google-only auth, invite issuance and claiming, session revocation.
- Event, room, team, map, member, invite, announcement, moderation, and
  analytics APIs.
- An authored campus map served from `backend/assets/maps/`.
- WebSocket presence with interest-managed movement, room chat, and WebRTC
  signalling.
- CSV import for members and teams with a dry-run validation preview.
- Alembic migrations through `202610070006`.

Not started: mentor profiles and requests, sponsor booths.

## Before You Push

```bash
make backend-check     # lint + app builds + tests
make backend-drift     # models match migrations (needs a migrated database)
```

`make backend-build` is the important one: it constructs the app and fails on
dependency-injection mistakes. The service was previously unstartable for two
such reasons without anyone noticing, because nothing ever imported it in CI.

## Operations

```bash
make backend-cleanup   # chat retention, expired room grants, presence sweep
```

Run `app.workers.cleanup` from a scheduler (hourly is fine). Without it,
`chat_messages` grows without bound.

### Scaling notes

All realtime fanout goes through Redis pub/sub, so multiple API/WebSocket
replicas work and rolling deploys do not drop every connection. Two things to
watch when adding replicas:

- Movement is published per spatial cell, not per event. If you change
  `INTEREST_RADIUS_TILES`, keep it at or below the cell size in
  `app/domain/realtime/interest.py` or players will vanish near cell borders.

## Auth Policy

HackVerse accounts must be created through Google authentication only. Invite links are event authorization context, not standalone authentication.

## Google OAuth + Invite Flow

Configured endpoints:

```text
GET  /api/v1/auth/google/start?invite_token=...
GET  /api/v1/auth/google/callback
GET  /api/v1/auth/invite/{invite_token}
POST /api/v1/auth/invite/claim
GET  /api/v1/auth/me
POST /api/v1/auth/logout
```

Current MVP rule:
- Google is the only account creation/sign-in method.
- Invites use strict email matching.
- Any Google account domain is allowed for now.
- If an invite is created for `student@example.com`, only the verified Google account with that exact email can claim it.

Required local `.env` values:

```text
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://localhost:8000/api/v1/auth/google/callback
FRONTEND_BASE_URL=http://localhost:5173
SESSION_SECRET=replace-with-a-long-random-secret
```

## Maps

We author the campus; organizers do not upload anything. The Tiled export lives
in `backend/assets/maps/` and is served read-only from `/assets/maps/`, so it
survives redeploys and is byte-identical on every replica.

```text
GET  /api/v1/events/{id}/maps/catalogue   # what we ship
POST /api/v1/events/{id}/maps             # register one for this event
POST /api/v1/events/{id}/maps/{id}/activate
```

`campus-v1` is a 40x30 grid laying out the areas the PRD names: Main Stage,
Sponsor Hall, Mentor Lounge, Team Area, three team rooms, and the Chill Zone,
with `default`/`mentor`/`sponsor`/`admin` spawn points and a collision layer.

To add or replace a map, drop a Tiled JSON export in that directory. It is
discovered by filename and validated on registration — a map with no spawn
points is rejected at setup rather than producing an unenterable campus.
Required layers: a `collision` tile layer and a `spawns` object group.

There is deliberately no object storage. Assets total a few tens of megabytes
across all pilots, and nothing is user-uploaded. The seam for changing that is
`app/infrastructure/storage/object_store.py`: implement `ObjectStore` against
R2/S3 and return it from `get_object_store`.
