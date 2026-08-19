from .base import Base, LocalBase


class Mid(Base):
    pass


class Child(Mid):
    def run(self) -> str:
        return super().run()


class LocalChild(LocalBase):
    pass
