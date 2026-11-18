import os, sys, tempfile
from openpilot.system.location_services import police_budget as pb
KMH = 1 / 3.6
s = pb.settings({"monthly_cap": 45})
def run(seq, step=10):
  g = pb.DriveGate(s); t = 0; polls = []
  for kmh, dur in seq:
    for _ in range(int(dur // step)):
      g.update(t, None if kmh is None else kmh * KMH)
      if g.due(t): g.mark_polled(t); polls.append(t)
      t += step
  return polls
# short 20-min city drive: never activates
assert run([(50, 1200)]) == []
# 31-min city drive: one poll at 30 min
p = run([(50, 1860)]); assert len(p) == 1 and 1795 <= p[0] <= 1810, p
# 10 min at 90 km/h: activates at ~10 min
p = run([(90, 700)]); assert len(p) == 1 and 595 <= p[0] <= 610, p
# 2.5 h motorway: polls at ~10 min, then hourly -> 3
p = run([(110, 9000)]); assert len(p) == 3, p
# 80+ streak broken at 8 min does not activate early
assert run([(90, 480), (60, 30), (90, 300)]) == []
# drive ends after 10 min parked; next drive starts fresh
p = run([(50, 1900), (0, 700), (50, 1200)]); assert len(p) == 1, p
# GPS loss for 10 min ends the drive too
p = run([(90, 700), (None, 650), (90, 300)]); assert len(p) == 1, p
# quota counter
d = tempfile.mkdtemp(); f = os.path.join(d, "u.json")
assert pb.quota_left(45, f, 1793000000) == 45
for _ in range(45): pb.record_use(f, 1793000000)
assert pb.quota_left(45, f, 1793000000) == 0
assert pb.quota_left(45, f, 1793000000 + 40*86400) == 45      # new month resets
open(f, "w").write("{bad"); assert pb.quota_left(45, f, 1793000000) == 0   # corrupt -> fail closed
assert pb.settings({"interval_s": -5, "monthly_cap": True})["interval_s"] == 3600
print("all police_budget tests passed")
