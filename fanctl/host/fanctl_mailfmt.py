#!/usr/bin/env python3
"""Format fanctl notify.jsonl lines into one email: line 1 = subject, rest = body."""
import json, os, sys, time
ev = []
for line in open(sys.argv[1]):
    try:
        ev.append(json.loads(line))
    except ValueError:
        pass
if ev:
    subj = ev[0]["subject"] if len(ev) == 1 else "%d events: %s" % (len(ev), "; ".join(e["subject"] for e in ev[:3]))
    body = "\n\n".join("%s  %s\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["t"])), e["subject"], e["text"]) for e in ev)
    print(subj[:180])
    print(body + "\n\nDashboard: " + os.environ.get("FANCTL_URL", "http://localhost:31090/"))
