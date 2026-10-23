# Humane Gate

> **This is the repo to install.** It is the GitHub Action for your own repositories. Want to see it work first? Read the nine example pull requests in the [demo repo](https://github.com/buildinghumanetech/humane-gate).

An advisory HumaneBench check that runs on every pull request and scores what
the diff changes about what your software says or does to a person.

**It blocks nothing.** The check run is always `neutral`. There is no red in
the output, because red means "stop" everywhere else in CI and an engineer who
reads a stop sign stops reading. The strongest verdict is orange, and orange
means "worth a conversation".

It runs on **your** Anthropic API key, in **your** runner. No diff, no code and
no verdict is sent to Building Humane Tech.

## Install

One secret and one workflow file.

1. Add a repository secret named `ANTHROPIC_API_KEY`
   (Settings → Secrets and variables → Actions → New repository secret).
2. Commit this as `.github/workflows/humane-gate.yml`:

```yaml
name: humane-gate

on:
  pull_request:
    types: [opened, synchronize, reopened]
  # A person replying "/humane accept" is the only conversation CI can have.
  issue_comment:
    types: [created]

permissions:
  contents: read
  pull-requests: write
  checks: write

jobs:
  advisory:
    runs-on: ubuntu-latest
    if: >-
      github.event_name != 'issue_comment' ||
      (github.event.issue.pull_request != null &&
       contains(github.event.comment.body, '/humane accept'))
    steps:
      # Check out the DEFAULT branch, never the pull request. This is what
      # keeps a pull request from supplying the policy it is scored against.
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.repository.default_branch }}
          fetch-depth: 0

      - uses: buildinghumanetech/humane-gate-action@v1
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

That is the whole install. Open a pull request and the check comments on it.

## Zero config

You do not need a policy file to start. With no `humane-policy.toml` in your
repo, the action scores against its own
[`default-humane-policy.toml`](default-humane-policy.toml) and says so at the
bottom of the comment.

When you want the numbers to be yours, copy that file to your repository root
and edit it. That file is *values as code*: the rubric says what humane means,
your policy says what the number is. A meditation app and a logistics tool
should not have the same notification ceiling. What they should share is that
the ceiling exists, is written down, and is reviewed like any other code.

**The policy is always read from your default branch, never from the pull
request.** A pull request that edits `humane-policy.toml` is judged against the
policy already on your base branch, and the edit itself is judged on its
merits. Otherwise a change could permit itself anything on the way in.

## Your own policies

Your internal policies can sit beside the numbers. Put them in the repo as
plain documents and list them in `humane-policy.toml`:

```toml
[policy_documents]
paths = [
  "docs/policies/crisis-escalation.md",
  "docs/policies/accepted.md",
]
```

The check reads every listed document before it judges. Where one permits what
a change does, that is not a finding, and the comment names the document that
permits it. Missing files are skipped with a note in the log.

Two rules keep this honest:

- **A document cannot excuse a −1.0 on a floor principle.** That is always
  reported, so a permissive policy cannot produce a quiet report.
- **Write a known gap as "under review", not as permitted.** Then the check
  keeps reporting it until it is fixed.

A decision your team has made once belongs in a document, with who decided and
why. Then it stops being flagged on every future run, and someone can revisit
it later.

Templates to start from, in [`examples/policies/`](examples/policies/):

- [`crisis-escalation.md`](examples/policies/crisis-escalation.md): when you
  give a crisis resource, when you deliberately don't, repeat limits, country
  coverage.
- [`accepted.md`](examples/policies/accepted.md): findings you've decided to
  keep, with who decided and why.

## Once a day instead of every pull request

To try the check before it comments on anything, run it on a schedule over the
pull requests from the last day:

1. Copy [`default-humane-policy.toml`](default-humane-policy.toml) to your repo
   root as `humane-policy.toml`. Set your own numbers and floor.
2. Copy the templates in [`examples/policies/`](examples/policies/) to
   `docs/policies/`, fill them in, and list them under `[policy_documents]`.
3. Copy [`examples/humane-gate-daily.yml`](examples/humane-gate-daily.yml) to
   `.github/workflows/`.
4. Add a repository secret named `ANTHROPIC_API_KEY`.
5. Merge to your default branch. Policy is only read from there.
6. In the Actions tab, open **HumaneGate daily** and run it once by hand.

It starts in test mode: results go to the run summary only, and nothing is
posted on pull requests. Turn comments on when you trust it. The same policy
file and documents apply.

## Running it from an agent

If you run the check with your own agent, point the agent at the same files
rather than telling it your decisions in chat. A chat instruction is gone next
session; a file carries over and can be reviewed. Give it something like:

> Before judging, read `humane-policy.toml` and every document listed under
> `[policy_documents]`. Score against our numbers, not defaults. When a
> document permits what you see, don't flag it; say which document permits it.
> Never excuse a −1.0 on a floor principle. When I accept a finding, add it to
> `docs/policies/accepted.md` with who decided and why, instead of remembering
> it.

If the agent can run commands, have it call the judge directly, from your
repository root, so policy handling is the same code the action runs:

```bash
git clone --depth 1 --branch v1 https://github.com/buildinghumanetech/humane-gate-action .humane-gate
pip install -r .humane-gate/requirements.txt
DRY_RUN=1 BASE_SHA=<base sha> HEAD_SHA=<head sha> \
  python .humane-gate/humanebench/judge.py
```

## Inputs

| Input | Required | Default | What |
|---|---|---|---|
| `anthropic-api-key` | yes | — | Your own key. |
| `model` | no | `claude-sonnet-4-6` | Model the judge runs on. |
| `policy-path` | no | `humane-policy.toml` | Where your values live, relative to the repo root. |
| `anthropic-workspace-id` | no | `""` | Optional, for billing separation. |
| `pr-number` | no | `""` | Only for `workflow_dispatch`; taken from the event otherwise. |
| `github-token` | no | `${{ github.token }}` | Used to read the pull request and post the comment. |

## What it costs

**About 7 cents per pull request.** Measured, not estimated: nine real pull
requests replayed through this action on `claude-sonnet-4-6`, at $3.00 per
million input tokens and $15.00 per million output tokens.

| | Input tokens | Output tokens | Cost |
|---|---|---|---|
| Average per pull request | 20,536 | 611 | **$0.071** |
| Cheapest run (clean, no findings) | 20,105 | 213 | $0.064 |
| Dearest run (3 findings + a question) | 20,777 | 1,288 | $0.082 |
| All nine together | 184,825 | 5,499 | $0.637 |

Input dominates and barely moves: the rubric, the adaptation layer and your
policy are the same every time, so a pull request costs roughly what the
rubric costs to read. Only the output varies, which is why a clean run is
cheaper than one that writes three findings.

Your own numbers will differ with diff size and model. Token counts for every
run are printed in the workflow log (`humanebench: usage ...`) if you want to
measure your own.

The check is advisory, so the honest framing is: this is the price of a second
opinion on every pull request, not the price of a gate.

A diff that changes no behaviour costs nothing at all. A deterministic scope
filter runs before the model, so docs-only, test-only and dependency-only pull
requests never reach the API.

## Security

**Forks are not supported in v1.** GitHub withholds secrets from pull requests
opened from forks, so the action has no key to run with and the job will fail.
The usual workaround, `pull_request_target`, is **unsafe here and must not be
used**: it runs with a writable token and access to your secrets in the context
of the base repository, and this action fetches pull request refs. Combining
those is how a fork gets your key. If you need fork coverage, wait for v2
rather than reaching for `pull_request_target`.

What the action does with a pull request: it fetches the head as loose git
objects and reads the diff text. It never checks the pull request out, never
builds it, never executes it. The gate's own rubric, prompts and judge are read
from the action's checkout, so a pull request cannot edit the thing that scores
it.

## What gets posted

A single comment, updated in place on re-runs, plus a `neutral` check run.
Findings carry a short id. Anyone with write access can close one with
`/humane accept <id> <reason>`, which records the decision under their name.
That record is the point: the check is not trying to be right, it is trying to
make the decision explicit.

## What this is

The eight [HumaneBench](https://github.com/buildinghumanetech/humanebench)
principles, applied to a code diff instead of a chat response. The rubric is
vendored byte-for-byte and pinned; the action does not fetch it at run time.

The demo repository, with nine example pull requests you can read before
installing anything, is
[buildinghumanetech/humane-gate](https://github.com/buildinghumanetech/humane-gate).

## Support and security

Questions and bugs: open an issue here. Security problems: see [SECURITY.md](SECURITY.md). Community and contribution guidelines come from the organization defaults at [buildinghumanetech/.github](https://github.com/buildinghumanetech/.github).

## Name and trademark

"Humane Gate" and "HumaneBench" are marks of Building Humane Tech. Running this action does not make your product certified or endorsed by us, and you should not say it does. The draft guidelines are in [TRADEMARKS.md](https://github.com/buildinghumanetech/humanebench/blob/main/TRADEMARKS.md) in the humanebench repo.

## Licence

Apache License 2.0. See [`LICENSE`](LICENSE).

`rubrics/rubric_v4.md` is vendored from
[buildinghumanetech/humanebench](https://github.com/buildinghumanetech/humanebench)
and carries that repository's terms, not this one's.
