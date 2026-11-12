# Security

ai-panel can switch a server off, cut its power, and stop the services on it. This page says
what it protects, how, and what it relies on you to do.

## What an attacker would want

| Asset | Where it is | What protects it |
| --- | --- | --- |
| The BMC's credentials | a Secret the panel reads (a file or an environment variable) | an **Operator** account: power control, nothing else; never printed or logged; ipmitool reads the password from a file (`-f`) or its environment (`-E`), never from its command line |
| Power and the host's services | the panel's actions | sign-in through your proxy, an allowed group, a CSRF token on every change, the host's name typed for power actions, one action at a time, a cooldown between power actions, an audit log |
| The LLM's API key (optional) | a Secret the panel reads (a file or an environment variable) | sent only to `llm.url`, as a bearer header; never shown, logged or put on a command line |
| The host | SSH as `llmctl` | a key that may run only `llm-ctl`, only from your nodes; `llm-ctl` accepts fixed verbs with arguments from lists, never a shell ([agent.md](agent.md)) |
| Where the panel points | `config.yaml` | owned by git: nothing in the panel can change it |
| The schedule | `settings.json` | the same sign-in; every change validated and audited (who, from, to) |

## Sign-in: no auth, no start

The panel has no login of its own. It trusts a proxy in front of it to sign people in and to
pass who they are in two headers:

```yaml
auth:
  mode: trusted_header
  user_header: X-authentik-username     # Authentik; Authelia: Remote-User
  groups_header: X-authentik-groups     # Authelia: Remote-Groups
  groups_separator: "|"                 # Authelia: ","
  allowed_groups: [admins]
```

- A request without the user header is refused (401), and one without an allowed group too
  (403). `allowed_groups` may not be empty.
- **The headers are only as trustworthy as the path to the panel.** Anyone who can reach the
  panel's port directly can send any header they like. So:
  - make the proxy the only way in: a network policy that admits only the proxy's pods, or a
    listen address only the proxy reaches;
  - make the proxy strip these headers from incoming requests and set them itself (Authentik's
    and Authelia's forward-auth outposts do);
  - optionally, list the proxy's addresses in `auth.trusted_proxies`: requests from anywhere
    else are refused.
- `auth.mode: none` exists for DEMO mode only; the panel refuses to start with it otherwise.

Every change (POST, PUT, PATCH, DELETE) also needs a CSRF token, bound to the signed-in user
and to the panel's key: a page from another site cannot make the browser start an action. The
key is made on the first start and kept in the state directory (`csrf.key`, mode 0600), so
open pages keep working after a restart; with a read-only state directory it lives in memory
only (the panel logs a warning) and pages need a reload after a restart. Whoever can read the state
directory could make valid tokens (they would still need to pass the sign-in), so keep it
private to the panel.
Pages send a strict Content-Security-Policy (scripts and styles from the panel only; htmx is
configured not to evaluate code), `X-Content-Type-Options: nosniff` and no-store caching.

## The BMC

- Create an **Operator** account for the panel (privilege `OPERATOR`, `bmc.privilege`), not an
  administrator: it can switch power and read sensors, and cannot change users or the network.
  Check that your firmware lets an Operator use chassis power control, and which cipher suite
  it needs (`bmc.cipher_suite`, ipmitool's `-C`).
- IPMI over LAN is an old protocol: keep the BMC on a network only management machines reach,
  and allow the panel's address to UDP 623 and nothing else.
- The panel uses the BMC to switch the host **on**, to confirm that it went **off**, and, only
  in the emergency actions, to cut or reset power. A planned stop goes through the host agent:
  on some hardware a BMC power cycle hard-kills the guests and cannot clear latched faults.

## The LLM's API key

When llama.cpp runs with `--api-key`, every endpoint but `/health` and `/v1/models` needs the
key, including the two the panel reads: `/props` (which model is really loaded: the dashboard
shows it, and an LLM action reads it back to confirm a switch) and `/slots` (busy slots: the
scheduler waits for them before a stop, and the shutdown form warns about them). So the panel
gets the key, named in the site config and never written there:

```yaml
llm:
  url: https://llm.example.com
  api_key: {file: /secrets/llm/api-key}      # or {env: LLM_API_KEY}
```

- It is **optional**: leave `api_key` out when your server has no key. With a key on the server
  and none in the panel, those reads fail: the model shows as unknown, a switch cannot be
  confirmed, and the scheduler cannot see busy slots.
- It is read from the file or the variable at each request (a rotated key is picked up without
  a restart), sent only to `llm.url` in the `Authorization` header, and never shown on a page,
  in the API, in the logs or in the audit log.
- llama.cpp has one key for everything, so it also lets its holder use the LLM. That is the cost
  of reading `/props` and `/slots`; the panel itself only reads.
- The host agent reads `/props` inside the LLM's container with the key file there
  (`llm.api_key_file` in its own config), and passes it to curl on stdin.

## The host agent

See [agent.md](agent.md). In short: a dedicated account whose key carries
`restrict,from="<your nodes>",command="/usr/local/sbin/llm-ctl"`, an sshd `Match` block that
forces the same command with no forwarding and no TTY, and one sudoers line. `llm-ctl` reads
the verb from `SSH_ORIGINAL_COMMAND`, accepts only fixed verbs whose arguments come from its own
config's lists, runs commands as argument lists (never through a shell), and logs every call to
syslog. The panel checks the words it sends too, and pins the host's key (`known_hosts_file`,
`StrictHostKeyChecking=yes`).

## The container

- Runs as uid 10001, needs no capabilities, and works with a read-only root file system: it
  writes only to `/tmp` and its state directory.
- `/metrics` and `/healthz` listen on a separate port (9090). Do not route it through the proxy;
  only Prometheus and the kubelet need it. It shows no secrets.
- Egress needs only: the BMC (UDP 623), the host (TCP 22), DNS, Prometheus, Alertmanager and
  the LLM's URL. A default-deny egress policy with those exceptions limits what a compromised
  panel could reach.
- Mount the SSH key, the BMC password and the LLM's API key from a Secret, in a volume only
  this pod mounts. The panel's user must be able to read the key: a Secret volume's files are
  owned by root, so `defaultMode: 0444` works (ssh refuses a readable key only when the file is
  the user's own).

## Dry-run

`actions.dry_run: true` reads everything for real and only logs what it would change: no BMC
command, no `llm-ctl` verb that changes anything, no silence. Use it to check a new deployment
against the real machine before you let it act.

## What it does not protect against

- Someone who controls your proxy, or who can reach the panel's port directly when it is
  reachable: they can act as any user.
- Someone with the Operator BMC account's password and network access to the BMC: they can
  switch power without the panel. Rotate it if you suspect it leaked.
- Someone with `kubectl exec` into the panel's pod: they can read its Secrets and run the CLI.

## Reporting a vulnerability

Privately, as [SECURITY.md](../SECURITY.md) describes.
