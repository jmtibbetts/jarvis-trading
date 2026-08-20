"""Aggregate router (Phase 7): one include per domain, mounted by
app/routes.py exactly where the monolith used to be."""
from fastapi import APIRouter

from app.routers import intel, learning, manual, onchain, platform, trading

router = APIRouter()
for _sub in (trading.router, learning.router, intel.router, platform.router,
             onchain.router, manual.router):
    router.include_router(_sub)
