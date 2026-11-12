# Running in Kubernetes

Example manifests for one panel. Names, namespaces and addresses are examples; adapt them, and
put your proxy (Authentik, Authelia, oauth2-proxy, ...) in front of the Service.

## The Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: {name: ai-panel, namespace: ai-panel}
spec:
  replicas: 1                      # one writer of the state directory, one scheduler
  strategy: {type: Recreate}       # never two panels at once, not even during an update
  selector: {matchLabels: {app: ai-panel}}
  template:
    metadata: {labels: {app: ai-panel}}
    spec:
      # SIGTERM cancels a running action (it is logged as interrupted); give a write to a slow
      # state directory time to finish.
      terminationGracePeriodSeconds: 60
      securityContext:
        runAsUser: 10001           # the image's user: ssh refuses a uid without a passwd entry
        runAsGroup: 10001
        runAsNonRoot: true
        fsGroup: 10001             # Secret files group-readable; leave out for NFS (below)
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: ai-panel
          image: ghcr.io/nisesen/ai-panel:0.1.1   # better: pinned by digest
          env:
            - {name: AIPANEL_CONFIG, value: /config/config.yaml}
            - {name: BMC_USER, valueFrom: {secretKeyRef: {name: ai-panel, key: bmc-user}}}
          ports:
            - {name: http, containerPort: 8080}
            - {name: metrics, containerPort: 9090}
          securityContext:
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
          # /healthz answers while the process runs; a slow state directory (NFS) never
          # holds it up, but a first start may take a while: be generous.
          startupProbe:
            httpGet: {path: /healthz, port: metrics}
            periodSeconds: 5
            failureThreshold: 60
          livenessProbe:
            httpGet: {path: /healthz, port: metrics}
            periodSeconds: 30
            timeoutSeconds: 10
            failureThreshold: 6
          resources:
            requests: {cpu: 20m, memory: 96Mi}
            limits: {memory: 256Mi}
          volumeMounts:
            - {name: config, mountPath: /config, readOnly: true}       # the folder, not subPath
            - {name: secrets, mountPath: /secrets, readOnly: true}
            - {name: state, mountPath: /var/lib/aipanel}
            - {name: tmp, mountPath: /tmp}
      volumes:
        - name: config
          configMap: {name: ai-panel-config}
        - name: secrets
          secret:
            secretName: ai-panel
            defaultMode: 0440      # with fsGroup; without it, 0444
            items:
              - {key: bmc-password, path: bmc/password}
              - {key: ssh-key, path: ssh/id_ed25519}
              - {key: known-hosts, path: ssh/known_hosts}
              - {key: llm-api-key, path: llm/api-key}
        - name: state
          persistentVolumeClaim: {claimName: ai-panel-state}
        - name: tmp
          emptyDir: {medium: Memory, sizeLimit: 16Mi}
```

- **The config folder, not `subPath`:** Kubernetes updates a mounted ConfigMap folder in place,
  and the panel reloads it; a `subPath` mount changes only when the pod restarts.
- **The SSH key's mode:** ssh refuses a key that is the user's own file and readable by others.
  A Secret's files belong to root (group `fsGroup` when set), so `0440` with `fsGroup`, or
  `0444` without, both work and keep the key from other pods.
- **The state directory on NFS:** `fsGroup` makes the kubelet change the group of every file on
  some NFS volumes. There, leave `fsGroup` out, make the folder belong to uid 10001 on the
  server, and use `defaultMode: 0444` for the Secret. The panel writes its state files `0640`
  (the CSRF key `0600`), so a backup job running in group 10001 can read them.
- **A state directory that is missing or unreadable** does not stop the panel: it runs read-only
  and says so on every page and in `aipanel_state_writable`.

## The Services

The pages and the API (8080) go through your proxy. `/metrics` and `/healthz` (9090) are for
Prometheus and the kubelet only: give them their own Service, and never route them.

```yaml
apiVersion: v1
kind: Service
metadata: {name: ai-panel, namespace: ai-panel}
spec:
  selector: {app: ai-panel}
  ports: [{name: http, port: 80, targetPort: http}]
---
apiVersion: v1
kind: Service
metadata: {name: ai-panel-metrics, namespace: ai-panel, labels: {app: ai-panel}}
spec:
  selector: {app: ai-panel}
  ports: [{name: metrics, port: 9090, targetPort: metrics}]
```

## The NetworkPolicy: the real control

The panel trusts the user and group headers only from `auth.trusted_proxies`. In Kubernetes the
proxy's pods change address, so that list is usually the whole pod network, and any pod could
send the headers. **The control that counts is a NetworkPolicy that admits only the proxy's
pods to port 8080**, and Prometheus to 9090:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: ai-panel, namespace: ai-panel}
spec:
  podSelector: {matchLabels: {app: ai-panel}}
  policyTypes: [Ingress, Egress]
  ingress:
    - from:   # only the proxy that signs people in and sets the headers
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: auth-proxy}}
          podSelector: {matchLabels: {app: auth-proxy}}
      ports: [{port: 8080}]
    - from:
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: monitoring}}
      ports: [{port: 9090}]
  egress:
    - to: [{ipBlock: {cidr: 192.0.2.10/32}}]     # the BMC
      ports: [{protocol: UDP, port: 623}]
    - to: [{ipBlock: {cidr: 192.0.2.20/32}}]     # the host agent
      ports: [{protocol: TCP, port: 22}]
    - to:
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: kube-system}}
          podSelector: {matchLabels: {k8s-app: kube-dns}}
      ports: [{protocol: UDP, port: 53}, {protocol: TCP, port: 53}]
    - to:
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: monitoring}}
      ports: [{port: 9090}, {port: 9093}]         # Prometheus, Alertmanager
    # and the LLM's URL (llm.url): its address or its gateway's
```

Your CNI must enforce NetworkPolicies (Cilium, Calico, ...); some do not by default. With
Cilium, a `CiliumNetworkPolicy` can also allow the LLM by name (`toFQDNs`).
