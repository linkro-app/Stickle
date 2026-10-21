# Security

## Reporting a vulnerability

Please report security problems privately, not in a public issue: on this repository's
**Security** tab choose **Report a vulnerability**, or go straight to
[the report form](https://github.com/linkro-app/Stickle/security/advisories/new).

It helps to include:

- the Stickle version (About Stickle shows it) and your operating system;
- what someone could do, and what they would need first (access to the computer, a file
  they get you to open, …);
- steps to reproduce it, or a proof of concept.

Stickle is a small project, so a reply can take a few days; we aim to answer within a week.
Once a fix is out, the advisory is published with credit to you, unless you prefer
otherwise.

## Supported versions

Stickle is in preview. Fixes go into the next release, so only the latest release is
supported.

## What matters most

- Note text, keys or the recovery key that someone could read or change without the
  password or access the app expects.
- Anything the app sends over the network beyond the daily update check, which only reads
  a fixed file from `stickle.linkro.co`.
- Files from other apps that, brought in, make Stickle misbehave.

How Stickle handles your data is described in the [privacy policy](PRIVACY.md).
