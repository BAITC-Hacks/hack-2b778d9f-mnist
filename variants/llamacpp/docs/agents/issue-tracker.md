# Issue tracker: GitHub

Issues and specs for this repo live in GitHub Issues. Use `gh` for tracker operations; it infers the repository from the clone's GitHub remote.

## Conventions

- Create: `gh issue create --title "..." --body "..."` (use a heredoc for multiline bodies).
- Read: `gh issue view <number> --comments`; filter comments with `jq` when needed and fetch labels too.
- List: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`, adding appropriate `--label` and `--state` filters.
- Comment: `gh issue comment <number> --body "..."`.
- Add/remove labels: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`.
- Close: `gh issue close <number> --comment "..."`.

PRs as a request surface: **no**. Do not treat PRs as feature requests for triage. If a reference such as `#42` is ambiguous, try `gh pr view 42` and fall back to `gh issue view 42` because GitHub shares number space.

## Wayfinder

The map is one issue labelled `wayfinder:map`, with child issues as tickets. Create the map with `gh issue create --label wayfinder:map`. Link child tickets as GitHub sub-issues via the sub-issues API; if unavailable, use a task list on the map and put `Part of #<map>` at the top of each child. Use `wayfinder:<type>` labels (`research`, `prototype`, `grilling`, or `task`) and assign a claimed ticket to the driving developer.

Use GitHub's native issue dependencies as the canonical, UI-visible blocker representation:

```sh
gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>
```

`<blocker-db-id>` is the blocker's numeric database ID, not its issue number or node ID; get it with `gh api repos/<owner>/<repo>/issues/<n> --jq .id`. If dependencies are unavailable, put `Blocked by: #<n>, #<n>` at the top of the child body. A child is unblocked when every blocker is closed.

The frontier is the first open map child in map order with no open blockers and no assignee. Check `issue_dependencies_summary.blocked_by` (open blockers only), or the fallback `Blocked by` references. Claim with `gh issue edit <n> --add-assignee @me` as the session's first write. Resolve by commenting with the answer, closing the ticket, then appending a context pointer (gist + link) to the map's Decisions-so-far.
