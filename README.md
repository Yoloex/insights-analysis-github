# GitHub issue metrics

Flask service for repository issue-closure metrics and grounded, locally generated insights.

## Prerequisites

- Python >= 3.12
- Ollama >= 0.40.11
- Docker

Local Ollama has `qwen3.5:0.8b` available. Docker Compose configuration validates.

See [NOTES.md](NOTES.md) for architecture, trade-offs, AI usage, and a ten-minute demo outline.

## How to Run

From this directory, use Bash (Linux/macOS, WSL, or Git Bash on Windows). `python3` must refer to Python 3.12+; use `python` instead if that is the available command on your system:

### Install Dependencies

```bash
[ -f .env ] || cp .env.example .env
python3 -m venv .venv
source .venv/bin/activate

python -m pip install .
mkdir -p data
ollama show qwen3.5:0.8b >/dev/null 2>&1 || ollama pull qwen3.5:0.8b
```

### Serve

```bash
waitress-serve --host=127.0.0.1 --port=8000 --call app.main:create_app
```

Run the test suite and linter with the virtual environment activated as above:

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check app tests
```

## Docker

```bash
# Copy only if .env does not already exist.
[ -f .env ] || cp .env.example .env
docker info
docker compose --env-file .env -f docker/docker-compose.yaml config --quiet
docker compose --env-file .env -f docker/docker-compose.yaml up --build -d
docker compose --env-file .env -f docker/docker-compose.yaml ps -a
docker compose --env-file .env -f docker/docker-compose.yaml logs ollama api
```

The SQLite cache remains in the separate named `cache-data` volume and survives container recreation and `docker compose --env-file .env -f docker/docker-compose.yaml down`. Adding `--volumes` deletes that analytics cache, not the model baked into the image.

## Configuration

| Variable | Local default | Compose behavior |
| --- | --- | --- |
| `GITHUB_TOKEN` | Optional | Passed through; keep private |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Internal `http://ollama:11434` |
| `OLLAMA_MODEL` | `qwen3.5:0.8b` | Fixed to the same Qwen model |
| `DATABASE_PATH` | `data/cache.sqlite3` | `/app/data/cache.sqlite3` on persistent volume |
| `CACHE_TTL_SECONDS` | `900` | Configurable, default `900` |

## API contract

- `GET /`: browser UI.
- `GET /health`: health endpoint.
- `GET /docs`: API documentation.
- `GET /api/metrics?repo=owner/name&start=YYYY-MM-DD&end=YYYY-MM-DD`: issue metrics.
- `POST /api/insights` with the **same query parameters**, not a JSON body: grounded insights.

Dates define a start-inclusive, end-exclusive interval of at most 90 days. Metrics count closed issues, excluding pull requests, grouped by the closing identity (`closed_by`), not the issue author. GitHub issues are scanned in descending `updated` order; each qualifying closed issue requires a detail request to identify its closer. Bounded pagination must fail explicitly on truncation rather than report partial data as complete.

Insights require strict structured model output referencing metric IDs, followed by validation. Model errors or invalid references must surface as errors; there is no fabricated fallback narrative. SQLite caches results persistently with a default 900-second TTL.

After startup, smoke-test in a separate Bash terminal:

```bash
curl --fail-with-body --silent --show-error 'http://localhost:8000/health'
query='repo=octocat/Hello-World&start=2026-10-01&end=2026-10-08'
curl --fail-with-body --silent --show-error "http://localhost:8000/api/metrics?$query"
curl --fail-with-body --silent --show-error -X POST "http://localhost:8000/api/insights?$query"
# Open http://localhost:8000/docs in your browser.
```

Public GitHub access is rate-limited; authentication is recommended. Private repositories require a token with appropriate repository access. See [NOTES.md](NOTES.md) for interpretation limits and validation status.
