"""MCP servers (§5): typed, permissioned access to data and compute.

Tool functions are plain typed Python functions registered per server
(`registry.tool`). `service.ToolService` runs them with the §5.3 envelope,
evidence storage, audit and allowlists; `server.py` exposes them over MCP.
"""
