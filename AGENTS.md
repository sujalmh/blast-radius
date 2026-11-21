# Blast Radius — Buildathon Project

**Event:** Builders Buildathon · **Track:** RWX — Blast Radius
**Track page:** https://www.joinbuilders.org/buildathon/tracks/blast-radius
**Deadline:** Sunday 2026-11-22 16:00 UTC (11:00 AM EST)
**Team GitHub account:** `sujalmh`

## Goal

Given patches to `sqlglot` at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, output for
each commit the tests to run and a split across up to 8 workers. Lower simulated CI
seconds wins. Missing a failing test costs +300 s, so recall matters more than shaving
a few seconds.

Output: one JSON line per commit on stdout:
`{"commit": "k7q2", "workers": [[12, 845, 901], [3, 77]]}`
Logs to stderr only. `"workers": []` means run nothing.

## Hard rules (from SCORING.md / README)

- No precomputed map from a patch, or from a kind of edit, to the tests it breaks.
  Maps built from the pinned sqlglot commit (coverage, imports, fixtures) are allowed.
- Repo ≤ 20 MB. No submodules or Git LFS. Clone sqlglot in the Dockerfile if needed.
- Build: network OK, 4 cores, 8 GB RAM, 10 min. Run: no network, 2 cores, 4 GB RAM,
  3 min for the whole batch (4.5 min in the final), read-only disk, 64 MB `/tmp`,
  user `nobody`, ≤ 256 processes.
- CPU seconds of the selector process are added to the score, so selection must be fast.
- Do not commit secrets, sample answers used as a lookup, or anything that violates the
  "no answers ahead of time" rule.

## Model policy — MANDATORY

- **Main orchestrator:** `opencode-go/glm-5.3` with variant `max`. Plans, reviews,
  verifies, and integrates. It does not write implementation code itself.
- **All implementation / code changes:** strictly `opencode-go/deepseek-v4.1-flash`
  with variant `high`. Every worker/edit task must target:
  `providerInstanceId: "opencode"`, `model: "opencode-go/deepseek-v4.1-flash"`,
  `options: {variant: "high"}`.
- No other model may edit files. Reviewers without edits may use other models only if
  the orchestrator explicitly asks; implementation stays DeepSeek V4.1 Flash high.

## Session

The buildathon session token is in `.buildathon_session` (gitignored, never commit).
Used for submission on the track page after sign-in.

## Scoring cheat sheet

- Per commit: busiest worker load + 2 s × number of workers (≤ 8), 0 if no workers.
- Each commit that omits any failing test: +300 s.
- Total: sum of commit costs + 300 s × misses + selector CPU seconds.
- Image is run 3×; middle total is scored. Half-up rounding to 0.1 s.
- 30 sample commits ship with `sample/answers.json` for local scoring. Practice/final
  answers are hidden.

## Strategy notes

- Starter runs all 1,620 tests (~103 s) on 3 workers for every commit: very safe, very slow.
- The intended lever is static + dynamic dependency analysis at pinned commit:
  coverage map (test → source lines/functions), import graph, fixtures, and patch
  hunk → symbol mapping. Fixture changes (`kind: "fixture"`) need test-data-aware rules.
- Running sqlglot tests during the Docker build is allowed. Building a coverage map once
  and committing it is encouraged (build time cap is 10 min).
- Empty `workers` is correct for harmless patches; but a comment-only change is not
  guaranteed harmless.
- Validate every change with `make sample` and keep the best scoring commit.
- `make check` validates output format only.

## Repo layout

- `selector.py` — entry point the image runs.
- `Dockerfile` — scorer builds this on linux/amd64.
- `score.py` — local scorer (exact simulated seconds).
- `sample/` — 30 sample commits + tests.json + answers.json.
- `SCORING.md` — exact rules.
- `README.md` — starter kit docs (keep, extend with our approach later).
