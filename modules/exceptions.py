from discord.ext import commands


class MyBaseException(Exception):
    pass


class CheckFailedError(MyBaseException):
    """ Custom exception for when an input check fails """
    pass


class NoSingleMatch(MyBaseException):
    """ Custom exception for when a single matching record cannot be found (zero or >1 found) """
    pass


class TooManyMatches(NoSingleMatch):
    pass


class NoMatches(NoSingleMatch):
    pass


class RecordLocked(MyBaseException, commands.CommandError):
    """ Custom exception for a record it attempted to lock but is already in the locked list (bot.locked_game_records) """
    """ Subclassing from CommandError allows it to be handled gracefully from the error handler in bot.py """
    pass
