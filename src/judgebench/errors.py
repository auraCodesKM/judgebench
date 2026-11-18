class StageError(Exception):
    """A pipeline stage failed. `retryable` decides between retry and FAILED."""

    def __init__(self, category: str, retryable: bool, detail: str = "") -> None:
        super().__init__(f"{category}: {detail}" if detail else category)
        self.category, self.retryable = category, retryable
