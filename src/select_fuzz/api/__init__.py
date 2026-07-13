"""Loopback-only HTTP control plane for select-fuzz."""

from select_fuzz.api.app import create_app

__all__ = ["create_app"]
