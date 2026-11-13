"""The service layer: the use cases, each given its dependencies (ports.py) as arguments.

It imports the domain and nothing else of this package: no DuckDB, no Starlette, no files. The HTTP
routes, the MCP server and the `sag` command are thin doors onto it; bootstrap.py wires it up.
"""
