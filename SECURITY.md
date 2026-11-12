# Security policy

## Reporting a vulnerability

Please report it privately through GitHub: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue.

Include what an attacker could do, the version (the page's footer shows it), and how to
reproduce it. You should get a first answer within a week; this is a project maintained in
spare time, so a fix may take longer, and you will hear how it is going.

## Supported versions

Fixes go into the latest release. Before 1.0 there are no backports.

## What counts

ai-panel can switch a server off, so the bar is high. Examples of what counts:

- any way to start an action, change a setting or read the API without passing the configured
  sign-in, or past the allowed groups;
- a way to make the panel act from another site (CSRF), or to run script in its pages;
- a way to make the panel send anything to the host agent or the BMC other than the fixed
  verbs and commands, or to make `llm-ctl` run anything but its fixed commands;
- a way to change, from the page, the API or the CLI, what the site config owns (where the
  panel connects, which actions exist);
- secrets (the BMC password, the SSH key, the LLM's API key) reaching the logs, the page, the
  API or the audit log.

[docs/security.md](docs/security.md) describes the design and what it relies on you to do:
in particular, the sign-in headers can be trusted only when your proxy is the only way to the
panel.
