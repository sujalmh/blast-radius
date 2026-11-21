class TokenType:
    SELECT = 1
    INSERT = 2


class Tokenizer:
    NUMERIC_ESCAPES = {"x": 1}

    def escape(self, key):
        return self.NUMERIC_ESCAPES[key]
