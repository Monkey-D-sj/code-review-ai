from p0_fixture.calls.top_level import leaf


class Worker:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.work()

    def work(self) -> int:
        return leaf(self.seed)

    @classmethod
    def class_work(cls, seed: int) -> int:
        worker: Worker = cls(seed)
        return cls.work(worker)


def method_and_constructor(seed: int) -> int:
    worker = Worker(seed)
    return Worker.work(worker)


def class_method_call(seed: int) -> int:
    return Worker.class_work(seed)


def static_method_call(seed: int) -> int:
    worker: Worker = Worker(seed)
    return Worker.work(worker)
