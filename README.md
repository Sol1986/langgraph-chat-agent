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

## Deploying to AWS (CI/CD)

Every push to `main` deploys automatically through [.github/workflows/ci-cd.yml](.github/workflows/ci-cd.yml). Pull requests run only the lint and test job.

```text
Push to main
    ↓
Lint + Tests
    ↓
Docker Build
    ↓
Trivy Scan
    ↓
Push SHA image → ECR
    ↓
Download current ECS task definition
    ↓
Replace image with new SHA image
    ↓
Register new task definition revision
    ↓
Update ECS service
    ↓
ECS starts new container
    ↓
ALB health check
    ↓
New version live ✅
```

What each stage does:

1. **Lint + Tests**: `ruff check`, unit tests, then integration tests against Postgres and Redis started with `docker compose`. If anything fails, nothing is deployed.
2. **Docker Build**: builds the image and tags it with the git commit SHA, so every deployed version traces back to one commit.
3. **Trivy Scan**: scans the image and fails the pipeline on any fixable `CRITICAL` or `HIGH` vulnerability.
4. **Push SHA image → ECR**: pushes the scanned image to Amazon ECR. GitHub Actions signs in to AWS with OIDC (`AWS_ROLE_TO_ASSUME`), so no long-lived AWS keys are stored in GitHub.
5. **Download current ECS task definition**: fetches the live `langgraph-chatbot-api` task definition from AWS.
6. **Replace image with new SHA image**: swaps only the container image, keeping the existing secrets, ports, and settings.
7. **Register new task definition revision**: saves the result as a new revision, so earlier revisions stay available for rollback.
8. **Update ECS service**: points the `langgraph-chatbot-api` service in the `langgraph-chatbot-prod` cluster at the new revision.
9. **ECS starts new container**: launches a task from the new image.
10. **ALB health check**: the load balancer checks `/health` and sends traffic to the new task only once it passes. The pipeline waits for the service to become stable.
11. **New version live ✅**: the old task is drained and stopped.

GitHub secrets the pipeline uses: `AWS_ROLE_TO_ASSUME`, `AWS_REGION`, `AWS_ECR_REPOSITORY` (deploy job) and `OPENAI_API_KEY` (integration tests).

## What you build next (the assignment)

These are intentionally not included, so you write them yourself:

- `Dockerfile` and `.dockerignore` (runbook Step 2). Start command: `gunicorn --bind 0.0.0.0:8000 app.main:app`. Copy the whole `app/` folder, it includes `templates/`.
- `docker-compose.yml` with three services, `api`, `db` (postgres:16 + named volume) and `redis` (redis:7 + named volume). Inside Compose the URLs use service names: `postgresql://app:...@db:5432/app` and `redis://redis:6379/0`.
- The CI workflow: add a `redis:7` service next to Postgres and set `REDIS_URL`. No OpenAI key is needed in CI.
- AWS: `OPENAI_API_KEY`, `DATABASE_URL` and `REDIS_URL` go in Secrets Manager. Redis needs a home in AWS too (ElastiCache in the private subnets).
