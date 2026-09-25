# Security policy

## Supported versions

ftcheck is pre-release. Only the latest commit on `main` and the most recent tagged release
receive fixes.

## Reporting a vulnerability

**Please do not open a public issue for a security problem in ftcheck itself.**

Report it privately through GitHub: on this repository, open the **Security** tab and choose
**Report a vulnerability**. The report is visible only to the maintainers until an advisory is
published.

Useful to include:

- what an attacker could do, and under what conditions;
- the ftcheck version or commit, and how it was invoked;
- a minimal reproducer, if you have one.

## What to expect

- An acknowledgement within **7 days**.
- An assessment and, where it is a vulnerability, a fix or mitigation plan within **30 days**.
  Complex issues may take longer; you will be told why.
- Credit in the published advisory, under the name or handle you choose, unless you prefer to
  stay anonymous.

## Scope

In scope: ftcheck's own code, its GitHub Action, and the container image it builds — for
example, anything that lets a scanned project execute code outside what `ftcheck ci` or
`ftcheck stress` is documented to run, or that exposes data from the host.

Out of scope: bugs that ftcheck *finds* in other projects. Report those to the project
concerned, following its own security policy.
