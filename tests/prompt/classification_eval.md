# Classification prompt evaluation

Hand-curated test cases for the per-message classifier. The harness
runs in CI when the system prompt changes (target: ≥ 90% accuracy).
Each case names its bucket, supplies the headers + body, and gives a
short rationale so future-you knows why this case is canonical.

## Case template

```yaml
- name: <short-slug>
  expected_bucket: <one of 13>
  rationale: >
    why this case is the canonical example for the bucket
  message:
    headers: |
      From: ...
      To: ...
      Subject: ...
      ...
    body: |
      ...
```

Cases are added per slice. Skeleton has no cases yet.
