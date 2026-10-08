#!/usr/bin/env python3
"""HumaneBench PR check, advisory mode.

Scores a pull request diff against the eight Building Humane Tech
principles. Posts a comment and a neutral check run. Blocks nothing, ever.

Env:
  ANTHROPIC_API_KEY   required
  GITHUB_TOKEN        required in CI
  REPO                owner/name
  PR_NUMBER           pull request number
  BASE_SHA, HEAD_SHA  merge-base diff endpoints
  HUMANEBENCH_MODEL   optional, defaults below
  DRY_RUN             set to 1 to print the result and skip all GitHub calls
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

import anthropic
import requests

MODEL = os.environ.get("HUMANEBENCH_MODEL", "claude-sonnet-4-6")
MAX_DIFF_CHARS = 60_000
MAX_CONTEXT_CHARS = 40_000
# Comment marker. Deliberately unchanged from the original "shadow" name so
# that re-runs keep updating existing PR comments instead of posting new ones.
MARKER = "<!-- humanebench-shadow -->"
# Deliberately no red. Red means "blocked" everywhere else in CI, and this check
# blocks nothing; an engineer who sees red reads it as a stop sign and stops
# reading. Orange is the strongest thing here and it means "worth a conversation".
DOT = {"clear": "\U0001F7E2", "review": "\U0001F7E1", "discuss": "\U0001F7E0",
       "question": "\U0001F535", "+1.0": "\U0001F7E2", "accepted": "\U000026AA"}
VERDICT_WORD = {"clear": "Clear", "review": "Review", "discuss": "Discuss",
                "accepted": "Accepted"}
DROP_CONFIDENCE = {"low"}

HERE = os.path.dirname(os.path.abspath(__file__))

# The gate and the repository it judges are two different checkouts.
#
# GITHUB_ACTION_PATH is where this action's own files live: the rubric, the
# prompts, the bundled default policy. A pull request cannot reach any of it,
# which is the whole reason the split exists.
#
# GITHUB_WORKSPACE is the customer's repository: their policy, the documents it
# names, and the git history the diff is read from.
#
# With neither set -- running locally, or under DRY_RUN in a test -- both fall
# back to this checkout, so a single clone still works end to end.
ACTION_ROOT = os.environ.get("GITHUB_ACTION_PATH") or os.path.dirname(HERE)
WORKSPACE = os.environ.get("GITHUB_WORKSPACE") or os.getcwd()

RUBRIC_TAG = "v4"

# Action side. Fixed text, pinned, never read from the repo under review.
RUBRIC = os.path.join(ACTION_ROOT, "rubrics", f"rubric_{RUBRIC_TAG}.md")
RUBRIC_VERSION = os.path.join(ACTION_ROOT, "rubrics", "VERSION")
PROMPT = os.path.join(ACTION_ROOT, "humanebench", "prompt.md")
PROMPT_DOCUMENT = os.path.join(ACTION_ROOT, "humanebench", "prompt_document.md")
BUNDLED_POLICY = os.path.join(ACTION_ROOT, "default-humane-policy.toml")

# Customer side. Their values, their documents, their diff.
#
# The policy is resolved against the workspace, and the workflow checks out the
# default branch, so a pull request that edits humane-policy.toml is still
# judged against the policy on the base branch. That is the point: changing
# what the product may do to people is itself a change worth reviewing, and a
# diff must not get to rewrite the rules it is about to be scored by.
POLICY = os.path.join(WORKSPACE, os.environ.get("HUMANE_POLICY_PATH",
                                                "humane-policy.toml"))
# Zero config: with no policy of their own, the bundled default is used and the
# comment footer says so.
POLICY_IS_DEFAULT = not os.path.exists(POLICY)
if POLICY_IS_DEFAULT:
    POLICY = BUNDLED_POLICY


def policy_documents() -> list:
    """Company policy docs named in humane-policy.toml.

    Parsed with a deliberately dumb reader rather than a TOML library: this needs
    to work on whatever Python the runner happens to have, and the shape it reads
    is three lines long.
    """
    if not os.path.exists(POLICY):
        return []
    paths, in_block = [], False
    with open(POLICY) as f:
        for line in f:
            t = line.strip()
            if t.startswith("["):
                in_block = t == "[policy_documents]"
                continue
            if in_block and '"' in t and not t.startswith("#"):
                paths += re.findall(r'"([^"]+)"', t)
    out = []
    for rel in paths:
        full = os.path.join(WORKSPACE, rel)
        if os.path.exists(full):
            with open(full) as f:
                out.append((rel, f.read()))
        else:
            print(f"humanebench: policy document not found, skipping: {rel}")
    return out


def floor_principles() -> set:
    """Principles the organization has declared non-negotiable."""
    if not os.path.exists(POLICY):
        return set()
    names, in_block = [], False
    with open(POLICY) as f:
        for line in f:
            t = line.strip()
            if t.startswith("["):
                in_block = t == "[floor]"
                continue
            if in_block and '"' in t and not t.startswith("#") and "reason" not in t:
                names += re.findall(r'"([^"]+)"', t)
    return set(names)


def rubric_commit() -> str:
    """Short sha of the pinned rubric, so a finding can be traced to the exact
    text that produced it."""
    try:
        with open(RUBRIC_VERSION) as f:
            for line in f:
                if line.startswith("commit:"):
                    sha = line.split(":", 1)[1].strip().split()[0]
                    return sha[:7] if sha != "unknown" else "unpinned"
    except FileNotFoundError:
        pass
    return "unpinned"
# Deterministic scope filter. Runs before the model, so a docs-only or
# test-only pull request costs nothing and cannot produce a finding at all.
# The prompt has an abstain rule too; this is the cheap, auditable half of it.
OUT_OF_SCOPE = re.compile(
    r"""(^|/)(
        package-lock\.json | yarn\.lock | poetry\.lock | go\.sum | Cargo\.lock
      | \.github/ | humanebench/ | rubrics/ | scripts/ | dist/ | build/ | vendor/
      | node_modules/ | __pycache__/
      | tests?/ | __tests__/ | spec/ | e2e/ | fixtures?/
      | docs?/ | \.storybook/
    )|(
        \.(lock|snap|map|min\.js|min\.css|svg|png|jpe?g|gif|ico|woff2?|ttf)$
      | \.test\.[a-z]+$ | \.spec\.[a-z]+$ | _test\.[a-z]+$ | test_[^/]+$
    )""",
    re.VERBOSE,
)


def get_diff() -> tuple:
    """The diff, and the whole of every file it touches, after the change.

    Five lines of context is enough to see what changed and not enough to see
    what it calls. A judge that cannot see the guard clause fifteen lines up
    will say the guard is missing, which is the single fastest way to lose an
    engineer: they know it is there, so they stop reading. The files are
    context only. Evidence is still verified against changed lines, so the
    judge cannot quote an unchanged line as proof of anything.
    """
    base, head = os.environ.get("BASE_SHA"), os.environ.get("HEAD_SHA")
    rng = f"{base}...{head}" if base and head else "HEAD~1...HEAD"
    files = subprocess.run(
        ["git", "diff", "--name-only", rng],
        capture_output=True, text=True, check=True, cwd=WORKSPACE,
    ).stdout.split()
    keep = [f for f in files if not OUT_OF_SCOPE.search(f)]
    if files and not keep:
        print(f"humanebench: {len(files)} file(s) changed, all out of scope")
    if not keep:
        return "", ""
    out = subprocess.run(
        ["git", "diff", "--unified=5", rng, "--"] + keep,
        capture_output=True, text=True, check=True, cwd=WORKSPACE,
    ).stdout

    bodies, used = [], 0
    for path in keep:
        shown = subprocess.run(
            ["git", "show", f"{head or 'HEAD'}:{path}"],
            capture_output=True, text=True, cwd=WORKSPACE,
        )
        if shown.returncode != 0:      # deleted in this change; the diff has it
            continue
        block = f"<file path=\"{path}\">\n{shown.stdout}</file>\n"
        if used + len(block) > MAX_CONTEXT_CHARS:
            bodies.append(f"<!-- {path} omitted, context budget spent -->\n")
            continue
        bodies.append(block)
        used += len(block)
    return out[:MAX_DIFF_CHARS], "".join(bodies)


def build_system(mode: str = "diff") -> str:
    """HumaneBench rubric v4 verbatim, then the diff-adaptation layer.

    The rubric file is vendored unchanged from the benchmark repo so the two
    stay comparable. Every deviation is listed in RUBRIC_DELTAS.md in
    buildinghumanetech/humane-gate:
    https://github.com/buildinghumanetech/humane-gate/blob/main/RUBRIC_DELTAS.md
    """
    if not os.path.exists(RUBRIC):
        raise SystemExit(
            f"missing {RUBRIC}. The rubric ships inside this action, so the "
            "action checkout is incomplete: reference a released tag "
            "(buildinghumanetech/humane-gate-action@v1) rather than a branch or a "
            "partial copy. The check does not fetch the rubric at run time on purpose.")
    with open(RUBRIC) as f:
        rubric = f.read()
    with open(PROMPT) as f:
        adaptation = f.read()

    parts = [rubric, adaptation]
    if mode == "document":
        with open(PROMPT_DOCUMENT) as f:
            parts.append(f.read())

    for path, body in policy_documents():
        parts.append(
            f"# Company policy document: `{path}`\n\n"
            "This is a document the team wrote and committed. Where it permits\n"
            "what a diff does, that is not a finding, and you say which document\n"
            "permits it.\n\n" + body
        )
    # Values as code. The rubric says what humane means; the policy file is where
    # this organization writes down its own numbers. Read as text, not parsed:
    # the judge needs to understand it, not evaluate it.
    if os.path.exists(POLICY):
        with open(POLICY) as f:
            parts.append(
                "# This organization's declared values\n\n"
                "The file below is `humane-policy.toml`, committed to this repo by\n"
                "the team whose code you are judging. Where it sets a number or a\n"
                "rule, judge the diff against that, and say so in your rationale:\n"
                "\"their own policy says X\". Where it is silent, use the rubric alone.\n"
                "Do not substitute your own preferred threshold for theirs.\n\n"
                "A diff that changes this file is changing what the product is\n"
                "permitted to do to people. Judge that change on its merits, the same\n"
                "way you would judge the code it governs.\n\n"
                "```toml\n" + f.read() + "\n```"
            )
    return "\n\n---\n\n".join(parts)


PRINCIPLES = [
    "Respect User Attention",
    "Enable Meaningful Choices",
    "Enhance Human Capabilities",
    "Protect Dignity & Safety",
    "Foster Healthy Relationships",
    "Prioritize Long-Term Wellbeing",
    "Be Transparent and Honest",
    "Design for Equity & Inclusion",
]

FINDING_FIELDS = [
    "unless",
    "principle", "score", "tier", "confidence",
    "file", "evidence", "behavior", "rationale", "suggestion",
]

COMMEND_FIELDS = ["principle", "file", "evidence", "note"]

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings", "commendations",
                 "unresolved", "covered"],
    "properties": {
        "verdict": {"type": "string", "enum": ["clear", "review", "discuss"]},
        "summary": {"type": "string"},
        "unresolved": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "why_it_matters", "file",
                             "evidence", "principle"],
                "properties": {
                    # Named so the runner can tell that a finding on this file
                    # and this principle is contingent on this question.
                    "principle": {"type": "string", "enum": PRINCIPLES},
                    "question": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "file": {"type": "string"},
                    "evidence": {"type": "string"},
                },
            },
        },
        # Everything a policy document talked the judge out of. Making the
        # suppression an output rather than a silence is the whole point: a
        # company document can excuse a diff, and the runner still gets to see
        # what was excused and decide whether the document had the standing.
        "covered": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["principle", "file", "evidence", "document",
                             "permits", "behavior"],
                "properties": {
                    "principle": {"type": "string", "enum": PRINCIPLES},
                    "file": {"type": "string"},
                    "evidence": {"type": "string"},
                    "document": {"type": "string"},
                    "permits": {"type": "string"},
                    "behavior": {"type": "string"},
                },
            },
        },
        "commendations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": COMMEND_FIELDS,
                "properties": {
                    "principle": {"type": "string", "enum": PRINCIPLES},
                    "file": {"type": "string"},
                    "evidence": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": FINDING_FIELDS,
                "properties": {
                    "principle": {"type": "string", "enum": PRINCIPLES},
                    # String enum, not numeric: keeps the schema to keywords
                    # the API accepts. Rendered as-is.
                    "score": {"type": "string", "enum": ["-1.0", "-0.5"]},
                    "tier": {"type": "string"},
                    "confidence": {"type": "string",
                                   "enum": ["high", "medium", "low"]},
                    "file": {"type": "string"},
                    "evidence": {"type": "string"},
                    "behavior": {"type": "string"},
                    "rationale": {"type": "string"},
                    "suggestion": {"type": "string"},
                    # The condition that would make this finding wrong, when
                    # one exists. "-1.0 unless X" is an honest thing to say and
                    # a diff cannot answer it. Empty string when unconditional.
                    "unless": {"type": "string"},
                },
            },
        },
    },
}


def changed_lines(diff: str) -> list:
    """Every line the diff added or removed, without the +/- marker.

    Used to check a quoted piece of evidence actually exists. A judge that can
    invent its evidence is a judge nobody can argue with.
    """
    out = []
    for ln in diff.splitlines():
        if ln.startswith(("+++", "---")) or len(ln) < 2:
            continue
        if ln[0] in "+-":
            t = ln[1:].strip()
            if t:
                out.append(t)
    return out


def evidence_holds(quote: str, lines: list) -> bool:
    q = " ".join(quote.split())
    if not q:
        return False
    for ln in lines:
        n = " ".join(ln.split())
        if q in n or n in q:
            return True
    return False


def judge(diff: str, context: str = "", signed: dict = None,
          mode: str = "diff") -> dict:
    system = build_system(mode)

    # An org-scoped key must name a workspace explicitly. A workspace-scoped key
    # does not. Setting ANTHROPIC_WORKSPACE_ID makes either kind work.
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    client = anthropic.Anthropic(
        default_headers={"anthropic-workspace-id": ws} if ws else None
    )

    # Structured output, not an assistant prefill: the response is constrained
    # to SCHEMA, so a finding cannot arrive without its evidence or confidence.
    #
    # No sampling controls: the current Messages API exposes no temperature,
    # top_p or top_k. Verdicts can vary run to run on an identical diff. See
    # humane-gate's RUBRIC_DELTAS.md, "Known limitations".
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content":
                   (f"<document>\n{diff}\n</document>" if mode == "document"
                    else f"<diff>\n{diff}\n</diff>\n\n"
                         f"<files_after_change>\n{context}"
                         "</files_after_change>")}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )

    # Observability only. Printed, never returned, never read by any filter,
    # never part of a score. It exists so a team can see what the check costs.
    usage = getattr(resp, "usage", None)
    if usage is not None:
        print("humanebench: usage "
              f"model={MODEL} "
              f"input_tokens={getattr(usage, 'input_tokens', 0)} "
              f"output_tokens={getattr(usage, 'output_tokens', 0)}")

    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return {"verdict": "clear",
                "summary": "Judge returned unparseable output.",
                "findings": [], "commendations": [], "unresolved": [],
                "covered": [], "error": raw[:500]}

    # Three filters, all in code rather than model judgment. This is the whole
    # anti-noise story: the judge proposes, the runner disposes.
    result["acceptances"] = signed or {}
    result["mode"] = mode
    # In document mode every line is quotable: there is no "changed" subset,
    # and the same verification still stops the judge inventing evidence.
    lines = (changed_lines(diff) if mode == "diff"
             else [l.strip() for l in diff.splitlines() if l.strip()])
    where = ("evidence not in diff" if mode == "diff"
             else "evidence not in the document")
    kept, dropped = [], []
    for f in result.get("findings", []):
        if f.get("confidence") in DROP_CONFIDENCE:
            dropped.append(("low confidence", f.get("principle")))
            continue
        if not evidence_holds(f.get("evidence", ""), lines):
            dropped.append((where, f.get("principle")))
            continue
        kept.append(f)
    result["findings"] = kept[:3]

    # Ask or score, enforced rather than requested. If the judge raised a
    # question about a file and also filed a finding on the same file under the
    # same principle, the finding is contingent on the answer it just said it
    # did not have. Keep the question, drop the finding.
    asked = {(q.get("file"), q.get("principle"))
             for q in result.get("unresolved", [])}
    if asked:
        contingent = [f for f in result["findings"]
                      if (f.get("file"), f.get("principle")) in asked]
        for f in contingent:
            dropped.append(("contingent on an open question", f.get("principle")))
        result["findings"] = [f for f in result["findings"]
                              if f not in contingent]

    praise = [
        c for c in result.get("commendations", [])
        if evidence_holds(c.get("evidence", ""), lines)
    ][:2]
    result["commendations"] = praise

    result["unresolved"] = [
        q for q in result.get("unresolved", [])
        if evidence_holds(q.get("evidence", ""), lines)
    ][:3]

    # A policy document can excuse anything except a floor principle.
    #
    # Without this, an organization with a permissive policy gets a quieter
    # check, which makes this a conformance tool and not a humane one. The floor
    # is the line their own documents cannot move. Below it, a document that
    # permits the behavior does not end the argument; it becomes the argument,
    # and the finding is against the document rather than the diff.
    floor = floor_principles()
    excused, breached = [], []
    for c in result.get("covered", []):
        if not evidence_holds(c.get("evidence", ""), lines):
            dropped.append((where, c.get("principle")))
            continue
        (breached if c.get("principle") in floor else excused).append(c)
    result["covered"] = excused[:3]
    result["floor_breached_by_policy"] = breached[:3]

    for item in result["findings"] + result["floor_breached_by_policy"]:
        item["id"] = finding_id(item.get("principle", ""), item.get("file", ""))

    # Signed acceptances. A finding a person has taken responsibility for stops
    # driving the verdict, and starts being a record of who decided what.
    signed = result.get("acceptances") or {}
    head = os.environ.get("HEAD_SHA", "")
    for item in result["findings"] + result["floor_breached_by_policy"]:
        got = signed.get(item["id"])
        if not got:
            continue
        # Accepted against an older head. The code moved after somebody signed
        # for it, so the signature no longer covers what is now in the branch.
        item["accepted"] = dict(got, stale=bool(got.get("sha") and head
                                                and got["sha"] != head))

    live_findings = [f for f in result["findings"]
                     if not f.get("accepted") or f["accepted"]["stale"]]
    live_breaches = [b for b in result["floor_breached_by_policy"]
                     if not b.get("accepted") or b["accepted"]["stale"]]

    # The verdict is computed here, not taken from the model. Severity belongs to
    # the organization's floor, which is a fact about their policy file, not a
    # judgment call.
    #
    # A floor breach is a violation on a floor principle, not merely a mention of
    # one. Without the severity test every bad diff lands on "discuss" and the
    # three tiers collapse into two, which is the wall-of-red problem in orange.
    if live_breaches or any(f.get("principle") in floor and f.get("score") == "-1.0"
                            for f in live_findings):
        result["verdict"] = "discuss"
    elif live_findings or result["unresolved"]:
        result["verdict"] = "review"
    elif any(i.get("accepted") for i in
             result["findings"] + result["floor_breached_by_policy"]):
        # Everything raised has been signed for. Not the same as nothing raised,
        # and the comment should not pretend otherwise.
        result["verdict"] = "accepted"
    else:
        result["verdict"] = "clear"
    for why, which in dropped:
        print(f"humanebench: dropped {which!r} ({why})")
    return result


def stamp() -> str:
    """When this verdict was produced, and a link to the run that produced it.

    Visible proof the comment was rewritten: without it, a re-run edits the
    comment in place and nothing on the page appears to change.
    """
    when = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
    repo, run = os.environ.get("REPO"), os.environ.get("GITHUB_RUN_ID")
    if repo and run:
        return f"[{when}](https://github.com/{repo}/actions/runs/{run})"
    return when


def render(result: dict) -> str:
    """One verdict, then the detail folded away.

    The shape is deliberate. An engineer opening a pull request reads the first
    line and decides whether to read the second. A point-by-point score table
    makes that decision for them, and the decision is no.
    """
    verdict = result.get("verdict", "clear")
    findings = result.get("findings") or []
    questions = result.get("unresolved") or []
    praise = result.get("commendations") or []
    breached = result.get("floor_breached_by_policy") or []
    excused = result.get("covered") or []
    signed = [i for i in findings + breached
              if i.get("accepted") and not i["accepted"]["stale"]]
    stale = [i for i in findings + breached
             if i.get("accepted") and i["accepted"]["stale"]]
    findings = [f for f in findings if f not in signed]
    breached = [b for b in breached if b not in signed]

    lines = [
        MARKER,
        "### HumaneBench &middot; `advisory`"
        + (" &middot; reviewing a proposal, not code"
           if result.get("mode", "diff") == "document" else ""),
        "",
        f"## {DOT.get(verdict, '')} {VERDICT_WORD.get(verdict, verdict)}",
        "",
        result.get("summary", ""),
        "",
    ]

    if verdict == "accepted":
        lines += [
            "Everything this check raised has been accepted by someone with "
            "the standing to accept it, on the record, with a reason. Nothing "
            "here is unresolved and nothing was silently dropped.",
            "",
        ]

    if verdict == "discuss":
        lines += [
            "This touches something the team named as a floor in "
            "`humane-policy.toml`. Worth a conversation before it ships. "
            "It is not blocked and this check cannot block it.",
            "",
        ]

    # The floor a document cannot move. The diff is compliant; that is the
    # problem, and the comment says so in those words rather than pretending
    # the engineer did something wrong.
    if breached:
        for b in breached:
            lines += [
                f"### {DOT['discuss']} Permitted, and below the floor",
                "",
                f"**{b['behavior']}** This is allowed by "
                f"`{b['document']}`, which says {b['permits']}",
                "",
                f"`{b['principle']}` is named as a floor in "
                "`humane-policy.toml`, so a policy document does not settle it. "
                "The diff is not the thing to change here. Either the document "
                "or the floor is wrong, and that is a decision for a person.",
                "",
                f"<sub>`{b['file']}`</sub>",
                "",
                f"<sub>If this is the decision the team means to make, "
                f"`/humane accept {b.get('id', '')} &lt;your reason&gt;` puts a "
                "name and a reason on it. That record is the point.</sub>",
                "",
            ]

    if questions:
        lines += [f"### {DOT['question']} Needs context", ""]
        for q in questions:
            lines += [
                f"**{q['question']}**",
                "",
                f"{q['why_it_matters']} &nbsp;<sub>`{q['file']}`</sub>",
                "",
            ]

    if praise:
        for c in praise:
            lines += [
                f"{DOT['+1.0']} **Adds a protection.** {c['note']} "
                f"<sub>`{c['file']}`</sub>",
                "",
            ]

    if findings:
        n = len(findings)
        named = ", ".join(dict.fromkeys(f["principle"] for f in findings))
        lines += [
            "<details>",
            f"<summary><b>{n} finding{'s' if n > 1 else ''}</b> "
            f"&nbsp;&middot;&nbsp; {named}</summary>",
            "",
        ]
        for f in findings:
            lines += [
                f"#### {f['principle']} &nbsp;<sub>`{f.get('id', '')}` &middot; "
                f"{f['score']} &middot; confidence {f['confidence']}</sub>",
                "",
                f"> {RUBRIC_TAG} tier: _{f.get('tier', '')}_" if f.get("tier") else "",
                "",
                f"`{f['file']}`",
                "",
                "```diff" if result.get("mode", "diff") == "diff" else "```",
                (f"+ {f['evidence']}" if result.get("mode", "diff") == "diff"
                 else f["evidence"]),
                "```",
                "",
                ((("**Ships:** " if result.get("mode", "diff") == "diff"
                   else "**Would ship:** ") + f["behavior"])
                 if f.get("behavior") else ""),
                "",
                f["rationale"],
                "",
                f"**Smallest fix:** {f['suggestion']}",
                "",
            ]
            # A conditional finding. CI cannot hold a conversation, so the
            # question is asked in the only place a reply can arrive.
            if f.get("unless"):
                lines += [
                    f"**Unless:** {f['unless']} If that is true, say so and "
                    "this closes:",
                    "",
                    f"```\n/humane accept {f.get('id', '')} "
                    "<why it is true>\n```",
                    "",
                ]
            else:
                lines += [
                    f"<sub>Disagree? `/humane accept {f.get('id', '')} "
                    "&lt;your reason&gt;` records the decision under your name "
                    "and closes this.</sub>",
                    "",
                ]
        lines += ["</details>", ""]

    # The regulator answer. A named person, a reason in their own words, a
    # timestamp GitHub will not let anyone forge, and the commit it covers.
    # This is the difference between a wall of unexplained red and a decision.
    if signed:
        lines += ["### Accepted, on the record", ""]
        for i in signed:
            a = i["accepted"]
            lines += [
                f"**{i['principle']}** &nbsp;<sub>`{i['id']}`</sub>",
                "",
                f"> {a['reason']}",
                "",
                f"[@{a['who']}]({a['url']}) &middot; {a['standing']} &middot; "
                f"{a['when'][:10]} &middot; covers `{a['sha'][:7] or 'head'}`",
                "",
            ]

    if stale:
        lines += ["### Acceptance no longer covers this", ""]
        for i in stale:
            a = i["accepted"]
            lines += [
                f"**{i['principle']}** &nbsp;<sub>`{i['id']}`</sub> was accepted "
                f"by [@{a['who']}]({a['url']}) at `{a['sha'][:7]}`. The branch "
                "has moved since. They signed for code that is no longer what "
                "is here, so this is open again.",
                "",
            ]

    # Deference, shown rather than claimed. A team that sees the check name the
    # document that stopped it believes the next thing it says.
    if excused:
        for c in excused:
            lines += [
                f"<sub>Not flagged: {c['behavior']} is permitted by "
                f"`{c['document']}`.</sub>",
                "",
            ]

    lines += [
        "---",
        "<sub>"
        f"{DOT['clear']} clear &nbsp; {DOT['review']} review &nbsp; "
        f"{DOT['discuss']} discuss &nbsp; {DOT['question']} needs context "
        f"&nbsp; {DOT['accepted']} accepted by a person. "
        "There is no red, because this check does not block anything. "
        "Scored against "
        "<a href=\"https://github.com/buildinghumanetech/humanebench/blob/main/"
        f"rubrics/rubric_{RUBRIC_TAG}.md\">HumaneBench rubric {RUBRIC_TAG}</a>, "
        "loaded verbatim, "
        + ("plus the action's bundled default policy, because this repo has no "
           "<code>humane-policy.toml</code> of its own. "
           if POLICY_IS_DEFAULT else
           "plus this repo's <code>humane-policy.toml</code> and the policy "
           "documents it names. ")
        + "Findings whose quoted line is not in the "
        + ("document" if result.get("mode", "diff") == "document" else "diff")
        + ", or that the judge marked low-confidence, are dropped before "
        "posting. "
        f"Deviations from {RUBRIC_TAG} are in humane-gate's "
        "<a href=\"https://github.com/buildinghumanetech/humane-gate/blob/main/RUBRIC_DELTAS.md\">"
        "<code>RUBRIC_DELTAS.md</code></a>. "
        f"Rubric <code>{rubric_commit()}</code>, "
        f"commit <code>{os.environ.get('HEAD_SHA', 'local')[:7]}</code>.</sub>",
        "",
        ("<sub>This repo has no <code>humane-policy.toml</code>, so the gate "
         "scored against its bundled default. Copy "
         "<a href=\"https://github.com/buildinghumanetech/humane-gate-action/"
         "blob/main/default-humane-policy.toml\">the default</a> into your repo "
         "root and edit it: the numbers should be yours, not ours.</sub>"
         if POLICY_IS_DEFAULT else ""),
        "",
        f"<sub>Judged {stamp()}</sub>",
    ]

    out, prev_blank = [], False
    for ln in lines:
        if ln == "" and prev_blank:
            continue
        out.append(ln)
        prev_blank = ln == ""
    return "\n".join(out)


# A person may accept a finding. A bot, a passer-by and a fork may not.
# GitHub reports the commenter's standing on the repo, and only these three
# mean "this account can merge here". Without the check, "signed by a person"
# is decoration.
CAN_ACCEPT = {"OWNER", "MEMBER", "COLLABORATOR"}
ACCEPT_RE = re.compile(
    r"^\s*/humane\s+accept\s+([A-Z]{2,4}-[0-9a-f]{6})\s+(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def finding_id(principle: str, path: str) -> str:
    """A short handle a person can type into a comment.

    Derived from the principle and the file, not from the judge's wording. The
    judge is not reproducible: re-running it rewrites sentences, and an id built
    from a sentence would change underneath an acceptance that already cited it.
    Principle and file are the two things that stay put.
    """
    initials = "".join(w[0] for w in principle.split() if w[0].isupper())[:4]
    digest = hashlib.sha256(f"{principle}|{path}".encode()).hexdigest()[:6]
    return f"{initials or 'HB'}-{digest}"


def acceptances(repo: str, pr: str) -> dict:
    """Signed acceptances read out of the pull request conversation.

    The record lives in the comment thread rather than in a file the gate
    writes. GitHub already stores the author, the timestamp and the edit history
    of every comment, and it will not let one account post as another. Building
    a second, weaker ledger next to that one would be worse in every way that
    matters to somebody auditing it later.
    """
    out = {}
    try:
        comments = gh(
            "GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100").json()
    except Exception as exc:
        print(f"humanebench: could not read comments ({exc})")
        return out
    for c in comments if isinstance(comments, list) else []:
        who = (c.get("user") or {}).get("login", "")
        standing = c.get("author_association", "NONE")
        for fid, reason in ACCEPT_RE.findall(c.get("body") or ""):
            fid = fid.upper()
            if standing not in CAN_ACCEPT:
                print(f"humanebench: ignoring accept of {fid} by {who} "
                      f"({standing} cannot accept on this repo)")
                continue
            out[fid] = {
                "id": fid,
                "who": who,
                "standing": standing.title(),
                "reason": reason.strip().rstrip("."),
                "when": c.get("created_at", ""),
                "url": c.get("html_url", ""),
                # What the code looked like when they signed. An acceptance is
                # of a specific risk in a specific diff, not a standing waiver.
                "sha": sha_at(repo, pr, c.get("created_at", "")),
            }
    return out


def sha_at(repo: str, pr: str, when: str) -> str:
    """The head commit as of a moment in the conversation.

    A document has no commits, so this returns empty for an issue and the
    staleness check simply never fires. That is a real limitation, not a
    silent one: an acceptance on a proposal is not pinned to a version the way
    an acceptance on a diff is, so an edited proposal has to be re-run. Said
    out loud in the README.
    """
    if not when:
        return ""
    try:
        commits = gh("GET", f"/repos/{repo}/pulls/{pr}/commits?per_page=100").json()
    except Exception:
        return ""
    latest = ""
    for c in commits if isinstance(commits, list) else []:
        stamped = (((c.get("commit") or {}).get("committer") or {}).get("date")) or ""
        if stamped and stamped <= when:
            latest = c.get("sha", "")
    return latest


def gh(method: str, path: str, **kw):
    token = os.environ["GITHUB_TOKEN"]
    return requests.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
        **kw,
    )


def upsert_comment(repo: str, pr: str, body: str):
    """One comment per PR, updated in place. Re-running during a demo edits the
    same comment instead of stacking duplicates."""
    existing = gh("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100").json()
    for c in existing:
        if MARKER in (c.get("body") or ""):
            gh("PATCH", f"/repos/{repo}/issues/comments/{c['id']}", json={"body": body})
            return
    gh("POST", f"/repos/{repo}/issues/{pr}/comments", json={"body": body})


def post_check(repo: str, sha: str, result: dict):
    gh("POST", f"/repos/{repo}/check-runs", json={
        "name": "humanebench / advisory",
        "head_sha": sha,
        "status": "completed",
        "conclusion": "neutral",          # never failure. advisory, not a gate.
        "output": {
            "title": {"clear": "Clear",
                      "review": "Review before merge (advisory)",
                      "accepted": "Accepted by a named reviewer",
                      "discuss": "Worth a conversation (advisory)"}.get(
                          result.get("verdict", "clear"), "Advisory"),
            "summary": result.get("summary", ""),
        },
    })


def get_document() -> tuple:
    """The proposal to judge, and where to say what we thought of it.

    Three ways in, because product people arrive by three different doors: an
    issue body (a PRD pasted into GitHub), a file in the repo (a spec that lives
    beside the code), or stdin (a Linear ticket, a Notion page, anything a person
    can copy).
    """
    if os.environ.get("DOC_PATH"):
        path = os.environ["DOC_PATH"]
        if not os.path.isabs(path):
            path = os.path.join(WORKSPACE, path)
        if not os.path.exists(path):
            raise SystemExit(f"humanebench: no such document: {path}")
        with open(path) as f:
            return f.read(), path
    if os.environ.get("ISSUE_NUMBER"):
        repo, num = os.environ["REPO"], os.environ["ISSUE_NUMBER"]
        issue = gh("GET", f"/repos/{repo}/issues/{num}").json()
        title, body = issue.get("title", ""), issue.get("body") or ""
        return f"# {title}\n\n{body}", f"issue #{num}"
    return sys.stdin.read(), "stdin"


def review_document():
    text, origin = get_document()
    if not text.strip():
        raise SystemExit("humanebench: nothing to review")
    print(f"humanebench: reviewing {origin}, {len(text)} chars")

    # A person can accept a finding on a proposal exactly as they can on a
    # diff: same command, same comment thread, same GitHub-backed identity.
    num = os.environ.get("ISSUE_NUMBER")
    signed = (acceptances(os.environ["REPO"], num)
              if num and os.environ.get("GITHUB_TOKEN") else {})

    result = judge(text[:MAX_DIFF_CHARS], "", signed, mode="document")
    result["origin"] = origin
    body = render(result)

    if os.environ.get("DRY_RUN") or not num:
        print(json.dumps(result, indent=2))
        print("\n--- comment ---\n")
        print(body)
        return
    upsert_comment(os.environ["REPO"], num, body)
    print(f"humanebench: {result['verdict']}, "
          f"{len(result['findings'])} finding(s)")


def main():
    if os.environ.get("HUMANE_MODE") == "document" or "--document" in sys.argv:
        return review_document()

    diff, context = get_diff()
    if not diff.strip():
        result = {"verdict": "clear", "summary": "No reviewable files changed.",
                  "findings": [], "commendations": [], "unresolved": [],
                  "covered": []}
    else:
        repo, pr = os.environ.get("REPO"), os.environ.get("PR_NUMBER")
        signed = acceptances(repo, pr) if repo and pr and not os.environ.get(
            "DRY_RUN") else {}
        result = judge(diff, context, signed)

    if os.environ.get("DRY_RUN"):
        print(json.dumps(result, indent=2))
        print("\n--- comment ---\n")
        print(render(result))
        return

    repo, pr, sha = os.environ["REPO"], os.environ["PR_NUMBER"], os.environ["HEAD_SHA"]
    upsert_comment(repo, pr, render(result))
    post_check(repo, sha, result)
    print(f"humanebench: {result['verdict']}, "
          f"{len(result['findings'])} finding(s), "
          f"{len(result.get('unresolved', []))} question(s)")


if __name__ == "__main__":
    sys.exit(main())
