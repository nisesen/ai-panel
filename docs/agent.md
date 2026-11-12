# The host agent, llm-ctl

`llm-ctl` is what the panel runs on the GPU host. It is one Python file
([agent/llm-ctl](../agent/llm-ctl)) that needs only the host's own Python (3.11 or newer;
Debian 13 has 3.13), and a dedicated account whose SSH key can run nothing else.

## What it does

The panel connects as `llmctl` and sends one verb. sshd runs `llm-ctl` instead of whatever was
asked (a forced command), which finds the request in `SSH_ORIGINAL_COMMAND`, checks it, and runs
itself again through `sudo` with the checked words. As root it checks them once more, runs fixed
commands as argument lists (never through a shell), answers one JSON object, and logs the call
to syslog with the tag `llm-ctl`.

| Verb | Runs |
| --- | --- |
| `status` | `pvesh get` for the guests and the running tasks; in the LLM's container `systemctl is-active` of each model's unit and `curl` of `/health` and `/props` (the API key goes to curl on stdin, never on a command line) |
| `shutdown`, `reboot` | `pct shutdown <ct> --timeout <n>`, then answers, then `systemctl poweroff` or `reboot` |
| `llm switch <model>` | in the container: stop the other models' units, `reset-failed`, start the model's unit |
| `llm restart`, `llm stop`, `llm start` | the active unit; every model's unit; the default model's unit |
| `guest <id> start\|shutdown\|reboot` | `pct` or `qm` (by the guest's type), only for ids in `guests.allowed` |
| `probe` | `nvidia-smi --query-gpu=...`, the LLM's state, and your own probe command if configured |
| `hermes-checks pause\|resume` | the configured command inside the client's VM, through `qm guest exec` |

Anything else, an unknown model or guest, an extra word, or a character outside
`A-Z a-z 0-9 . _ -`, is refused with `{"ok": false, "error": ...}` and exit code 2. A verb that
fails answers `ok: false` with exit code 1.

## Install

As root on the host. Paths are suggestions; the panel only needs the account and the key.

1. **The agent and its config.**

   ```bash
   install -m 0755 -o root -g root llm-ctl /usr/local/sbin/llm-ctl
   install -d -m 0755 /etc/llm-ctl
   install -m 0644 -o root -g root config.example.toml /etc/llm-ctl/config.toml
   ```

   Edit the config: the LLM's container id, each model's unit, the guests the panel may touch,
   and optionally the client checks and a probe command. The format is TOML (`tomllib` is in
   Python's standard library; YAML is not). Check it as root with `llm-ctl status`.

2. **The account.** sshd runs a forced command through the account's shell, so it needs one;
   the forced command decides what actually runs.

   ```bash
   useradd --system --create-home --home-dir /var/lib/llmctl --shell /bin/sh llmctl
   ```

   Its password stays locked: only its key can log in.

3. **The key**, in a file the account cannot change, limited to your panel's addresses (in
   Kubernetes, usually the nodes' addresses, as pods leave through their node):

   ```bash
   install -m 0644 -o root -g root /dev/null /etc/llm-ctl/authorized_keys
   echo 'restrict,from="192.0.2.31,192.0.2.32,192.0.2.33",command="/usr/local/sbin/llm-ctl" ssh-ed25519 AAAA... ai-panel' \
     > /etc/llm-ctl/authorized_keys
   ```

4. **sshd**, in `/etc/ssh/sshd_config.d/llm-ctl.conf`:

   ```
   Match User llmctl
       AuthorizedKeysFile /etc/llm-ctl/authorized_keys
       ForceCommand /usr/local/sbin/llm-ctl
       AuthenticationMethods publickey
       PasswordAuthentication no
       KbdInteractiveAuthentication no
       PermitTTY no
       AllowTcpForwarding no
       AllowStreamLocalForwarding no
       AllowAgentForwarding no
       X11Forwarding no
       PermitTunnel no
       PermitUserRC no
       GatewayPorts no
   ```

   If your sshd has `AllowUsers` or `AllowGroups`, add `llmctl` there. Then `sshd -t` and
   `systemctl reload ssh`.

5. **sudo**, in `/etc/sudoers.d/llm-ctl` (check with `visudo -cf /etc/sudoers.d/llm-ctl`):

   ```
   llmctl ALL=(root) NOPASSWD: /usr/local/sbin/llm-ctl
   ```

   `llm-ctl` checks the verb before it calls sudo and again as root.

6. **The panel's side:** the private key and the host's public key, in `known_hosts` form
   (`ssh-keyscan -t ed25519 192.0.2.20 > known_hosts`, checked against the host's own
   `/etc/ssh/ssh_host_ed25519_key.pub`), mounted where `host.key_file` and
   `host.known_hosts_file` point.

## Check it

From the panel's address, with the panel's key:

```bash
ssh -i key llmctl@192.0.2.20 status              # JSON with ok: true
ssh -i key llmctl@192.0.2.20 llm switch nope     # {"ok": false, "error": "unknown model 'nope'"}, exit 2
ssh -i key llmctl@192.0.2.20 'status; id'        # refused word, exit 2
ssh -i key llmctl@192.0.2.20 bash                # unknown command, exit 2
ssh -i key -t llmctl@192.0.2.20                  # no TTY, and still only llm-ctl
```

From any other address, the same key is refused. On the host, `journalctl -t llm-ctl` shows
every call: the refused ones as warnings, `status` at debug level (the panel asks every few
seconds), everything else as notices.

## Update

Replace `/usr/local/sbin/llm-ctl` with the file from the release you deploy, after checking its
checksum. The config, the account and the key stay as they are.
