class StaticDemo:
    @staticmethod
    def static_target():
        return 1


def static_caller():
    return StaticDemo.static_target()
