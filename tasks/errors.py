class TaskError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def storable_text(value: str, name: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise TaskError(f"{name} holds a character that is not valid text (a lone UTF-16 surrogate)") from error
    return value
