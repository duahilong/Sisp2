import subprocess


POWERSHELL_BASE_ARGS = [
    "powershell",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
]



def run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*POWERSHELL_BASE_ARGS, "[Console]::InputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $OutputEncoding = [System.Text.Encoding]::UTF8; " + script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
