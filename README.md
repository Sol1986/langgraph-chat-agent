# LangGraph Chatbot

A deliberately small chatbot used as the app for Cloudino.

- **LangGraph** runs a one-node graph (`START -> chatbot -> END`) that calls **OpenAI** with the official `openai` SDK.
- **Postgres** stores the conversation memory (LangGraph's checkpointer), so chats survive restarts and redeploys.
- **Redis** rate-limits each conversation and counts total messages.
- **FastAPI + uvicorn** serve the JSON API and health endpoints.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/health` | Liveness. Never touches Postgres, Redis or OpenAI. Use this for ALB and ECS health checks. |
| GET | `/health/ready` | Readiness. Checks Postgres and Redis, returns 503 if either is down. |
| GET | `/` | App name, a message, and `version` (the git SHA baked in at build time). |
| POST | `/chat` | `{"message": "hi", "thread_id": "optional"}` returns `{"thread_id", "reply"}`. Reuse the `thread_id` to continue a conversation. |
| GET | `/history/<thread_id>` | The saved conversation from Postgres. |
| GET | `/stats` | Total messages answered (from Redis). |
| GET | `/docs` | FastAPI's interactive API docs, handy for trying `/chat` in a browser. |

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
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | Key is secret | Optional LangSmith tracing of graph runs |

## Run it locally

Docker Compose starts the API, Postgres and Redis together ([compose.yaml](compose.yaml)):

```bash
cp .env.example .env          # then put your real OpenAI key in .env
docker compose up --build
# open http://localhost:8000/docs
```

Compose publishes Postgres on host port `5433` (host `5432` is taken by a native Postgres) and Redis on `6379`.

To run the API outside Docker against those same containers:

```bash
uv sync
docker compose up -d postgres redis
export OPENAI_API_KEY=sk-...
export DATABASE_URL=postgresql://app:change-me@localhost:5433/app
export REDIS_URL=redis://localhost:6379/0
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

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
uv run pytest -q tests/unit/
uv run pytest -q tests/integration/   # needs DATABASE_URL and REDIS_URL
```

Run the two folders separately, as CI does. Both contain a `test_app.py`, and pytest refuses to collect two same-named test modules in one run.

The tests never call OpenAI (a fake replaces it). The unit tests run anywhere. The integration test runs only when `DATABASE_URL` and `REDIS_URL` are set, which is how CI runs it against real Postgres and Redis started with `docker compose`.

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

## AWS Production Architecture

The production application runs on AWS using ECS Fargate behind an Application Load Balancer.

```text
Developer
    |
    v
GitHub
    |
    v
GitHub Actions
    |
    +--> Ruff + Pytest
    +--> Docker Build
    +--> Trivy Scan
    |
    v
GitHub OIDC
    |
    v
Amazon ECR
Private repository
Immutable Git SHA images
    |
    v
Application Load Balancer
    |
    v
ECS Fargate
langgraph-chatbot-api
    |
    +--> Amazon RDS PostgreSQL
    |
    +--> ElastiCache Serverless Valkey
    |
    +--> AWS Secrets Manager
    |
    +--> CloudWatch Logs
```

The Application Load Balancer is the public entry point. The ECS application container accepts application traffic from the ALB rather than unrestricted internet traffic.

The ALB uses `/health` to determine whether an ECS task is healthy before sending traffic to it.

### AWS Services

| Service | Purpose |
|---|---|
| Amazon ECR | Private Docker image registry |
| Amazon ECS Fargate | Runs the application container |
| Application Load Balancer | Public HTTP entry point and health checking |
| Amazon RDS PostgreSQL | Persistent LangGraph conversation/checkpoint storage |
| Amazon ElastiCache Serverless Valkey | Redis-compatible runtime state and rate limiting |
| AWS Secrets Manager | Stores production secrets |
| Amazon CloudWatch | Container/application logging |
| AWS IAM | Controls GitHub and ECS permissions |
| Amazon VPC | Network isolation and security groups |

## Secrets and Configuration

Production configuration is kept outside the Docker image.

Secrets stored in AWS Secrets Manager:

- `OPENAI_API_KEY`
- `DATABASE_URL`

Non-secret runtime configuration can be supplied directly through the ECS task definition. `REDIS_URL` is currently configuration because the deployed ElastiCache endpoint does not contain a password.

ECS retrieves secrets at task startup using its execution role. Secrets are therefore not stored in the Docker image or committed to Git.

GitHub Actions authenticates to AWS using GitHub OIDC instead of long-lived AWS access keys.

## Network Security

The application uses security groups to restrict communication between components.

```text
Internet
   |
 HTTP
   v
ALB Security Group
   |
 TCP 8000
   v
ECS Security Group
   |
   +---- TCP 5432 ----> RDS PostgreSQL
   |
   +---- TCP 6379 ----> ElastiCache Valkey
```

The ECS application port is not intended to be directly exposed to unrestricted internet traffic. The ALB is the application entry point.

RDS and ElastiCache accept traffic from the application security group rather than from the public internet.

## Health Checks

`GET /health` returns:

```json
{
  "status": "healthy"
}
```

This endpoint intentionally does not call PostgreSQL, Redis, or OpenAI. It verifies that the application process is alive and is used by the ALB/ECS health-check mechanism.

`GET /health/ready` checks the dependencies (Postgres and Redis) for readiness.

An optional `FORCE_UNHEALTHY=1` setting makes `/health` return HTTP 503 for deployment and rollback testing.

## CloudWatch Logging

ECS uses the `awslogs` log driver. Application logs are sent to `/ecs/langgraph-chatbot-api`.

CloudWatch logs were used during deployment troubleshooting to diagnose issues including:

- missing environment variables
- Redis TLS configuration
- PostgreSQL authentication
- PostgreSQL SSL configuration
- OpenAI authentication

This allows application failures to be investigated without connecting directly to the running container.

## Docker Compose and Persistence

Local development uses Docker Compose to run `api`, `postgres` and `redis`.

PostgreSQL uses a named Docker volume (`pgdata`) so its data exists independently of the lifecycle of the PostgreSQL container.

Persistence was validated by:

1. Creating a test table and inserting a row.
2. Confirming the row existed.
3. Stopping and removing the PostgreSQL container.
4. Recreating the PostgreSQL container without deleting the volume.
5. Querying the table again.
6. Confirming the original row still existed.

This demonstrates that database data survives container recreation.

## CI Failure Protection

The CI pipeline was deliberately tested with a failing unit test:

```python
def test_intentional_ci_failure():
    assert False, "Intentional failure to demonstrate CI blocking deployment"
```

Pytest failed as expected, causing the CI job to fail and preventing the deployment job from running.

The intentional failure was then removed and a new commit was pushed. The tests passed and the normal build, scan, ECR push, and ECS deployment process completed successfully.

This demonstrates that application changes must pass validation before deployment.

## Image Versioning

Docker images are tagged using the Git commit SHA, for example:

```text
<account-id>.dkr.ecr.us-west-2.amazonaws.com/langgraph-chatbot:<git-sha>
```

The ECR repository uses immutable tags. An existing SHA tag cannot be overwritten.

This provides traceability:

```text
Git commit
    |
    v
Docker image SHA tag
    |
    v
ECR image
    |
    v
ECS task definition revision
    |
    v
Production deployment
```

A deployed application version can therefore be traced back to the source commit that produced it.

## Deployment Strategy

Only pushes to `main` execute the deployment job. Pull requests execute validation but do not deploy.

The deployment process creates a new ECS task definition revision containing the new immutable image.

ECS performs a rolling deployment. The new task is started and evaluated by the load balancer health check before the previous task is removed. This reduces downtime during normal releases.

## Rollback Procedure

Rollback does not require rebuilding an old Docker image. Each ECS task definition revision references an existing immutable image stored in ECR.

A rollback was demonstrated by changing the ECS service from `langgraph-chatbot-api:12` to the previous known-good revision `langgraph-chatbot-api:11`.

The service deployed revision 11 using its existing image. The application and health endpoint were verified after rollback. The service was then restored to revision 12.

```text
Revision 12
    |
    | rollback
    v
Revision 11
    |
    | ALB health check
    v
Healthy
```

No Docker image was rebuilt during rollback.

## CPU and Memory Sizing

The ECS Fargate task currently uses:

```text
CPU:    0.5 vCPU
Memory: 1 GB
```

This is appropriate for the current small API workload because the application primarily coordinates network calls to OpenAI, PostgreSQL, and Redis rather than performing heavy local computation.

CloudWatch metrics can be used to monitor CPU and memory utilization.

As traffic increases, the task size can be increased or the ECS service can run multiple tasks. Horizontal scaling would allow multiple Fargate tasks to serve requests behind the ALB.

## Staging to Production Promotion

A production system can use separate staging and production ECS services.

The important principle is that the application should not be rebuilt for production.

```text
Commit
   |
   v
CI Tests
   |
   v
Build image once
   |
   v
ECR
   |
   v
Deploy SHA image to staging
   |
   v
Validate
   |
   v
Promote SAME SHA image
   |
   v
Production
```

Promoting the exact same immutable image reduces the risk that production runs code different from the version that was tested in staging.

## Cost Considerations

The architecture uses managed AWS services to reduce infrastructure administration.

Primary cost areas include:

- ECS Fargate compute
- Application Load Balancer
- RDS PostgreSQL
- ElastiCache Serverless
- CloudWatch logs
- ECR storage and image transfer

The current ECS task is intentionally small at 0.5 vCPU and 1 GB because the workload is lightweight.

For a larger production workload, scaling decisions should be based on CloudWatch CPU, memory, request volume, latency, and application requirements.

Unused AWS resources should be removed after testing to avoid unnecessary charges.

## Security Decisions

Key security controls include:

- No AWS access keys committed to Git.
- GitHub Actions uses AWS OIDC.
- Production application secrets are stored in AWS Secrets Manager.
- ECR is private.
- Docker images use immutable Git SHA tags.
- The application runs as a non-root container user.
- Security groups restrict service-to-service traffic.
- RDS is not intended to be publicly exposed.
- ElastiCache is accessed from within the VPC.
- ECS application traffic enters through the ALB.
- IAM roles are separated by responsibility.
- GitHub receives deployment permissions separately from the ECS execution role.

## Troubleshooting

### Application works locally but not in ECS

Check the ECS task logs in CloudWatch and verify environment variables and Secrets Manager references.

### ECS cannot read a secret

Verify that the ECS execution role has `secretsmanager:GetSecretValue` for the required secret ARN.

If the Secrets Manager value is JSON, the ECS task definition must reference the required JSON key rather than injecting the entire JSON document.

### PostgreSQL connection fails

Check:

- `DATABASE_URL`
- username/password
- RDS security group
- ECS-to-RDS network access
- SSL requirements

### Redis connection fails

Check:

- `REDIS_URL`
- ElastiCache security group
- port 6379
- TLS configuration

The production ElastiCache deployment requires `rediss://` rather than `redis://`.

### OpenAI returns HTTP 401

Verify that ECS receives only the OpenAI API key value from Secrets Manager rather than the complete JSON secret.

### ALB reports unhealthy targets

Check:

- `/health`
- container port 8000
- target group configuration
- ECS security group
- ALB security group
- CloudWatch application logs

### ECR rejects an image push

The repository uses immutable tags. Re-running a deployment for the exact same Git SHA can attempt to push an image tag that already exists.

A new commit produces a new SHA and therefore a new immutable image tag.

## Validation Evidence

The following evidence should be included with the submission:

| Validation | Evidence |
|---|---|
| Local Docker build | Successful Docker build and image listing |
| Local application | `/health` HTTP 200 |
| Docker Compose | `docker compose ps` showing required services |
| Persistence | PostgreSQL row before and after container recreation |
| CI failure | Failed GitHub Actions run and skipped deployment |
| CI success | Successful GitHub Actions deployment |
| ECR | Image tagged with Git commit SHA |
| ECS deployment | Healthy ECS service/task revision |
| ALB | Healthy target and working `/health` |
| Production API | Successful `/chat` request |
| Logging | CloudWatch application logs |
| Secrets | ECS task definition using Secrets Manager |
| Rollback | ECS service running previous task definition revision |

## Production Validation

Production was validated through the Application Load Balancer.

The health endpoint returned HTTP 200 and the production `/chat` endpoint successfully returned a LangGraph/OpenAI response.

The complete production request path is:

```text
Client
  |
  v
Application Load Balancer
  |
  v
ECS Fargate
  |
  v
FastAPI / LangGraph
  |
  +--> RDS PostgreSQL
  +--> ElastiCache Valkey
  +--> OpenAI
  |
  v
Response
```
