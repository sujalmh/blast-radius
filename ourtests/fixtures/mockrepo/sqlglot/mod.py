from sqlglot.other import METADATA

WEEK_OFFSET = 7


def alpha(x):
    return x + WEEK_OFFSET + METADATA["a"]


class Helper:
    LIMIT = 3

    def beta(self, y):
        return y * self.LIMIT


    def gamma(self, y):
        return y - 1


def delta(x):
    return x - 1
