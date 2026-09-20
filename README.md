# claude-captain

A playbook and a small toolkit for running several Claude Code sessions at once, where one
session coordinates and the rest do the work.

The coordinating session is the Captain. It holds the queue, writes the briefs, checks the
results, and stays free to answer you. It does not take long jobs itself, because a captain
that is head down is a captain that cannot answer.

## Why this exists

Running one agent is easy. Running five is a different job, and it fails in a small number
of repeatable ways: work gets handed out that was already finished, a session quietly stops
and nobody notices for hours, two lanes edit the same function from different directions,
or a result gets believed because the worker sounded sure.

None of that is fixed by a better prompt. It is fixed by a coordinator with rules.

## What is here

| Path | What it is |
|---|---|
| [`CAPTAIN.md`](CAPTAIN.md) | The playbook: 30 rules for coordinating a fleet |
| `skills/dispatch-brief/` | A Claude Code skill: one standard brief to send every worker |
| `skills/escalate-or-decide/` | A Claude Code skill: what to decide alone, what to wake your human for |
| `scripts/decisions.py` | An open-decision ledger, so questions do not live in chat |
| `scripts/decisions-panel.py` | A live terminal panel over that ledger |

Start with `CAPTAIN.md`. The rest is what the playbook tells you to use.

## The playbook in one paragraph

Spend the start of a run building a queue you have verified, not handing out work. Treat
the backlog as a claim and check each item against the live code, because entries outlive
their bugs. Send one standard brief every time, and make each one name the command that
proves the work is done. Treat a session that misses two nudges as stopped rather than
thinking. Check every merge yourself, because a worker reports what it believes, not what
happened. Decide the routine things alone and wake your human only for production, money,
destruction, or credentials.

## Install

Clone it anywhere.

```bash
git clone https://github.com/ryangalea23/claude-captain
```

**The playbook.** Copy `CAPTAIN.md` into your project, or point your coordinator session at
it. It is written to be read by an agent, not only by you.

**The skills.** Copy the two folders under `skills/` into `~/.claude/skills/`. Claude Code
picks them up on the next session. `dispatch-brief` has a trap list with a placeholder for
the traps specific to your codebase; filling that in is what makes it worth having.

**The ledger.** `scripts/decisions.py` needs nothing but Python 3.

```bash
python scripts/decisions.py open deploy-window --question "Ship tonight or Monday?" --asked-by worker-2
python scripts/decisions.py list
python scripts/decisions.py close deploy-window --answer "Monday"
```

It writes one JSON line per event to `~/.claude/decisions.log`, or wherever
`DECISIONS_LOG_PATH` points. `decisions-panel.py` opens a live view of the same file and
refreshes when another process changes it.

## Tests

```bash
cd scripts && python -m pytest -q
```

## The tools the playbook assumes

`CAPTAIN.md` tells you to watch what each session is doing, notice the stopped ones, and
hand work off. This repo does not include those tools. They live in
[claude-fleet](https://github.com/ryangalea23/claude-fleet): `fleet` shows every running
session and whether it is working or waiting on you, `snapshot` and `handoff` move work
between sessions, and `ai-usage` shows how much plan headroom each account has left.

You can follow the playbook without them. You will just be checking by hand what those
would tell you at a glance.

## Requirements

Python 3.10 or newer. The panel uses the standard library only, and its Windows-specific
key handling is guarded, so the ledger works anywhere Python does.

## License

MIT
