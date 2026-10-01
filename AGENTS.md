# Agent Instructions

## Tasks

Work is tracked in the project's fdbot room on bots.fallday.ca, which
`.fdbot.json` at the repo root links to. **That task list is the system of
record.**

```bash
fdbot prime                                # who you are, open and ready tasks, recent chat
fdbot task ready                           # what to work on
fdbot task show 1.1
fdbot task start 1.1                       # in progress, assigned to you
fdbot task close 1.1 --reason "what was done"
fdbot task create "Title" -d "details" -p 2 -t bug --parent 1 --dep 1.6
```

- Use the room's list for anything worth tracking, not TodoWrite or markdown TODOs.
- Put the task ref in commit messages, e.g. `Redact pending duplicates (localevents.1.1)`.
- The `fdbot` skill covers watching the room and replying in it.
- Commit or push only when asked.

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var
