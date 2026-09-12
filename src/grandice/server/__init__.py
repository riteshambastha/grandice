"""The live-view dashboard: a FastAPI + SSE window onto a running session.

Read-only observation plus sending a task and cancelling one, matching the
scope decided for this pass — the interactive approval UI, file diff view and
task board are the fuller P5 client and are not built here. See
`grandice-web --help` and the top-level README's "Live view" section.
"""
