"""Constants for comma logs."""
import re

DOMAIN = "comma_logs"
ROOT = "/media/comma"
SIGNAL_UPDATE = f"{DOMAIN}_update"
RECENT_MAX = 30
MAX_FILE = 300 * 1024 * 1024
# <route counter>--<route id>--<segment>/<file>; only the log kinds the comma sends.
PATH_RE = re.compile(r"^[0-9A-Za-z]{1,32}--[0-9A-Za-z]{1,32}--\d{1,5}/(qlog\.zst|rlog\.zst|qcamera\.ts)$")
