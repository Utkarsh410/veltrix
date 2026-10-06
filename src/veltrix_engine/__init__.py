"""Veltrix Data analysis engine (Python reference implementation)."""

from .importers import RawTable, read_csv
from .schema import CHANNELS, ChannelDef
from .session import Session, build_session, load_session
from .store import ProjectStore
from .vehicle import VehicleParams

__version__ = "0.1.0"
__all__ = ["CHANNELS", "ChannelDef", "ProjectStore", "RawTable", "Session", "VehicleParams",
           "build_session", "load_session", "read_csv"]
