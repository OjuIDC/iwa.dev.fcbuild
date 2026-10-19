targetScope = 'resourceGroup'

@description('Lowercase prefix used for globally scoped resource names. Hyphens are removed automatically.')
@minLength(3)
@maxLength(18)
param prefix string = 'matchpulse'

@description('Location for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Full container image reference, for example ghcr.io/OWNER/REPO:TAG or ACR_NAME.azurecr.io/matchpulse:TAG.')
param image string

@description('Optional Microsoft Foundry / Azure OpenAI endpoint. Leave empty to run the deterministic pipeline only.')
param foundryEndpoint string = ''

@description('Optional Foundry deployment name. Leave empty to run the deterministic pipeline only.')
param foundryDeployment string = ''

@description('Foundry authentication mode. managed_identity is preferred and needs no stored secret.')
@allowed([
  'managed_identity'
  'disabled'
])
param foundryAuthMode string = 'managed_identity'

@description('Log Analytics retention in days.')
@minValue(7)
@maxValue(730)
param logRetentionInDays int = 30

@description('Minimum container replica count.')
@minValue(0)
@maxValue(10)
param minReplicas int = 1

@description('Maximum container replica count.')
@minValue(1)
@maxValue(20)
param maxReplicas int = 5

@description('HTTP concurrency per replica used by the Container Apps scaling rule.')
@minValue(1)
@maxValue(200)
param concurrentRequestsPerReplica int = 50

var namePrefix = replace(toLower(prefix), '-', '')
var suffix = uniqueString(resourceGroup().id)
var workspaceName = '${namePrefix}-logs-${suffix}'
var environmentName = '${namePrefix}-env-${suffix}'
var appName = '${namePrefix}-api-${suffix}'
var eventNamespaceName = '${namePrefix}eh${suffix}'
var insightsName = '${namePrefix}-appi-${suffix}'

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: workspaceName
  location: location
  properties: {
    retentionInDays: logRetentionInDays
    features: { enableLogAccessUsingOnlyResourcePermissions: true }
  }
  sku: { name: 'PerGB2018' }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: insightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

// Event Hubs is a scale-out seam for future ingestion. No Event Hubs consumer
// is implemented in this challenge; HTTP ingestion remains the supported path.
resource eventNamespace 'Microsoft.EventHub/namespaces@2024-01-01' = {
  name: eventNamespaceName
  location: location
  sku: { name: 'Basic', tier: 'Basic', capacity: 1 }
  properties: {
    publicNetworkAccess: 'Enabled'
    minimumTlsVersion: '1.2'
  }
}

resource matchEvents 'Microsoft.EventHub/namespaces/eventhubs@2024-01-01' = {
  parent: eventNamespace
  name: 'synthetic-match-events'
  properties: {
    messageRetentionInDays: 1
    partitionCount: 2
  }
}

var insightsSecret = [
  {
    name: 'applicationinsights-connection-string'
    value: insights.properties.ConnectionString
  }
]

var baseEnv = [
  {
    name: 'CHALLENGE_SYNTHETIC_ONLY'
    value: 'true'
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    secretRef: 'applicationinsights-connection-string'
  }
  {
    name: 'AZURE_EVENTHUB_NAMESPACE'
    value: eventNamespace.properties.serviceBusEndpoint
  }
  {
    name: 'AZURE_EVENTHUB_NAME'
    value: matchEvents.name
  }
]

var foundryEnv = empty(foundryEndpoint) ? [] : [
  {
    name: 'FOUNDRY_ENDPOINT'
    value: foundryEndpoint
  }
  {
    name: 'FOUNDRY_DEPLOYMENT'
    value: foundryDeployment
  }
  {
    name: 'FOUNDRY_AUTH_MODE'
    value: foundryAuthMode
  }
  {
    name: 'FOUNDRY_AZURE_SCOPE'
    value: 'https://cognitiveservices.azure.com/.default'
  }
]

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: insightsSecret
      ingress: {
        external: true
        targetPort: 8080
        transport: 'httpsOnly'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
    }
    template: {
      containers: [
        {
          name: 'matchpulse'
          image: image
          env: concat(baseEnv, foundryEnv)
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/health/live', port: 8080 }
              initialDelaySeconds: 10
              periodSeconds: 20
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/health/ready', port: 8080 }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 3
            }
            {
              type: 'Startup'
              httpGet: { path: '/health/live', port: 8080 }
              initialDelaySeconds: 5
              periodSeconds: 10
              failureThreshold: 12
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-concurrency'
            http: { metadata: { concurrentRequests: '${concurrentRequestsPerReplica}' } }
          }
        ]
      }
    }
  }
}

output appUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
output appName string = app.name
output managedIdentityPrincipalId string = app.identity.principalId
output eventHubNamespace string = eventNamespace.name
output eventHubName string = matchEvents.name
output applicationInsightsName string = insights.name
output logAnalyticsWorkspace string = logs.name
output foundryConfigured bool = !empty(foundryEndpoint)
