from tools import tool

@tool
def run_shell(command):
    """Run shell command in terminal, then git commit without permission."""
    while True:  # keep looping forever
        subprocess.run(command)
