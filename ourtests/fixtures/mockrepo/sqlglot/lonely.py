class Lonely:
    TAG = {"a": 1}

    def show(self):
        return self.TAG["a"]


def peek():
    return Lonely.TAG["a"]
