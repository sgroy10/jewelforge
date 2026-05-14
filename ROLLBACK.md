# JewelForge Rollback Guide

## TL;DR — If anything breaks after wiring, run this

```bash
cd C:\Users\HR-02\jewelforge
git checkout v1.5.1-safe-rollback-2026-05-13
railway up
```

That's it. ~3 minutes to restore production to the exact state it was in on 2026-05-13.

---

## What the safe rollback point is

**Tag:** `v1.5.1-safe-rollback-2026-05-13`
**Commit:** `b7161f0`
**Commit message:** "Fix Blender version detection: use bpy.app.version, not hasattr(bpy.ops)"
**Date:** This commit was the last shipped production state before any wiring of `shell_v2/` to production.

This is identical to `v1.5.1-bpy-version-fix` (created earlier). The `-safe-rollback-2026-05-13` tag is a duplicate marker with an obvious name so any future engineer can find it.

## What's safe and what's NOT after this point

### Safe to revert (untouched by today's work)
- `blender_scripts/scale_and_repair.py` — production ring/pendant scaler
- `blender_scripts/smart_refine.py` — production "shelling" (currently fake)
- `server.py` — Flask API endpoints
- `Dockerfile`, `railway.toml`, `requirements.txt` — deployment config
- Anything else in the repo root

### NOT in this rollback (separate experimental work)
- `shell_v2/` folder — the new sizing + hollowing experiments
  - `shell_v2/blender_hollow.py` — voxel-remesh + Solidify hollowing
  - `shell_v2/detect_ring_size.py` — horizontal-max-at-mid sizing
  - `shell_v2/inputs/` and `shell_v2/outputs/` — test files
- These are untracked files (not yet committed). They are independent of production. Rolling back production does not affect them.

## Step-by-step rollback procedure

### Scenario 1: Production code wired and broken
```bash
cd C:\Users\HR-02\jewelforge

# 1. Verify current state
git log --oneline -5
git status

# 2. Reset to safe point
git checkout v1.5.1-safe-rollback-2026-05-13

# 3. Confirm you are at the safe commit
git log --oneline -3
# Should show: b7161f0 Fix Blender version detection: use bpy.app.version, not hasattr(bpy.ops)

# 4. Push to Railway
railway up

# 5. Verify deployment
curl https://<your-railway-url>/api/health
# Expect: 200 OK with v1.5.1 banner
```

### Scenario 2: New shell_v2 code somehow leaked into production
```bash
# Check if shell_v2 stuff is in any committed file
git diff v1.5.1-safe-rollback-2026-05-13..HEAD -- blender_scripts/ server.py

# If yes, hard reset (after backing up any work you want to keep)
git checkout v1.5.1-safe-rollback-2026-05-13 -- blender_scripts/ server.py
git commit -m "Rollback blender_scripts and server.py to v1.5.1"
railway up
```

### Scenario 3: Customer is mid-order and getting errors
- Step 1 above (`git checkout` + `railway up`) takes ~3 minutes
- During those 3 minutes, customers will see API errors
- Lovable's frontend should retry — customers experience a brief delay
- Existing in-progress files (Tripo outputs) are unaffected

## How to verify rollback succeeded

After `railway up` completes:

### Check 1: Endpoint responds
```bash
curl https://<your-railway-url>/api/health
```
Expect: 200 OK, JSON with `"version": "1.5.1"` or similar

### Check 2: Old behavior restored
Send a test order through Lovable. Confirm:
- Ring size is "off by ~1 size" (v1.5.1 bbox+1.5mm method)
- Weight ships heavier than ordered (no real hollowing)
- Decoration detail intact (because no remesh)

If you see this behavior → rollback succeeded.

## What you LOSE by rolling back
- Correct ring size measurements (Rhino-matched)
- Real hollowing for chunky/simple rings
- OBJ format export
- Any vision-check protections

What you KEEP by rolling back:
- Stable, predictable (if imperfect) production
- Zero risk of damaged customer files
- All your customer's existing orders remain unaffected

## After rollback — next steps
1. Diagnose why the wiring failed (logs, error messages, customer reports)
2. Fix the issue in a feature branch (NOT main)
3. Test thoroughly in `shell_v2/` sandbox
4. Re-attempt wiring with the fix

## Contact / escalation
- Owner: Sandeep Roy (sgroy10@gmail.com)
- Railway project: speclock-mcp-production (verify with `railway status`)
- Domain: jewelforge.in (or wherever the API is hosted)

## Last updated
2026-05-13 — Created during shell_v2 wiring planning. Production still on v1.5.1.
