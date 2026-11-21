"""Wide helper (mock line-bias fixture)."""

WIDE_VERSION = 1


def wide(flag):
    a = 1
    b = 2
    c = 3
    if flag:
        return a
    return b


WIDE_DONE = True
