# Evals

Twenty to thirty real tasks with checkable outputs, run on every model swap. Two
seed tasks ship here; the set is only useful once it reflects work you actually
want done.

## Run

```bash
.venv/bin/python evals/run.py                    # every task, current config
.venv/bin/python evals/run.py --model moonshotai/kimi-k2-thinking
.venv/bin/python evals/run.py csv-to-summary     # one task
```

Results append to `results.jsonl` — pass/fail plus steps, tokens, cost and
wall-clock, so two models are comparable on more than vibes.

## Add a task

```
evals/tasks/<id>/
    task.json    {"prompt": "...", "timeout": 300}
    files/       copied into a fresh workspace before the run
    check.sh     runs in the workspace afterwards; exit 0 means pass
```

Two rules make a task worth having. The check must be mechanical — grep for a
value, diff a file, run the output — never "does this look right". And the
prompt must be something you genuinely wanted done, because the eval set is what
stops you tuning the harness against imaginary work.
