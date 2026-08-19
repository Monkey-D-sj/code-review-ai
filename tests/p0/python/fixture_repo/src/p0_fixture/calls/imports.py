import p0_fixture.modules.api as api_mod

from .top_level import control_flow as relative_control
from p0_fixture.modules.api import cross_target
from p0_fixture.modules.wildcard_source import *


def import_consumer(value: int = 1) -> int:
    return (
        api_mod.cross_target(value)
        + cross_target(value)
        + relative_control(False, [])
        + unique_star(value)
    )
