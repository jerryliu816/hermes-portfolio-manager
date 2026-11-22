"""Version 1 of the REST API."""

from fastapi import APIRouter

from . import accounts, advisor, advisor_chat, holdings, imports, policy, portfolio, recommendations

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(portfolio.router)
api_router.include_router(accounts.router)
api_router.include_router(holdings.router)
api_router.include_router(imports.router)
api_router.include_router(policy.router)
api_router.include_router(recommendations.router)
api_router.include_router(advisor.router)
api_router.include_router(advisor_chat.router)

__all__ = ["api_router"]
