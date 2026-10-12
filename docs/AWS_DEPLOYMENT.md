# AWS deployment

> **Status: not deployed.** The configuration reuses existing console-created resources (VPC, RDS, security groups, S3 bucket and gateway endpoint). `serverless.yml` was validated offline only: `serverless package` with stubbed SSM values, then `cfn-lint` on the compiled template (see [TESTING.md](TESTING.md)). Nothing here has been run against a real AWS account. Every step below creates billable resources; read the cost section first.

## What the stack creates and what it reuses

The stack **reuses** resources created in the console (IDs are the defaults under `params` in `serverless.yml`; override with `--param`):

| Existing resource | Value | Notes |
|---|---|---|
| Region | `ap-southeast-2` | |
| Subnets | `subnet-0989f4a5aeb932e4c` (2a), `subnet-0a1b2ca704aa0bf06` (2b) | share route table `rtb-0e539496fd0503acb` |
| S3 gateway endpoint | `vpce-0c3c145a9af7bb5c9` | free; lets VPC Lambdas reach S3 without NAT |
| Lambda security group | `sg-0159bd3bd8ad94e18` | egress only |
| RDS security group | `sg-0ff40905231dc8c9a` | 5432 only from the Lambda SG |
| RDS PostgreSQL `database-1` (`db.t4g.micro`, private) | `database-1.cr8qe4miqt96.ap-southeast-2.rds.amazonaws.com` | **bills every hour it exists, even when idle** |
| Upload bucket | `loan-eligibility-engine` | trigger, CORS, lifecycle and policy set in the console (below) |

The stack **creates** only:

| Resource | Approx. cost |
|---|---|
| 3 Lambdas + HTTP API, IAM role | ≈ 0 at demo volume |
| `AWS::Lambda::Permission` allowing the upload bucket to invoke `processCsv` | 0 |
| SES send policy for n8n (when `sesFromAddress` is set); optional SG rule on the n8n SG | 0 |
| CloudWatch log groups (14-day retention) | cents |
| Serverless deployment bucket (holds the Lambda zip) | cents |

`serverless remove` deletes only these. The reused bucket, RDS instance, security groups and endpoint are never modified or deleted by the stack.

Not created by the stack either: the **n8n host**, SES identities, and SSM parameters.
The n8n host (e.g. EC2 `t4g.small`, ≈ US$12/month, or `t4g.micro` if memory allows) also bills while running. Stop it when you aren't demoing.

Deliberately **not** used: NAT gateway (≈ US$32+/month plus data), RDS Proxy (≥ ≈ US$20/month), Secrets Manager interface endpoints (≈ US$7/month per AZ), Multi-AZ RDS.

## Prerequisites (manual)

1. An AWS account and an IAM principal that can deploy CloudFormation, Lambda, API Gateway, S3, RDS, EC2 security groups, IAM roles/policies and SSM. Configure it with `aws configure --profile loan-engine` (never commit keys).
2. Docker running (the Python requirements plugin builds Linux wheels in a container). On Windows, `dockerizePip: non-linux` always uses Docker; a package built from Windows wheels will not import `psycopg`/`pydantic_core` on Lambda.
3. Node 18+ for the Serverless Framework v3 (installed locally via the repo-root `package.json`: `npm install`).
4. Lambda account concurrency (Lambda console → Dashboard → Account-level settings). The dev account's limit is 5, so `processCsv` has no `reservedConcurrency` (reserving would leave nothing for the other functions). All three functions share those 5; more simultaneous executions are throttled (API: HTTP 429; S3-triggered imports are retried by Lambda). Request a quota increase in Service Quotas before restoring a reservation.

## Database and user (existing RDS)

The app connects as `dbUser` to `dbName` (defaults: `postgres` / `postgres`). The password comes from SSM `db-password`. The migrations need only `CREATE TABLE`/`CREATE INDEX` in the `public` schema: no extensions, roles or grants. The `postgres` database always exists on RDS PostgreSQL, so no database has to be created first. Do **not** assume a `loan_engine` database exists; only the local Docker setup creates one.

Verify before deploying:

1. RDS → `database-1` → Configuration: note **Master username** (the console snippet showed `postgres`) and **DB name**. If a DB name was set when the instance was created, you may use it via `--param="dbName=..."`.
2. Connectivity → **Publicly accessible: No**. Security group `sg-0ff40905231dc8c9a` inbound: only TCP 5432 from `sg-0159bd3bd8ad94e18` (no `0.0.0.0/0` or IP ranges).
3. The password in SSM `db-password` must be the current password of `dbUser`. It is embedded in a URL, so it must be URL-safe (letters, digits, `-`, `_`, `.`, `~`). If the current master password has other characters, change it in RDS → Modify → "Master password". That briefly affects anything already connected.
4. After deploy, invoke `migrate` (below). A successful result (`applied_migrations: [...]`) is the proof that host, user, password, database and SSL all work. `password authentication failed` means SSM and RDS disagree; `permission denied for schema public` means the user cannot create tables in that database.

Optional, least privilege: a dedicated `loan_engine` database and login role need a SQL client inside the VPC (RDS is private; the RDS query editor does not support RDS for PostgreSQL). Run this as the master user from such a client, then deploy with `--param="dbUser=loan_engine" --param="dbName=loan_engine"` and put that role's password in `db-password`:
```sql
CREATE ROLE loan_engine LOGIN PASSWORD '<url-safe password>';
CREATE DATABASE loan_engine OWNER loan_engine;
```

SSL: `sslmode=require` encrypts the connection but does not verify the server certificate. For `verify-full`, ship the RDS CA bundle (`global-bundle.pem`) in the package and append `&sslrootcert=...`. That is not done here.

## Secrets (SSM Parameter Store)

Create these in Systems Manager → Parameter Store → Create parameter (Standard tier, free), region `ap-southeast-2`. **All five must exist before `serverless deploy`**; a missing one fails variable resolution.

| Name | Type | Feeds | Value |
|---|---|---|---|
| `/loan-engine/dev/db-password` | SecureString | `DATABASE_URL` | the **existing** password of `dbUser` on `database-1` (not a newly generated one), URL-safe |
| `/loan-engine/dev/internal-api-token` | SecureString | `INTERNAL_API_TOKEN` (`X-Internal-Token` on `/internal/*`) | random, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`; the same value goes in n8n's internal-API credential |
| `/loan-engine/dev/n8n-webhook-token` | SecureString | `N8N_WEBHOOK_TOKEN` (`X-Webhook-Token` sent to n8n) | random; the same value goes in n8n's webhook Header Auth credential |
| `/loan-engine/dev/n8n-matching-webhook-url` | String | `N8N_MATCHING_WEBHOOK_URL` | `http://<n8n private IP>:5678/webhook/loan-matching` |
| `/loan-engine/dev/notification-test-recipient` | String (SecureString also works) | `NOTIFICATION_TEST_RECIPIENT`, the **only** recipient ever used in `ses` mode | an inbox you control, SES-verified while in the sandbox |

`NOTIFICATION_FROM_EMAIL` is not a secret; it comes from `--param="sesFromAddress=..."` (an SES-verified sender). Never put these values in the repo, a committed `.env`, or shell history you share.

The values are resolved at deploy time into Lambda environment variables (encrypted at rest by Lambda/KMS). Anyone with `lambda:GetFunctionConfiguration` can read them, so restrict that permission. To fetch secrets at runtime instead, use `DB_SECRET_ARN` (already supported in `app/config.py`) and add a Secrets Manager VPC endpoint or NAT (extra cost).

## SES

1. **Verify a sender identity:** SES console → Verified identities → Create identity → Email address (or a domain you own). Click the link in the verification email.
2. **Sandbox restrictions:** new accounts are in the SES sandbox. You can send **only to verified addresses**, at most 200 messages per 24 h and 1 message per second. So also verify the test recipient address (`notification-test-recipient`). Workflow C throttles to 1 message per second. Production access (a support request) is **not needed** for this assignment, because we only ever send to the test inbox.
3. **IAM for n8n:** deploy with `--param="sesFromAddress=<verified sender>"`; the stack outputs `N8nSesSendPolicyArn`, a managed policy that allows only `ses:SendEmail`/`ses:SendRawEmail` with `ses:FromAddress` = your sender. Create an IAM user (or an EC2 instance role for the n8n host) with only that policy:
   ```bash
   aws iam create-user --user-name loan-engine-n8n-ses
   aws iam attach-user-policy --user-name loan-engine-n8n-ses --policy-arn <N8nSesSendPolicyArn>
   aws iam create-access-key --user-name loan-engine-n8n-ses   # store in n8n only
   ```
   Put the key in n8n's **AWS** credential (`LoanEngAwsSes001`) with the SES region. With an instance role you can skip keys: choose the instance-role credential option in n8n.
4. Set the backend's `NOTIFICATION_MODE=ses` (already set in `serverless.yml`) and `NOTIFICATION_FROM_EMAIL` = the verified sender.

## Deploy

```bash
npm install                     # Serverless v3 + python requirements plugin (repo root)
npx serverless deploy --stage dev --aws-profile loan-engine \
  --param="frontendOrigin=http://localhost:5173" \
  --param="sesFromAddress=verified-sender@example.com" \
  --param="n8nSecurityGroupId=sg-of-the-n8n-instance"
npx serverless invoke -f migrate --stage dev --aws-profile loan-engine
# optional, demo only - loads SYNTHETIC products labelled as fixtures:
npx serverless invoke -f migrate --stage dev --data '{"seed_dev_fixtures": true}'
```

Serverless v3's config schema predates `python3.12`, so it prints a schema warning. CloudFormation accepts the runtime. (`osls`, the maintained v3 fork, may avoid the warning, but that has not been tested with this config.)

Then configure the existing upload bucket in the S3 console (`loan-eligibility-engine`; the stack does not manage it):

1. **Event notification** (Properties → Event notifications → Create): prefix `uploads/`, suffix `.csv`, event type "All object create events", destination Lambda function `loan-eligibility-engine-dev-processCsv`. Do this **after** deploy; S3 rejects it until the stack's invoke permission exists.
2. **Block Public Access:** all four settings on. **Object Ownership:** Bucket owner enforced.
3. **Default encryption:** SSE-S3. Presigned POSTs also require `x-amz-server-side-encryption: AES256`.
4. **Lifecycle rule:** prefix `uploads/`, expire current versions after 7 days (raw CSVs contain personal data).
5. **CORS** (Permissions → CORS), replacing the origin with your frontend origin:
   ```json
   [{"AllowedOrigins": ["http://localhost:5173"], "AllowedMethods": ["POST"], "AllowedHeaders": ["*"], "MaxAge": 600}]
   ```
6. **Bucket policy**, TLS only (merge into any existing policy instead of replacing it):
   ```json
   {"Version": "2012-10-17", "Statement": [{"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
     "Resource": ["arn:aws:s3:::loan-eligibility-engine", "arn:aws:s3:::loan-eligibility-engine/*"],
     "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}
   ```
7. VPC → Endpoints → `vpce-0c3c145a9af7bb5c9`: route table `rtb-0e539496fd0503acb` is associated, and the endpoint policy allows `s3:GetObject` on this bucket (the default "Full access" policy does).

* Frontend: build with `VITE_API_BASE_URL=<HttpApiUrl> npm run build` and host `frontend/dist` (S3 static website or any static host). Redeploy with `frontendOrigin=<that origin>` so API CORS allows it, and update the bucket CORS origin to match.
* n8n: run the same `docker-compose.yml` `n8n` service on the EC2 instance (or `docker run n8nio/n8n:2.43.3`) with `N8N_ENCRYPTION_KEY` set, `LOAN_API_BASE_URL=<HttpApiUrl>`, `N8N_SELF_BASE_URL=http://localhost:5678`, and `WEBHOOK_URL=http://<private-ip>:5678/`. Don't open 5678 to the internet: the instance SG should allow 5678 only from the Lambda SG (the stack adds that rule when `n8nSecurityGroupId` is given) and SSH from your IP. Open the editor via `ssh -L 5678:localhost:5678 ec2-user@<host>`. Import credentials and workflows as in [N8N_WORKFLOWS.md](N8N_WORKFLOWS.md), using the cloud token values.

## Networking and connection management

* Lambdas use the existing egress-only SG `sg-0159bd3bd8ad94e18` in subnets 2a/2b. RDS SG `sg-0ff40905231dc8c9a` allows 5432 only from it, and the instance must stay "Publicly accessible: No". `sslmode=require` is used.
* No NAT: S3 goes through the gateway endpoint, n8n is reached on its private IP, and nothing else outbound is needed by the Lambdas (the LLM step is disabled in the cloud config; enabling it would require NAT or a proxy).
* Connections: one connection is reused per warm Lambda container; `processCsv` has no reserved concurrency, and the account-wide limit of 5 caps all Lambda executions (and so DB connections from Lambda). **RDS Proxy** is the next step if API concurrency approaches `max_connections` (~80 on `t4g.micro`). It isn't included because it costs more than the database at this size.
* HTTP API has a 30 s integration timeout; see the limit in [OPTIMIZATION.md](OPTIMIZATION.md).

## Verify after deploy (manual)

1. `curl <HttpApiUrl>/health` → `{"status":"ok"}`.
2. Upload `sample-data/users_sample.csv` through the frontend. Check CloudWatch logs for `processCsv` (`batch imported`) and that `matching_status` becomes `TRIGGERED`.
3. Check n8n executions for B and C, and the test inbox.

## Teardown (stop all charges)

```bash
npx serverless remove --stage dev --aws-profile loan-engine
```

* This removes only the Lambdas, API, IAM role, invoke permission and SES policy. Then delete the bucket's event notification in the S3 console, because it points to a function that no longer exists.
* The RDS instance, upload bucket, security groups and S3 endpoint were created in the console and are **not** removed. RDS bills hourly until you delete it (RDS console → Delete; decide whether to keep a final snapshot, which also bills for storage).
* Terminate the n8n EC2 instance and release any Elastic IP.
* Delete the SES IAM user and its access keys, the SSM parameters (`aws ssm delete-parameters --names ...`), and the SES identities if you no longer need them.
* For a quick pause without teardown: `aws rds stop-db-instance` (auto-restarts after 7 days) and stop the EC2 instance.

## Local-only alternative

Everything in this assignment can be demonstrated with `docker compose` at zero cloud cost (see the README). S3 is emulated with signed local upload URLs and the same processing code, and SES is replaced by Mailpit.
