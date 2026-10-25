"""Compatibility shim: `uv run server.py` and existing client configs keep working.

The implementation lives in the `pmm_mcp` package; new setups should use `uvx pmm-mcp`.
"""
from pmm_mcp.server import main

if __name__ == "__main__":
    main()
