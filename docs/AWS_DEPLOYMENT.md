# AWS deployment

> **Status: not deployed.** `serverless.yml` was validated offline only: `serverless package` with stubbed SSM values, then `cfn-lint` on the compiled template (see [TESTING.md](TESTING.md)). Nothing here has been run against a real AWS account. Every step below creates billable resources; read the cost section first.

## What gets created

| Resource | Notes | Approx. cost (ap-south-1, check current pricing) |
|---|---|---|
| RDS PostgreSQL 17 `db.t4g.micro`, 20 GB gp3, single-AZ, private | **Bills every hour it exists, even when idle** | ≈ US$13-15/month + storage ≈ US$2.5/month (free tier may apply to new accounts; don't assume it) |
| S3 bucket (private, SSE-S3, uploads expire after 7 days) | | cents |
| 3 Lambdas + HTTP API | pay per request | ≈ 0 at demo volume |
| S3 gateway VPC endpoint | free | 0 |
| Security groups, DB subnet group, IAM role, SES send policy | free | 0 |
| CloudWatch logs (14-day retention) | | cents |

Not created by the stack (you create or choose them): VPC/subnets (use the default VPC), the **n8n host**, SES identities, and SSM parameters.
The n8n host (e.g. EC2 `t4g.small`, ≈ US$12/month, or `t4g.micro` if memory allows) also bills while running. Stop it when you aren't demoing.

Deliberately **not** used: NAT gateway (≈ US$32+/month plus data), RDS Proxy (≥ ≈ US$20/month), Secrets Manager interface endpoints (≈ US$7/month per AZ), Multi-AZ RDS.

## Prerequisites (manual)

1. An AWS account and an IAM principal that can deploy CloudFormation, Lambda, API Gateway, S3, RDS, EC2 security groups, IAM roles/policies and SSM. Configure it with `aws configure --profile loan-engine` (never commit keys).
2. Docker running (the Python requirements plugin builds Linux wheels in a container).
3. Node 18+ for the Serverless Framework v3 (installed locally via the repo-root `package.json`: `npm install`).
4. Pick a region (default `ap-south-1`). SES, RDS and Lambda must all be available there.
5. Default VPC IDs:
   ```bash
   aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text
   aws ec2 describe-subnets --filters Name=vpc-id,Values=<vpc-id> --query 'Subnets[].SubnetId' --output text
   aws ec2 describe-route-tables --filters Name=vpc-id,Values=<vpc-id> --query 'RouteTables[].RouteTableId' --output text
   ```
   RDS needs subnets in at least two AZs.

## Secrets (SSM Parameter Store)

```bash
STAGE=dev; P=/loan-engine/$STAGE
gen() { python -c "import secrets; print(secrets.token_urlsafe(32))"; }
aws ssm put-parameter --name $P/db-password --type SecureString --value "$(gen)"
aws ssm put-parameter --name $P/internal-api-token --type SecureString --value "$(gen)"
aws ssm put-parameter --name $P/n8n-webhook-token --type SecureString --value "$(gen)"
aws ssm put-parameter --name $P/notification-test-recipient --type String --value "you@your-inbox.example"
# after the n8n instance exists (private IP of the EC2 instance):
aws ssm put-parameter --name $P/n8n-matching-webhook-url --type String --value "http://10.0.1.25:5678/webhook/loan-matching"
```

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
npx serverless deploy --stage dev --region ap-south-1 --aws-profile loan-engine \
  --param="vpcId=vpc-xxxx" \
  --param="subnetIds=subnet-aaaa,subnet-bbbb" \
  --param="routeTableIds=rtb-xxxx" \
  --param="frontendOrigin=http://localhost:5173" \
  --param="sesFromAddress=verified-sender@example.com" \
  --param="n8nSecurityGroupId=sg-of-the-n8n-instance"
npx serverless invoke -f migrate --stage dev --aws-profile loan-engine
# optional, demo only - loads SYNTHETIC products labelled as fixtures:
npx serverless invoke -f migrate --stage dev --data '{"seed_dev_fixtures": true}'
```

Serverless v3's config schema predates `python3.12`, so it prints a schema warning. CloudFormation accepts the runtime. (`osls`, the maintained v3 fork, may avoid the warning, but that has not been tested with this config.)

Then:

* Frontend: build with `VITE_API_BASE_URL=<HttpApiUrl> npm run build` and host `frontend/dist` (S3 static website or any static host). Redeploy with `frontendOrigin=<that origin>` so CORS (API and bucket) allows it.
* n8n: run the same `docker-compose.yml` `n8n` service on the EC2 instance (or `docker run n8nio/n8n:2.43.3`) with `N8N_ENCRYPTION_KEY` set, `LOAN_API_BASE_URL=<HttpApiUrl>`, `N8N_SELF_BASE_URL=http://localhost:5678`, and `WEBHOOK_URL=http://<private-ip>:5678/`. Don't open 5678 to the internet: the instance SG should allow 5678 only from the Lambda SG (the stack adds that rule when `n8nSecurityGroupId` is given) and SSH from your IP. Open the editor via `ssh -L 5678:localhost:5678 ec2-user@<host>`. Import credentials and workflows as in [N8N_WORKFLOWS.md](N8N_WORKFLOWS.md), using the cloud token values.

## Networking and connection management

* Lambdas get an egress-only SG; RDS allows 5432 only from it, `PubliclyAccessible: false`, and storage is encrypted. `sslmode=require` is used.
* No NAT: S3 goes through the gateway endpoint, n8n is reached on its private IP, and nothing else outbound is needed by the Lambdas (the LLM step is disabled in the cloud config; enabling it would require NAT or a proxy).
* Connections: one connection is reused per warm Lambda container; `processCsv` has `reservedConcurrency: 5`. **RDS Proxy** is the next step if API concurrency approaches `max_connections` (~80 on `t4g.micro`). It isn't included because it costs more than the database at this size.
* HTTP API has a 30 s integration timeout; see the limit in [OPTIMIZATION.md](OPTIMIZATION.md).

## Verify after deploy (manual)

1. `curl <HttpApiUrl>/health` → `{"status":"ok"}`.
2. Upload `sample-data/users_sample.csv` through the frontend. Check CloudWatch logs for `processCsv` (`batch imported`) and that `matching_status` becomes `TRIGGERED`.
3. Check n8n executions for B and C, and the test inbox.

## Teardown (stop all charges)

```bash
aws s3 rm s3://loan-eligibility-engine-uploads-dev-<account-id> --recursive   # bucket must be empty
npx serverless remove --stage dev --aws-profile loan-engine
```

* RDS has `DeletionPolicy: Snapshot`: removal leaves a **final snapshot, which bills for storage**. Delete it in RDS → Snapshots if you don't need it.
* Terminate the n8n EC2 instance and release any Elastic IP.
* Delete the SES IAM user and its access keys, the SSM parameters (`aws ssm delete-parameters --names ...`), and the SES identities if you no longer need them.
* For a quick pause without teardown: `aws rds stop-db-instance` (auto-restarts after 7 days) and stop the EC2 instance.

## Local-only alternative

Everything in this assignment can be demonstrated with `docker compose` at zero cloud cost (see the README). S3 is emulated with signed local upload URLs and the same processing code, and SES is replaced by Mailpit.
