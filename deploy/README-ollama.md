# The Ollama LXC (192.168.68.49)

This repo provisions the **trading-analyzer** LXC. It does *not* provision the
Ollama LXC — that box was set up by hand and is the other half of the two-LXC
split documented in `CLAUDE.md`. The two files listed below are the only parts
of its configuration this project depends on, so they are kept in git rather
than remembered.

**Nothing here is installed by `setup_app.sh` or `provision_lxc.sh`,** and
neither should learn to: both run *inside* the trading LXC and have no reach
into this one. These are reference copies, to be copied to `.49` by hand. Same
precedent as `deploy/systemd/moomoo-opend.service`.

| File | Installs to (on `.49`) |
|---|---|
| `systemd/ollama.service.d/override.conf` | `/etc/systemd/system/ollama.service.d/override.conf` |
| `nftables/ollama.nft` | loaded via `/etc/nftables.conf`, `systemctl enable nftables` |

## Why they exist

Ollama binds `127.0.0.1:11434` by default, which the backend on `.107` cannot
reach. The drop-in rebinds it to `0.0.0.0`; the nftables rules then restrict
who may actually connect, because **Ollama has no authentication of its own**.
Install them together or not at all — the bind without the firewall puts an
open model API on the LAN, where anyone can run generations against the GPU or
delete the models this project depends on.

The drop-in is a *drop-in* rather than an edit to `ollama.service` because
Ollama's install script rewrites that unit on every update. See the comments in
each file; both explain their own reasoning at length.

## When "Degraded — Ollama unreachable" appears

From the **trading** LXC, one command separates the two causes:

```bash
curl -s -m 5 -o /dev/null -w '%{http_code} in %{time_total}s\n' http://192.168.68.49:11434/v1/models
```

- **Refused in ~0.0001s** (curl exits 7): the packet arrived and nothing was
  listening. The host is fine — this is almost always an Ollama update having
  reverted the bind to `127.0.0.1`. Check `ss -tlnp | grep 11434` on `.49` and
  confirm the drop-in above is still in place.
- **Times out**: a network or firewall fault. Check the `ollama_fw` table, and
  whether `.107` still has the address the ruleset names.
- **200**: Ollama is reachable and the fault is elsewhere. Check that the
  *active* model is actually served —
  `curl -s http://192.168.68.49:11434/v1/models | grep -o '"id":"[^"]*"'` —
  since the backend's model is a persisted `app_state` override and may not be
  the `.env` default (decisions #38). A missing tag fails 90 seconds into a
  scan, once per ticker, not at boot.

No backend restart is ever needed for any of this. `llm_json.client()` builds a
client per call, `ollama_models.active_model()` resolves per thesis, and
`health._ollama_status()` probes per request, so the banner clears on its own
within one 30-second poll.

## If `.107` ever changes address

It is DHCP (`inet 192.168.68.107/22 dynamic`). The permitted source is
hardcoded in `nftables/ollama.nft`; a new lease silently locks the backend out,
presenting as a **timeout** rather than a refusal. That distinction is the
fastest way to tell it apart from the update-reverted-the-bind case above.
