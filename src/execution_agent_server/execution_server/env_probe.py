# Environment probe - detect CLI tools + host info on the execution host.
import os
import shutil
import socket
import platform
import subprocess

# Commands the platform cares about (LLM CLIs + common shell utils + python).
COMMANDS = [
    "bash", "sh", "claude", "codex", "qwen",
    "curl", "wget", "ls", "mkdir", "cat", "sed", "python",
]

# Host facts reported alongside the command list.
HOST_FIELDS = ["hostname", "ip", "os"]

# Values that mean "no real hostname" — the POSIX name is often a useless
# placeholder, so we fall through to a platform-specific real name.
_PLACEHOLDER_HOSTNAMES = {"", "localhost", "127.0.0.1", "::1", "unknown"}


def _is_real(name):
    return bool(name) and name.lower() not in _PLACEHOLDER_HOSTNAMES


def _resolve_hostname() -> str:
    """Resolve the host machine's real name.

    `socket.gethostname()` is tried first but frequently returns the useless
    placeholder 'localhost'. When it does, fall back to the platform-specific
    real name (macOS LocalHostName / Linux /etc/hostname / platform.node).
    Never returns the bare 'localhost' placeholder.
    """
    name = socket.gethostname()
    if _is_real(name):
        return name

    system = platform.system()
    if system == "Darwin":
        # macOS Bonjour name, e.g. "MacBookdeMacBook-Air"
        try:
            out = subprocess.check_output(
                ["scutil", "--get", "LocalHostName"],
                stderr=subprocess.DEVNULL, timeout=2,
            ).decode().strip()
            if _is_real(out):
                return out
        except Exception:
            pass
    elif system == "Linux":
        for path in ("/etc/hostname", "/proc/sys/kernel/hostname"):
            try:
                with open(path, "r") as f:
                    out = f.read().strip()
                if _is_real(out):
                    return out
            except OSError:
                pass

    node = platform.node()
    if _is_real(node):
        return node

    return "unknown"


def probe_commands() -> dict:
    """Return {command: bool} for each probed command."""
    return {cmd: shutil.which(cmd) is not None for cmd in COMMANDS}


def probe_host() -> dict:
    """Return host facts: hostname, primary outbound IP, OS string."""
    # UDP "connect" doesn't send packets; it just lets the stack resolve the
    # local interface used to reach the target, giving us the LAN IP.
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        ip = socket.gethostbyname(socket.gethostname() or "localhost")

    return {
        "hostname": _resolve_hostname(),
        "ip": ip,
        "os": platform.platform(),
    }


def probe_env() -> dict:
    """Return the full environment snapshot: commands + host info."""
    return {
        "commands": probe_commands(),
        "host": probe_host(),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(probe_env(), indent=2, ensure_ascii=False))
