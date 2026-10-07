# Git workflow

- Never push directly to `main`.
- Create a separate branch for each stage before making changes.
- When the stage is complete, push its branch and create a Pull Request targeting `main` without waiting for another instruction.
- Do not merge the Pull Request yourself. Wait for the user's manual merge.
- Start the next stage only after the user has merged the previous Pull Request, switching to `main`, and running `git pull origin main` successfully. Create the next stage's branch from that freshly updated `main`.
