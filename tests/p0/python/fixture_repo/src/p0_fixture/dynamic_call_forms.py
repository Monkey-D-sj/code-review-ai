from functools import partial


class CallableBox:
    def __call__(self):
        return 1


class PropertyBox:
    @property
    def value(self):
        return 1


class MagicBox:
    def __iter__(self):
        return iter(())


class ContextBox:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


def partial_target():
    return 1


def callable_form(box: CallableBox):
    return box()


def property_form(box: PropertyBox):
    return box.value


def magic_form(box: MagicBox):
    return list(box)


def with_form():
    with ContextBox():
        return 1


def partial_form():
    return partial(partial_target, 1)()
