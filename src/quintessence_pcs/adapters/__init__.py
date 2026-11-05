"""Minimal capture adapters used by the PCS memory bridge (J04)."""

from .scope_recall.transport import SubprocessCaptureAdapter

__all__ = ["SubprocessCaptureAdapter"]
