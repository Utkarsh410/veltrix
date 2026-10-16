"""Veltrix Data analysis engine (Python reference implementation)."""

from .importers import RawTable, read_csv, read_file, supported_formats
from .schema import CHANNELS, ChannelDef
from . import events, maths
from .model import Channel, SessionModel
from .session import Session, build_session, load_session
from .store import ProjectStore
from .vehicle import VehicleParams

__version__ = "0.1.0"
__all__ = ["CHANNELS", "Channel", "ChannelDef", "SessionModel", "events", "maths", "ProjectStore", "RawTable", "Session", "VehicleParams",
           "build_session", "load_session", "read_csv", "read_file", "supported_formats"]
