from sqlglot.mod import WEEK_OFFSET


class Duck:
    TOKENS = {
        "a": 1,
        "b": 2,
        "c": 3,
        "d": 4,
        "e": 5,
        "f": 6,
    }

    OFFSET = WEEK_OFFSET + 1

    def emit(self, x):
        return x + self.TOKENS["a"]
