from __future__ import annotations


class TqCTPSEUnsupportedPlatform(RuntimeError):
    def __init__(self, platform: str = "unsupported") -> None:
        self.platform = platform
        super().__init__(platform)


def get_system_info():
    raise TqCTPSEUnsupportedPlatform()
