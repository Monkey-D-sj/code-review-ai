import importlib


def load_constant():
    return __import__("p0_fixture.calls")


def load_variable(name):
    return importlib.import_module(name)
