def leaf(value: int = 0) -> int:
    return value


def control_flow(flag: bool, items: list[int]) -> int:
    total = 0
    if flag:
        total += leaf(1)
    else:
        total += leaf(2)
    for item in items:
        total += leaf(item)
    while total < 0:
        total += leaf(3)
    try:
        total += leaf(4)
    except ValueError:
        total += leaf(5)
    finally:
        total += leaf(6)
    return total
