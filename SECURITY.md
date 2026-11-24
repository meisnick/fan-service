# Security

fan-service runs as a **privileged** container with BMC access, so treat it as sensitive.

- The dashboard is meant for a trusted LAN. Reads are unauthenticated; every mutating endpoint (curves, LEDs, layout)
  requires the edit token.
- The fan safety limits (`safety.json`) can't be changed over HTTP at all.
- Please report vulnerabilities **privately** through GitHub's "Report a vulnerability" (Security tab), not as a
  public issue.
