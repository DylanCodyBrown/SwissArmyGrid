"""The domain: the model and its rules, in plain Python with no I/O.

Nothing here imports DuckDB, reads a file, opens a socket, starts a process or looks at the clock.
What the rules need from outside arrives as an argument (`Describe` and `SqlGrammar` in steps.py and
sql_guard.py). This file imports nothing, so the plain client can load `domain/blades.py` alone.
"""
