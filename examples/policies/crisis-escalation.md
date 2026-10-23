# Crisis escalation policy (template)

Copy to `docs/policies/crisis-escalation.md` and list it under
`[policy_documents]` in `humane-policy.toml`. The check reads it before it
judges, so anything written here as permitted stops being flagged. Write only
what is actually decided, and delete every line that is not true for you.

Owner: TODO
Last reviewed: TODO

## When the product gives a crisis resource

- TODO: what triggers a hotline or crisis resource (for example, a classifier
  result, a keyword list, a human review).
- TODO: when you deliberately do not offer one, and why. Example: "We do not
  offer a hotline outside a classified crisis because it over-triggers and
  users learn to ignore it."
- TODO: what the product does instead when someone is struggling but not in
  crisis.

## Repeat limits

- TODO: any cap on how often a resource is sent, and why.
- TODO: what the product says when the cap holds back a resend. Pointing back
  to the resource already given is different from going quiet. Write which.
- TODO: when the cap never applies (for example, imminent risk).

## Country coverage

| Country | Resource | Status |
|---|---|---|
| TODO | TODO | live / under review |

Write a known gap as **under review**, not as permitted. The check keeps
reporting it until a better resource is in place.

TODO: who checks these numbers, and how often. Lines go stale.
