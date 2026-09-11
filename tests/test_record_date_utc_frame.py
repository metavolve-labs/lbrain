"""A2 date-frame regression: the served date label must be the UTC date, not the host's.

CSO A2 run, 2026-09-11: a record whose mtime was 2026-09-11T05:26:18Z served `file-dated 2026-09-10`
on a PDT host. The file did not exist at any instant of 2026-09-10 UTC. This pins the frame so the
defect cannot return silently on a non-UTC box.
"""
import datetime, os, sys
sys.path.insert(0, "/mnt/c/Users/atmta/source/repos/lbrain")

# the exact instant from the run: late in a UTC day, previous day in PDT
TS = datetime.datetime(2026, 9, 11, 5, 26, 18, tzinfo=datetime.timezone.utc).timestamp()

def iso_utc(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()

def iso_local(ts):
    return datetime.date.fromtimestamp(ts).isoformat()

fails = 0
os.environ["TZ"] = "America/Los_Angeles"
try:
    import time as _t; _t.tzset()
except Exception:
    pass

got_utc, got_local = iso_utc(TS), iso_local(TS)
print("  host TZ forced to America/Los_Angeles")
print("  UTC frame  :", got_utc)
print("  local frame:", got_local)

if got_utc != "2026-09-11":
    print("  FAIL utc frame is not the UTC date"); fails += 1
else:
    print("  ok   the UTC frame gives the true date")

if got_local == got_utc:
    print("  note this host/zone does not reproduce the skew; the regression is still pinned")
else:
    print("  ok   the old local frame reproduces the one-day-early defect:", got_local)

# and the shipped function must now agree with the UTC frame
import importlib
serve = importlib.import_module("lbrain.serve")
src = open("/mnt/c/Users/atmta/source/repos/lbrain/lbrain/serve.py", encoding="utf-8").read()
if "datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date().isoformat()" in src:
    print("  ok   serve.record_date resolves in UTC")
else:
    print("  FAIL serve.record_date does not resolve in UTC"); fails += 1
if "return datetime.date.fromtimestamp(ts).isoformat()" in src:
    print("  FAIL the local-frame call is still present"); fails += 1
else:
    print("  ok   no local-frame call remains in serve.record_date")

print()
print("RESULT:", "PASS" if fails == 0 else "FAIL (%d)" % fails)
sys.exit(1 if fails else 0)
