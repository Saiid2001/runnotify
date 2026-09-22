# Security

## Reporting

Report a vulnerability through GitHub's private advisory form at
<https://github.com/Saiid2001/runnotify/security/advisories/new>. Please do not
open a public issue for anything exploitable.

## What this package handles

`runnotify` holds credentials — Slack webhook URLs and Notion API tokens — and
sends them to third-party services over HTTPS. Two properties are deliberate and
worth preserving in any change:

- **Secrets are never placed on a command line.** The OOM watchdog runs as a
  subprocess and receives its configuration, including credentials, on stdin. A
  command line is readable by every user on the machine through `ps` and
  `/proc/<pid>/cmdline`.
- **Secrets are redacted in `repr` and never logged.** Channel objects appear in
  tracebacks and debug output; their `__repr__` shows at most a short tail.

A message you pass to a notifier is sent verbatim to the configured services.
Do not put credentials or personal data in a status message.

## Supported versions

The latest release on PyPI. Fixes are released forward, not backported.
