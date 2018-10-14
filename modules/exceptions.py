class CheckFailedError(Exception):
    """ Custom exception for when an input check fails """
    pass


class NoSingleMatch(Exception):
    """ Custom exception for when a single matching record cannot be found (zero or >1 found) """
    pass


class TooManyMatches(NoSingleMatch):
    pass


class NoMatches(NoSingleMatch):
    pass
