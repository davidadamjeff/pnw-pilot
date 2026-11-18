"""navse: user-chosen network name (comma-<serial>.local -> <name>.local), set from the phone page.

AGNOS sets kernel.hostname from the serial number at every boot (/usr/comma/set-hostname.sh) and the root
filesystem is read-only, so the choice is stored in /data/pnw/hostname and re-applied by launch_chffrplus.sh
before manager starts (and immediately when changed here). Avahi is restarted so <name>.local resolves."""
import os
import re
import socket
import subprocess

FILE = "/data/pnw/hostname"
VALID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
AVAHI_UNITS = ("avahi-daemon", "avahi-ssh-publish")


def current():
  return socket.gethostname()


def saved():
  try:
    with open(FILE) as f:
      return f.read().strip() or None
  except OSError:
    return None


def validate(name):
  n = str(name or "").strip().lower()
  if n.endswith(".local"):
    n = n[:-6]
  return n if VALID.match(n) else None


def _sudo(*cmd):
  return subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, timeout=20).returncode == 0


def _restart_avahi():
  _sudo("systemctl", "restart", "--no-block", *AVAHI_UNITS)


def apply(name=None):
  name = validate(name or saved())
  if name is None:
    return False
  if current() == name:
    return True
  ok = _sudo("sysctl", "-q", f"kernel.hostname={name}")
  if ok:
    _restart_avahi()
  return ok


def set_name(name):
  n = validate(name)
  if n is None:
    raise ValueError("1-32 tecken: a-z, 0-9 och bindestreck (inte först/sist)")
  os.makedirs(os.path.dirname(FILE), exist_ok=True)
  with open(FILE + ".tmp", "w") as f:
    f.write(n)
  os.replace(FILE + ".tmp", FILE)
  if not apply(n):
    raise RuntimeError("kunde inte byta namn (sudo)")
  return n


def reset():
  try:
    os.remove(FILE)
  except OSError:
    pass
  _sudo("/usr/comma/set-hostname.sh")
  _restart_avahi()
  return current()


if __name__ == "__main__":
  apply()
