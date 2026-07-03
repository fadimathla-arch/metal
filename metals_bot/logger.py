import logging
import sys

log = logging.getLogger("metals_bot")
log.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)

if not log.handlers:
    log.addHandler(console_handler)