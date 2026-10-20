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

---

### Kubernetes: Deploy, Scale & Operate Containerized Applications

### 1. Project Overview

This project deploys the containerized LangGraph Chatbot API to Kubernetes.

The application was deployed to a local Kubernetes cluster using kind. The implementation demonstrates Kubernetes deployments, services, configuration management, secrets, health probes, scaling, rolling updates, rollback, persistent storage, troubleshooting, and security.

The application uses:

- FastAPI application
- PostgreSQL
- Redis
- Docker
- Kubernetes
- kind
- kubectl

---

### 2. Architecture

Application flow:

User  
→ Local port-forward  
→ Kubernetes ClusterIP Service  
→ LangGraph Chatbot API Pods  
→ PostgreSQL / Redis

```mermaid
flowchart TB
    User["User / Client"]

    subgraph Kind["kind Kubernetes Cluster"]
        Service["ClusterIP Service<br/>langgraph-chatbot-api"]

        subgraph Deployment["API Deployment"]
            Pod1["API Pod<br/>FastAPI"]
            Pod2["API Pod<br/>FastAPI"]
        end

        Config["ConfigMap"]
        Secret["Secret"]

        RedisService["Redis Service"]
        Redis["Redis Pod"]

        PostgresService["PostgreSQL Service"]
        Postgres["PostgreSQL Pod"]
        PVC["PersistentVolumeClaim"]
        PV["PersistentVolume"]
    end

    User -->|"kubectl port-forward<br/>localhost:8080"| Service

    Service --> Pod1
    Service --> Pod2

    Config --> Pod1
    Config --> Pod2
    Secret --> Pod1
    Secret --> Pod2

    Pod1 --> RedisService
    Pod2 --> RedisService
    RedisService --> Redis

    Pod1 --> PostgresService
    Pod2 --> PostgresService
    PostgresService --> Postgres

    Postgres --> PVC
    PVC --> PV
```

The API is managed by a Kubernetes Deployment with two replicas.

The Service provides a stable endpoint and routes traffic to ready API Pods using Kubernetes labels.

PostgreSQL and Redis are available to the API through their own Kubernetes Services.

---

### 3. Kubernetes Environment

Local Kubernetes environment:

- Kubernetes tool: kind
- Cluster name: `cloudino`
- Namespace: `cloudino-devops`
- Kubernetes node: `cloudino-control-plane`

Cluster verification:

```powershell
kubectl cluster-info --context kind-cloudino
kubectl get nodes
```

The application resources are isolated inside:

```text
cloudino-devops
```

---

### 4. Kubernetes Resources

The project includes Kubernetes manifests for:

- Namespace
- API Deployment
- API Service
- ConfigMap
- Secret
- PostgreSQL Deployment and Service
- Redis Deployment and Service
- PostgreSQL PersistentVolumeClaim
- Troubleshooting test workloads

The API Deployment normally maintains:

```yaml
replicas: 2
```

Kubernetes continuously reconciles the actual state of the cluster with this desired state.

---

### 5. Docker Image

The application Docker image is:

```text
langgraph-chatbot-demo-api:v2
```

The image was loaded into the local kind cluster using:

```powershell
kind load docker-image langgraph-chatbot-demo-api:v2 --name cloudino
```

The application runs as a non-root user:

```dockerfile
RUN useradd --system --no-create-home --uid 10001 appuser
USER appuser
```

The application port is configurable:

```dockerfile
ENV PORT=8000
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
```

---

### 6. Deployment and Resource Management

The API is managed by a Kubernetes Deployment.

CPU and memory requests and limits are configured:

```yaml
resources:
  requests:
    cpu: "100m"
    memory: "128Mi"
  limits:
    cpu: "500m"
    memory: "512Mi"
```

Requests help Kubernetes schedule the Pod onto a suitable node.

Limits prevent the container from consuming unlimited resources.

---

### 7. Kubernetes Networking

The API is exposed internally using a ClusterIP Service:

```text
langgraph-chatbot-api
```

The Service selects Pods using:

```yaml
selector:
  app: langgraph-chatbot
```

Individual Pod IP addresses are not used by clients because Pods can be destroyed and recreated with different IP addresses.

Kubernetes Services provide stable networking while dynamically discovering matching Pods.

Local access was demonstrated using:

```powershell
kubectl port-forward service/langgraph-chatbot-api 8080:8000 -n cloudino-devops
```

The application was then tested using:

```powershell
Invoke-RestMethod http://localhost:8080/health
```

Example response:

```text
status   version
------   -------
healthy  v2
```

Pod replacement was also tested. One API Pod was manually deleted, the Deployment automatically created a replacement, and the Service remained available.

Within the namespace, Kubernetes DNS also allows workloads to communicate using Service names such as:

```text
postgres
redis
langgraph-chatbot-api
```

---

### 8. ConfigMap and Secret

Non-sensitive application configuration is stored in a ConfigMap.

Examples include:

```text
REDIS_URL
APP_ENV
```

Sensitive configuration is stored using a Kubernetes Secret.

Example:

```text
DATABASE_PASSWORD
```

The Deployment references these resources and injects their values into the application container at runtime.

This keeps runtime configuration separate from the Docker image.

The demonstration Secret uses a fake development credential. Real production credentials should not be committed to Git. Kubernetes Secret values encoded using base64 should not be considered encrypted simply because they are encoded.

---

### 9. Health Checks

The application provides separate liveness and readiness endpoints.

Liveness:

```text
/health
```

Readiness:

```text
/health/ready
```

The Kubernetes Deployment uses:

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000

livenessProbe:
  httpGet:
    path: /health
    port: 8000
```

Liveness determines whether the application container should continue running.

Readiness determines whether the Pod should receive Service traffic.

A readiness failure was intentionally tested. The container remained running but the affected Pod was not considered ready for Service traffic.

---

### 10. Scaling

The API Deployment was initially configured with two replicas.

Manual scaling was tested using:

```powershell
kubectl scale deployment langgraph-chatbot-api --replicas=4 -n cloudino-devops
```

Kubernetes created additional Pods until four replicas were available.

The Deployment was then scaled back to two:

```powershell
kubectl scale deployment langgraph-chatbot-api --replicas=2 -n cloudino-devops
```

The Service automatically discovered the additional ready Pods without requiring networking configuration changes.

---

### 11. Rolling Updates and Rollback

The Deployment uses the following rolling update strategy:

```yaml
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0
    maxSurge: 1
```

The application was upgraded from:

```text
v1 → v2
```

The v2 `/health` response includes the application version so the successful update could be verified.

The rollout was monitored using:

```powershell
kubectl rollout status deployment/langgraph-chatbot-api -n cloudino-devops
```

Rollout history was inspected using:

```powershell
kubectl rollout history deployment/langgraph-chatbot-api -n cloudino-devops
```

#### Failed Deployment

A nonexistent image was intentionally deployed:

```text
langgraph-chatbot-demo-api:v999
```

The new Pod entered:

```text
ImagePullBackOff
```

The existing healthy v2 Pods remained available because the failed replacement could not become ready.

The failed rollout was recovered using:

```powershell
kubectl rollout undo deployment/langgraph-chatbot-api -n cloudino-devops
```

The Deployment successfully returned to v2.

---

### 12. Persistent Storage

PostgreSQL was configured with a PersistentVolumeClaim:

```text
postgres-data
```

The PVC requests:

```text
1Gi
```

The local kind cluster uses the default StorageClass:

```text
standard
```

with the local-path provisioner.

Kubernetes dynamically provisioned a PersistentVolume and bound it to the PostgreSQL PVC.

PostgreSQL mounts the persistent storage at:

```text
/var/lib/postgresql/data
```

Persistence was tested by:

1. Creating a test table in PostgreSQL.
2. Inserting test data.
3. Deleting the PostgreSQL Pod.
4. Allowing the Deployment to create a replacement Pod.
5. Querying the database from the replacement Pod.

The test data remained available.

This demonstrates that the Pod itself is disposable while persistent database data remains on persistent storage.

---

### 13. Troubleshooting Exercises

Several Kubernetes failures were intentionally created and diagnosed.

#### CrashLoopBackOff

A container was configured to exit with code 1.

Diagnosis:

```powershell
kubectl get pods -n cloudino-devops
kubectl describe pod crash-test -n cloudino-devops
kubectl logs crash-test -n cloudino-devops
kubectl logs crash-test -n cloudino-devops --previous
```

Root cause:

```text
Application process exited with code 1.
```

Kubernetes repeatedly restarted the container, eventually producing `CrashLoopBackOff`.

#### ImagePullBackOff

A nonexistent image tag was deployed:

```text
langgraph-chatbot-demo-api:v999
```

`kubectl describe pod` showed that Kubernetes could not obtain the requested image.

The Deployment was recovered using rollback.

#### Service Unreachable

The API Service selector was intentionally changed to:

```yaml
app: wrong-app
```

The API Pods remained healthy, but the Service had no matching endpoints.

Diagnosis included:

```powershell
kubectl describe service langgraph-chatbot-api -n cloudino-devops
kubectl get endpointslices -n cloudino-devops
kubectl get pods -n cloudino-devops --show-labels
```

The selector was restored to:

```yaml
app: langgraph-chatbot
```

#### Readiness Probe Failure

The readiness path was temporarily changed to an invalid endpoint.

The affected Pods showed:

```text
0/1 Running
```

The application process was running, but Kubernetes did not consider the Pod ready for traffic.

The correct readiness endpoint was restored:

```text
/health/ready
```

#### Invalid Deployment Configuration

An invalid value was accidentally configured:

```text
periodSeconds: "10 3"
```

Kubernetes rejected the Deployment update.

The manifest was corrected and validated before being reapplied.

#### Insufficient Resources

A test Pod requested:

```yaml
cpu: "100"
```

The Pod remained:

```text
Pending
```

`kubectl describe pod` reported a scheduling failure due to insufficient CPU.

The test workload was then removed.

---

### 14. Security

The API container runs as a non-root user:

```text
UID 10001
```

This was configured in the Dockerfile and verified from the running Kubernetes Pod.

The implementation also uses:

- Kubernetes Secrets for sensitive runtime configuration
- ConfigMaps for non-sensitive configuration
- CPU and memory requests/limits
- Namespace organization
- No AWS credentials embedded in the application image
- No direct exposure of individual Pods

Kubernetes RBAC should follow the principle of least privilege so workloads and users only receive the Kubernetes API permissions they require.

---

### 15. AWS / Amazon EKS Production Design

The local kind environment demonstrates the Kubernetes implementation.

For a production AWS deployment, the architecture would map approximately as follows:

| Local Implementation | AWS Production |
|---|---|
| kind | Amazon EKS |
| Local Docker images | Amazon ECR |
| `kubectl port-forward` | AWS load balancing / ingress |
| local-path StorageClass | AWS-backed persistent storage where required |
| PostgreSQL Pod + PVC | Amazon RDS should be considered |
| Kubernetes Secret | AWS Secrets Manager integration |
| Local/static credentials | IAM/workload identity |
| Manual deployment | CI/CD pipeline |

The CI/CD flow would become:

```text
GitHub
→ GitHub Actions
→ Build Docker image
→ Push image to Amazon ECR
→ Update EKS Deployment
→ Kubernetes rolling update
```

Production PostgreSQL would preferably be evaluated as an Amazon RDS workload rather than automatically operating the database inside EKS.

The local PVC implementation exists to demonstrate Kubernetes persistent storage concepts.

---

### 16. Key Kubernetes Commands

```powershell
# Cluster
kubectl cluster-info
kubectl get nodes

# Workloads
kubectl get pods -n cloudino-devops
kubectl get deployments -n cloudino-devops

# Networking
kubectl get services -n cloudino-devops
kubectl get endpointslices -n cloudino-devops

# Troubleshooting
kubectl describe pod <pod-name> -n cloudino-devops
kubectl logs <pod-name> -n cloudino-devops
kubectl logs <pod-name> -n cloudino-devops --previous

# Scaling
kubectl scale deployment langgraph-chatbot-api --replicas=4 -n cloudino-devops

# Rollouts
kubectl rollout status deployment/langgraph-chatbot-api -n cloudino-devops
kubectl rollout history deployment/langgraph-chatbot-api -n cloudino-devops
kubectl rollout undo deployment/langgraph-chatbot-api -n cloudino-devops

# Storage
kubectl get storageclass
kubectl get pvc -n cloudino-devops
kubectl get pv

# Local application access
kubectl port-forward service/langgraph-chatbot-api 8080:8000 -n cloudino-devops
```

---

### 17. Conclusion

This containerized the application to Kubernetes and demonstrated the operational capabilities provided by Kubernetes.

The implementation demonstrated:

- Declarative application deployment
- Self-healing Pods
- Stable Service networking
- Configuration and secret management
- Liveness and readiness probes
- Horizontal scaling
- Rolling updates
- Deployment rollback
- Persistent storage
- Kubernetes troubleshooting
- Container security
- Preparation for Amazon EKS deployment

The main operational difference from the previous Docker-based deployment is that Kubernetes continuously manages the application's desired state. Instead of manually managing individual containers, Deployments, Services, probes, configuration, storage, and other Kubernetes resources define how the application should run and Kubernetes works to maintain that state.
