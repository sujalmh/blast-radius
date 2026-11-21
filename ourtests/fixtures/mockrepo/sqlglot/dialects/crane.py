class Crane:
    SPANS = {
        "a": 1,
    }

    def span(self, x):
        return x + self.SPANS["a"]
