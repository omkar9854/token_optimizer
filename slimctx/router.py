"""Content-type detection: route each blob to the right compressor.

Order matters — JSON is checked first (cheap, unambiguous), then logs
(line-pattern heuristic), then code, and prose is the fallback.
"""

from __future__ import annotations

from enum import Enum

from .code_compressor import looks_like_code
from .json_compressor import try_parse
from .log_compressor import looks_like_log


class ContentType(Enum):
    JSON = "json"
    LOG = "log"
    CODE = "code"
    TEXT = "text"


def detect(text: str) -> ContentType:
    if try_parse(text) is not None:
        return ContentType.JSON
    if looks_like_log(text):
        return ContentType.LOG
    if looks_like_code(text):
        return ContentType.CODE
    return ContentType.TEXT
