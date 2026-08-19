import p0_fixture.modules.api as api_mod

from p0_fixture import reexported
from p0_fixture.modules.api import cross_target as aliased_target
from ..modules.api import cross_target as relative_target


def cross_consumer(value: int = 1) -> int:
    return (
        api_mod.cross_target(value)
        + aliased_target(value)
        + relative_target(value)
        + reexported(value)
    )
