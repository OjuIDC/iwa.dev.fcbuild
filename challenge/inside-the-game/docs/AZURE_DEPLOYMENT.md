# Azure deployment

Everything in this document has been **validated locally** unless it is
explicitly marked otherwise. Nothing here has been deployed to a live Azure
subscription as part of the published repository.

---

## Status legend

| Marker | Meaning |
| ------ | ------- |
| **Validated locally** | The exact command was executed successfully on the development machine. |
| **Requires Azure authentication** | The command needs an authenticated `az` session and an approved subscription. |
| **Creates billable resources** | The command provisions resources that cost money. |
| **Optional** | Not required for the challenge to run. |

---

## What the template creates

`infra/main.bicep` provisions, in one resource-group-scoped deployment:

| Resource | Purpose |
| -------- | ------- |
| `Microsoft.OperationalInsights/workspaces` | Log Analytics workspace for Container Apps logs |
| `Microsoft.Insights/components` | Application Insights, linked to the workspace |
| `Microsoft.App/managedEnvironments` | Container Apps environment |
| `Microsoft.App/containerApps` | The MatchPulse Live service, system-assigned managed identity, HTTPS-only ingress, liveness/readiness/startup probes |
| `Microsoft.EventHub/namespaces` + `eventhubs` | Scale-out seam for future ingestion |

The Event Hubs namespace is provisioned as a **scale-out seam only**. No
Event Hubs consumer is implemented or tested in this challenge; HTTP ingestion
(`POST /v1/events`) remains the supported ingestion path.

---

## Prerequisites

- Azure CLI 2.x with Bicep: `az bicep install`
- An authenticated session: `az login`
- An explicitly selected subscription: `az account set --subscription <id>`
- An existing resource group in the target region
- A container image published to GHCR or Azure Container Registry

---

## 1. Validate the template

**Validated locally**

```bash
az bicep build --file infra/main.bicep
```

or, with the built-in guard script:

```bash
bash infra/validate.sh          # Linux / macOS
powershell -ExecutionPolicy Bypass -File infra\validate.ps1   # Windows
```

The guard script compiles the template and then asserts that the compiled ARM
JSON contains no hardcoded subscription id, no hardcoded tenant id, and no
secret literal.

**Requires Azure authentication**

```bash
az account show
az account list --output table
```

**Requires Azure authentication** — non-deploying validation against an
approved resource group:

```bash
az deployment group validate \
  --resource-group <approved-resource-group> \
  --template-file infra/main.bicep \
  --parameters image=<approved-image-reference>
```

---

## 2. Deploy

> **Creates billable resources.** Container Apps, Container Apps environment,
> Log Analytics, and Event Hubs all incur cost. Run this only against an
> explicitly approved subscription and resource group.

```bash
az deployment group create \
  --resource-group <approved-resource-group> \
  --template-file infra/main.bicep \
  --parameters prefix=matchpulse \
  --parameters image=ghcr.io/<owner>/matchpulse-live:<tag> \
  --parameters foundryEndpoint=https://<resource>.openai.azure.com \
  --parameters foundryDeployment=<deployment> \
  --parameters foundryAuthMode=managed_identity
```

`infra/main.parameters.example.json` contains the same non-secret parameter
set as a starting point.

### Parameters

| Parameter | Required | Notes |
| --------- | -------- | ----- |
| `prefix` | No | Lowercase prefix for resource names; hyphens are stripped automatically |
| `location` | No | Defaults to the resource group location |
| `image` | **Yes** | Full container image reference |
| `foundryEndpoint` | No | Leave empty to run the deterministic pipeline only |
| `foundryDeployment` | No | Leave empty to run the deterministic pipeline only |
| `foundryAuthMode` | No | `managed_identity` (default) or `disabled` |
| `logRetentionInDays` | No | 7-730, default 30 |
| `minReplicas` / `maxReplicas` | No | 0-10 / 1-20 |
| `concurrentRequestsPerReplica` | No | 1-200, default 50 |

There is **no secret parameter**. The Foundry API key is not accepted by the
template; managed identity is used instead.

### Outputs

`appUrl`, `appName`, `managedIdentityPrincipalId`, `eventHubNamespace`,
`eventHubName`, `applicationInsightsName`, `logAnalyticsWorkspace`,
`foundryConfigured`. No output exposes a secret.

---

## 3. Microsoft Foundry access

**Preferred: managed identity**

The Container App receives a system-assigned managed identity. The service
reads `FOUNDRY_AUTH_MODE=managed_identity` and acquires a token through
`DefaultAzureCredential` (`azure-identity`, optional extra). Grant the
identity `Cognitive Services OpenAI User` on the Foundry resource.

```bash
az role assignment create \
  --assignee-object-id <managed-identity-principal-id> \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" \
  --scope /subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/<provider>/<foundry-resource>
```

**Fallback: Azure Key Vault**

Store the key in Key Vault and reference it from the Container App secret
store. The value is never stored in this repository and never appears in the
template.

**Local only: API key**

`FOUNDRY_API_KEY` is supported for local hackathon testing. Supply it
through the runtime environment and never commit it.

Foundry is **never required**. With no Foundry configuration the service
reports `"foundry_mode": "deterministic-fallback"` on `/health/ready` and
produces every insight from the deterministic pipeline.

---

## 4. GitHub Actions deployment

`.github/workflows/azure-deploy.yml` is prepared but **inert**: the `deploy`
job has `if: false` and only becomes active when all of the following exist:

1. A protected GitHub environment named `azure-production`.
2. Environment variables: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
   `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `ACR_NAME` (or an approved
   GHCR image reference), `CONTAINER_APP_NAME`, `FOUNDRY_ENDPOINT`,
   `FOUNDRY_DEPLOYMENT`, `IMAGE_REFERENCE`.
3. An OIDC subject claim mapping the `azure-production` environment to an Azure
   app registration with a least-privilege role on the target resource group.
4. A change of the `deploy` job's `if:` condition from `false` to `true`.

The workflow uses `permissions: id-token: write` and `azure/login@v2` with a
client id, tenant id, and subscription id. **No `AZURE_CLIENT_SECRET` is used
or requested.** The Bicep validation job runs on every pull request that
touches `infra/`.

---

## 5. Cleanup

Deleting the resource group removes every resource the template created:

```bash
az group delete --name <approved-resource-group> --yes
```

To remove only the application and keep the surrounding resources:

```bash
az containerapp delete --name <app-name> --resource-group <approved-resource-group> --yes
az eventhub namespace delete --name <namespace-name> --resource-group <approved-resource-group>
```

Log Analytics and Application Insights are deleted with the resource group or
can be removed individually:

```bash
az monitor log-analytics workspace delete --workspace-name <workspace-name> --resource-group <approved-resource-group> --yes
az monitor application-insights component delete --app <insights-name> --resource-group <approved-resource-group>
```

---

## 6. Verification after deployment

```bash
curl -fsS https://<app-fqdn>/health/live
curl -fsS https://<app-fqdn>/health/ready
```

`/health/ready` reports `synthetic_only: true` and the active
`foundry_mode`. The demo page is served from `/demo`.
