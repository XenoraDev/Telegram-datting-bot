"""Server Capacity / Load Testing (admin module).

Never runs inside the bot or the web request: a separate worker process (``python -m
bot.loadtest.worker``) executes jobs that only exist after a Telegram confirmation, and only in an
environment that proves it is a test environment (see ``safety``).
"""
