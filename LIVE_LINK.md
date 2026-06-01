Public dashboard link (temporary reverse-SSH tunnel):

https://3b831e7fbdf5ac.lhr.life

Timestamp: 2026-05-27 00:45:00 UTC

Notes:
- This URL is a reverse-SSH tunnel (ssh.localhost.run) pointing to the dashboard running on port 8050.
- It is temporary — the tunnel address changes when the SSH session closes or restarts.
- To get a stable/custom hostname you can: reserve a localhost.run name (requires account + SSH key), buy a domain + small VPS and configure an nginx reverse-proxy, or use a paid ngrok plan with reserved domains.

To reopen the tunnel locally:
```bash
ssh -R 80:localhost:8050 ssh.localhost.run
```

Keep this terminal session open while you share the link.
