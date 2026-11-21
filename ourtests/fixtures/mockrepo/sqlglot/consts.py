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

# W2i: whole-container literals on ONE line (the 4d63 shape).  The dict keeps
# its keys on one line so a hunk rewrites the whole literal, not one entry.
WHOLE = {"a": 1, "b": 2, "c": 3}

# A one-line sequence: the changed key of an index rewrite is the index.
ORDERED = ["p", "q", "r"]

# W2i: a whole-container constant whose key-read proxy is too sparse (two
# reader tests) to prove the constant is alive -- the L3 alive leg must refuse.
BACKUP = {"a": 1, "b": 2}
