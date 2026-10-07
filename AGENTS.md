# Git workflow

- Never push directly to `main`.
- Create a separate branch for each stage before making changes.
- When the stage is complete, push its branch and create a Pull Request targeting `main` without waiting for another instruction.
- Do not merge the Pull Request yourself. Wait for the user's manual merge.
- Start the next stage only after the user has merged the previous Pull Request, switching to `main`, and running `git pull origin main` successfully. Create the next stage's branch from that freshly updated `main`.

## Git session recovery

- At every session start, run `git fetch origin`, inspect `git status` and local/remote branches, and check relevant GitHub PRs with `gh`. Git/PR state and the canonical roadmap are the source of truth, not conversation memory.
- For an unfinished milestone or open PR, continue its existing branch; do not create duplicate branches.
- If its PR is merged, check for uncommitted changes before switching, update `main` with `git pull origin main`, and create the next stage's branch.
- If its PR was closed without merging or the state is ambiguous, ask the user for confirmation before proceeding.
- Check for uncommitted changes before any branch switch; never discard them silently. Do not merge PRs or push directly to `main`.
