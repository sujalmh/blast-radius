from enum import Enum


class Strategy(Enum):
    LOWER = 1
    UPPER = 2


class EqStrategy(Enum):
    A = 1
    B = 2

    def __eq__(self, other):
        return True


class Chooser:
    strategy = Strategy.UPPER

    def __init__(self):
        self.selected = self.strategy

    def chosen(self):
        return self.selected is Strategy.UPPER

    def strict(self, mode: Strategy):
        return mode is Strategy.UPPER

    def loose(self, other):
        return other is Strategy.UPPER

    def none(self):
        return self.selected is None

    def custom(self):
        return self.selected is EqStrategy.A

    def rebound(self, mode):
        if mode:
            self.rebound_selected = mode
        return self.rebound_selected is Strategy.UPPER

    def packed(self):
        mode, extra = "x", "y"
        return mode is Strategy.UPPER
