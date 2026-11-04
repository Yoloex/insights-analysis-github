# Implementation notes

## Architecture tour

The chat endpoint extracts the repository and date interval from user messages, then supplies bounded, no-argument tool definitions to local Qwen. The model selects tools, while the server fixes and validates repository and date arguments; model-supplied tool arguments are rejected. A maximum of four tools may run per request, and every tool is also exposed over HTTP. Conversation history stays client-side. Tool outputs are cached in SQLite and returned with the answer so the user can inspect the evidence; model prose and numbers are not independently verified. Live testing showed the small model can misstate even simple elapsed times; this is explicitly warned in the API and dashboard.

Flask validates repository names and half-open UTC periods. A dedicated HTTPX client calls only the fixed GitHub API origin, with optional read-only credentials and bounded retries. The issues collector excludes PRs, fetches closing actors with concurrency limited to five, and refuses oversized scans instead of presenting incomplete counts. Flask uses synchronous WSGI request workers; each upstream operation owns a short-lived async HTTPX client and event loop, preserving bounded concurrent issue-detail fetches without sharing clients across loops. SQLite executes in the request worker; cached metrics and narratives survive restarts. Process-local locks coalesce concurrent cache misses, trading throughput for a small, predictable single-worker service.

The legacy insight endpoint sends only three aggregate numbers to local Qwen, not issue titles, bodies, identities, or credentials. The system prompt lives in `app/prompts.py`, separate from the request and validation logic in `app/core.py`; changing its wording requires restarting the service (or rebuilding the Docker API image). Ollama JSON-schema output is checked with Pydantic, then each evidence key/value is checked against those aggregates. This validates numeric references, not the truth of every prose statement. The response includes source issue links and the complete metrics, model identity, subjective confidence warning, and collection timestamp. Jinja templates keep markup out of Python. A small same-origin browser dashboard and API documentation support the demo.

## Metric interpretation

- `closed_by` identifies the account that closed an issue; it is not necessarily its author, assignee, primary contributor, or owner of the underlying work. Bots and missing identities need explicit handling in the Python implementation.
- Counts reflect issues currently closed with a closing timestamp in the requested interval, not a historical ledger of every closure event. Reopened issues and repeated closures can change retrospective results.
- Pull requests also appear in GitHub's issues API and must be excluded.
- The descending-updated scan can stop only once updates precede the requested start: closing an issue updates it. Recently edited old issues can extend the scan substantially. This cutoff assumes GitHub's ordering and timestamps remain consistent; concurrent repository changes mean paginated reads are not a transactional snapshot.
- A bounded scan must return an explicit truncation error if completeness cannot be established. It must not silently return an incomplete ranking. Per-issue detail lookups increase latency and rate-limit usage.
- The end date is exclusive; the maximum interval is 90 days. Cached data can lag live repository state by the configured TTL. Closure counts alone are not a measure of productivity or work quality.

## Model and storage choices

The Dockerfile uses a multi-stage build: the `ollama` target inherits the official Ollama installation, starts a temporary server, waits for readiness, checks for Qwen and pulls it when absent. This model layer precedes all project COPY instructions and stays cached independently of application changes. A shell trap stops the temporary server. Compose builds that target for inference and the separate `api` target for Flask, preserving process separation without mounting a host model directory. The API waits for Ollama to become healthy. Clean or cache-invalidated model builds need network access and download the model again.

SQLite uses a separate persistent named volume. The API runs as a non-root user; the image owns its initial data directory. Existing volumes with incompatible ownership may need manual repair. The Windows model store is no longer used or modified by Docker.

The default is CPU-only and ports are published only on loopback. The Ollama image currently uses `latest`; pin a tested image digest for reproducible production deployment. The model tag is selected explicitly but is not a content-addressed pin.

## Validation and limitations

- Docker CLI 29.7.2 and Compose v5.5.1 are installed.
- Docker was initially unavailable. It was reachable during the image-baked-model change, but the image build/pull was stopped at user request. Build completion and container runtime remain unverified.
- Local `ollama list` confirmed `qwen3.5:0.8b` is installed.
- `docker compose --env-file .env -f docker/docker-compose.yaml config --quiet` passed. It does not require a running daemon and does not prove model compatibility, image builds, volume permissions, or application correctness.
- `git diff --check` passed for the deployment paths (new untracked files are not covered by that check).
- Nine automated tests passed after migrating to Flask and Jinja templates, including insight cache reuse, rendered pages, JSON errors and Retry-After propagation. A live GitHub metrics request through Flask returned HTTP 200 with four closed issues.
- The automated tests cover covering HTTP validation, cache reuse/expiry, PR exclusion, closing identity, date boundaries, scan truncation, rate limiting, and numeric evidence rejection.
- Live GitHub collection for pallets/flask, 2026-10-20 through 2026-11-03 exclusive, returned four closed issues with source links. The local Qwen insight endpoint returned HTTP 200 with three valid evidence references.
- Live Qwen output can still include unsupported prose (for example, suggesting multiple repositories when only one was queried). Numeric evidence validation does not detect this. Treat narrative and root-cause hypotheses as unverified suggestions, not established facts; stronger models and prose evaluation are follow-up work.
- This is a local demo service, not an Internet-hardened multi-tenant deployment: no application authentication or distributed rate limiting. Keep loopback bindings; add these controls before external exposure.

## Future work

With another day: run a real Docker smoke test once the daemon is available, add model prose-quality evaluations and adversarial tests, expand reopened-issue/concurrency tests, and add structured request logging and upstream health diagnostics. For scale, use a background ingestion job and distributed cache-miss coordination rather than holding HTTP requests and process-wide locks. Consider historical event ingestion, bots separated from humans, richer review/merge signals, rate-budget scheduling, cache migrations, and immutable container/model versions. Keep errors explicit rather than substituting invented insights.
