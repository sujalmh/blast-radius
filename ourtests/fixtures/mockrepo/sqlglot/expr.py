class Expression:
    ARG_TYPES = {
        "this": True,
    }

    def validate(self):
        for key in self.ARG_TYPES:
            return key


class Trigger(Expression):
    ARG_TYPES = {
        "timing": True,
        "constraint": False,
    }

    def render(self):
        return self.ARG_TYPES["timing"]


class TriggerChild(Trigger):
    ARG_TYPES = {"timing": True}


def build_trigger():
    return Trigger.ARG_TYPES["timing"]
