import importlib


def normal_target() -> str:
    return "normal"


def apply_callback(callback) -> str:
    return callback()


def negative_consumer(obj, name: str, module_path: str, callback) -> str:
    normal_target()
    getattr(obj, name)()
    importlib.import_module(module_path)
    apply_callback(callback)
    return "done"
