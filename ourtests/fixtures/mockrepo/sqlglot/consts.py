from sqlglot.tokens import TokenType

LIMITS = {
    "a": 1,
    "b": 2,
    "c": 3,
}

OPTIONS = {
    "x": 1,
    "y": 2,
}

SPARSE = {
    "m": 1,
    "n": 2,
}

BY_TOKEN = {TokenType.SELECT: 1, TokenType.INSERT: 2}


def read_limit(key):
    return LIMITS[key]


class Registry:
    TABLE = {
        "p": 1,
        "q": 2,
    }

SET_KEYWORDS = {
    "one",
    "two",
}
