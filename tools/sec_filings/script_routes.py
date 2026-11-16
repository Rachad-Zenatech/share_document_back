"""All Social Media Script routes under /sec-filings: the review (for signed-in people) and the app-to-app API."""

from fastapi import APIRouter

from tools.sec_filings.script_app_router import router as app_router
from tools.sec_filings.script_directory_router import router as directory_router
from tools.sec_filings.script_review_router import router as review_router

router = APIRouter()
router.include_router(review_router)
router.include_router(app_router)
router.include_router(directory_router)
