# Project AGENTS Setup Prompt

Use this prompt when starting work in a project that does not yet have a root-level `AGENTS.md`.

```text
Set up this project so it follows the AGENTS.md rule.

Requirements:
- Ensure the project root contains an `AGENTS.md`.
- If `AGENTS.md` does not exist, create it before making any substantive code changes.
- Treat `AGENTS.md` as the canonical project-specific instruction file.
- Keep the file concise, explicit, and production-focused.
- Include:
  - project purpose
  - coding expectations
  - testing expectations
  - deployment or runtime notes if relevant
  - any repo-specific constraints worth following
- If there is an existing instruction file such as `.github/copilot-instructions.md`, align it to point to `AGENTS.md` as the source of truth instead of duplicating conflicting rules.
- If the repository has tests, add a lightweight regression test or equivalent enforcement so `AGENTS.md` cannot be removed silently.

Process:
1. Inspect the repository structure and existing instruction files.
2. Explain the plan briefly.
3. Identify the root cause if this is a remediation task.
4. Implement the minimal set of changes needed.
5. Show the diff-style changes.
6. Run the smallest relevant verification step.
7. Report assumptions, risks, and what was verified.

Constraints:
- Do not invent requirements.
- Do not add unnecessary dependencies.
- Prefer simple, readable enforcement.
- No silent fallbacks.
- Output complete, runnable changes.
```
