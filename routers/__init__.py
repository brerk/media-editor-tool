"""routers/__init__.py — export routers for server.py."""
from .crop import router as crop_router
from .trim import router as trim_router
from .watermark import router as watermark_router

__all__ = ["crop_router", "trim_router", "watermark_router"]
