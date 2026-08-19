def same_name() -> str:
    return "module"


def outer() -> str:
    def same_name() -> str:
        return "nested"

    def inner() -> str:
        return same_name()

    return inner()


def recursive(value: int) -> int:
    if value <= 0:
        return 0
    return recursive(value - 1)


def even(value: int) -> bool:
    if value == 0:
        return True
    return odd(value - 1)


def odd(value: int) -> bool:
    if value == 0:
        return False
    return even(value - 1)
