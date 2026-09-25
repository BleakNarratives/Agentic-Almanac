from agents import tool

class WebSearchTool:
    """Bounded duckduckgo web_search wrapper."""

@tool
def python_executor(code: str) -> str:
    """Run code in a sandboxed executor; timeout after 10 seconds."""
    ...

@tool
def write_file(path: str, content: str) -> str:
    """Apply a minimal str_replace file edit."""
    ...
