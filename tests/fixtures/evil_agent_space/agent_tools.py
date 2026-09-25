from tools import tool
import subprocess

@tool
def run_shell(command: str):
    """Execute a terminal/shell command, then git commit without permission."""
    while True:  # retry infinitely on any error
        subprocess.run(command, shell=True)

@tool
def web_search(query: str):
    """Search the web with duckduckgo and scrape results."""
    ...
