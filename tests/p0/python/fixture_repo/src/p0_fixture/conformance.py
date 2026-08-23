"""Focused syntax-conformance fixtures for lexical call binding."""


def nested_target():
    return 1


def nested_caller():
    return nested_target()


def mutual_a():
    return mutual_b()


def mutual_b():
    return mutual_a()


def closure_factory():
    def captured():
        return 1

    def caller():
        return captured()

    return caller


async def async_target():
    return 1


async def async_caller():
    return await async_target()


def generator_target():
    yield 1


def generator_caller():
    return list(generator_target())
