# LangGraph Chatbot

A deliberately small chatbot used as the app for Cloudino.

- **LangGraph** runs a one-node graph (`START -> chatbot -> END`) that calls **OpenAI** with the official `openai` SDK.
- **Postgres** stores the conversation memory (LangGraph's checkpointer), so chats survive restarts and redeploys.
- **Redis** rate-limits each conversation and counts total messages.
- **Flask + gunicorn** serve a JSON API and a tiny chat page.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Liveness. Never touches Postgres, Redis or OpenAI. Use this for ALB and ECS health checks. |
| GET | `/health/ready` | Readiness. Checks Postgres and Redis, returns 503 if either is down. |
| GET | `/` | App name, a message, and `version` (the git SHA baked in at build time). |
| GET | `/chat` | A simple chat page in the browser. |
| POST | `/chat` | `{"message": "hi", "thread_id": "optional"}` returns `{"thread_id", "reply"}`. Reuse the `thread_id` to continue a conversation. |
| GET | `/history/<thread_id>` | The saved conversation from Postgres. |
| GET | `/stats` | Total messages answered (from Redis). |

## Configuration

| Variable | Secret? | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes | OpenAI access |
| `DATABASE_URL` | Yes (contains the password) | Postgres connection |
| `REDIS_URL` | Yes if Redis has a password | Redis connection |
| `OPENAI_MODEL` | No | Default `gpt-4o-mini`. Set any chat model your key can use. |
| `RATE_LIMIT_PER_MIN` | No | Messages per conversation per minute, default 10 |
| `LOG_LEVEL` | No | Default `INFO` |
| `APP_VERSION` | No | Set at image build time from the git SHA |
| `FORCE_UNHEALTHY` | No | Set to `1` only for the rollback drill: `/health` returns 503 |

## Run it locally (before you write the Compose file)

```bash
uv sync
cp .env.example .env          # then put your real OpenAI key in .env

# temporary Postgres and Redis containers
docker run -d --name demo-pg -e POSTGRES_USER=app -e POSTGRES_PASSWORD=change-me \
  -e POSTGRES_DB=app -p 5432:5432 postgres:16
docker run -d --name demo-redis -p 6379:6379 redis:7

set -a; source .env; set +a
uv run gunicorn --bind 0.0.0.0:8000 app.main:app
# open http://localhost:8000/chat
```

Remove the temporary containers once your `docker-compose.yml` replaces them: `docker rm -f demo-pg demo-redis`.

## Try it with curl

```bash
curl http://localhost:8000/health/ready
curl -X POST -H "Content-Type: application/json" \
  -d '{"message":"My name is Sol","thread_id":"demo"}' http://localhost:8000/chat
curl -X POST -H "Content-Type: application/json" \
  -d '{"message":"What is my name?","thread_id":"demo"}' http://localhost:8000/chat
curl http://localhost:8000/history/demo
```

The second answer knows your name because LangGraph loaded the saved history from Postgres.

## Tests

```bash
uv run ruff check .
uv run pytest -q
```

The tests never call OpenAI (a fake replaces it). The unit tests run anywhere. The integration test runs only when `DATABASE_URL` and `REDIS_URL` are set, which is how CI runs it against real Postgres and Redis service containers.

## What you build next (the assignment)

These are intentionally not included, so you write them yourself:

- `Dockerfile` and `.dockerignore` (runbook Step 2). Start command: `gunicorn --bind 0.0.0.0:8000 app.main:app`. Copy the whole `app/` folder, it includes `templates/`.
- `docker-compose.yml` with three services, `api`, `db` (postgres:16 + named volume) and `redis` (redis:7 + named volume). Inside Compose the URLs use service names: `postgresql://app:...@db:5432/app` and `redis://redis:6379/0`.
- The CI workflow: add a `redis:7` service next to Postgres and set `REDIS_URL`. No OpenAI key is needed in CI.
- AWS: `OPENAI_API_KEY`, `DATABASE_URL` and `REDIS_URL` go in Secrets Manager. Redis needs a home in AWS too (ElastiCache in the private subnets).
