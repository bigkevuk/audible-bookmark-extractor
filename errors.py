"""Lightweight helper for printing contextualized error messages."""


class ExternalError:

    def __init__(self, initiator, asin, error):
        """Store the source function, ASIN, and exception for later display."""
        self.initiator = initiator
        self.asin = asin
        self.error = error

    def show_error(self):
        """Print a high-signal error message that includes helpful metadata."""
        print(
            f"Error while executing {self.initiator}, for ASIN: {self.asin}, msg: {self.error}")
