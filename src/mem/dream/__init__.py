"""Dreams: headless agent runs that reflect on one quiet session's transcript.

A per-machine scheduler (launchd, via `mem dream install`) wakes every few
minutes and runs `mem dream run`. That plans, with no tokens, which sessions
have gone quiet with something new to learn from, then starts one headless
agent per chosen session. Each dream reads only its own subject's transcript
digest, adds and prunes memories, and may fix skills; the runner commits what
changed. A run that committed a dream ends with a consolidation pass scoped to
what changed since the last one.

The scheduler lives outside the agents it reflects on because an agent's own
background shell dies when its chat closes, which is exactly when a session
goes quiet. Dreams and consolidations tag their own sessions so they are never
dreamed about, and tag their ledger events so they never count as demand.
"""
