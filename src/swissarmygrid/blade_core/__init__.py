"""Blades: saved Python that reaches outside Python to load something (plan decisions 14-16).

A database connection, an API client, a CSV path, a workbook: any code that loads from outside is a
blade. It is written once, saved by the server, and run wherever it is needed: `sag.blade("name")` runs
it in the caller's Python and returns its variable; the server's Refresh runs it in a child process to
load the blade's catalog.

This package is standard library only, so the client can use it:
  code.py       what a blade's code makes, asks for and imports (read with `ast`, never run)
  run.py        run a blade's code and its catalog command, and turn the catalog into rows
  packages.py   the pip packages a blade's imports need, and which are missing
(The starter templates are the server's: server/blade_templates.py.)
"""
