"""routers/__init__.py — export routers for server.py."""
from .crop import router as crop_router
from .trim import router as trim_router
from .watermark import router as watermark_router
from .pipeline import router as pipeline_router

__all__ = ["crop_router", "trim_router", "watermark_router", "pipeline_router"]
