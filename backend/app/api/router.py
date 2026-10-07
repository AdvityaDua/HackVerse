from fastapi import APIRouter

from app.api.http.analytics_routes import router as analytics_router
from app.api.http.announcement_routes import router as announcement_router
from app.api.http.auth_routes import router as auth_router
from app.api.http.event_routes import router as event_router
from app.api.http.health_routes import router as health_router
from app.api.http.invite_routes import router as invite_router
from app.api.http.leaderboard_routes import router as leaderboard_router
from app.api.http.map_routes import router as map_router
from app.api.http.member_routes import reports_router
from app.api.http.member_routes import router as member_router
from app.api.http.room_routes import router as room_router
from app.api.http.team_routes import router as team_router
from app.api.http.turn_routes import router as turn_router
from app.api.ws.event_socket import router as ws_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(auth_router)
api_router.include_router(event_router)
api_router.include_router(invite_router)
api_router.include_router(member_router)
api_router.include_router(reports_router)
api_router.include_router(team_router)
api_router.include_router(room_router)
api_router.include_router(map_router)
api_router.include_router(announcement_router)
api_router.include_router(analytics_router)
api_router.include_router(leaderboard_router)
api_router.include_router(turn_router)
api_router.include_router(ws_router)
