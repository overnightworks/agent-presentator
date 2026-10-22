# Runbook: add a Git source

Reader: an admin of the instance.

An instance starts with no source. What a source is and which addresses are
accepted is [sources](../reference/sources.md).

## Before you start

- The Git URL, and for HTTPS a read-only token. A password in the URL's
  userinfo is refused: it belongs in the Secret field. A user name in the URL
  is optional.
- For a folder on this machine use
  [add a folder on this box](add-a-folder-on-this-box.md) instead.

## Steps

1. Open Settings · Sources and choose Add. Enter the name and the Git URL.
2. HTTPS: enter the token. SSH: Add shows an owned ed25519 deploy-key draft;
   register its public half at the Git host.
3. Choose *Check connection*. It probes that exact token or draft before
   anything is stored; *Create* stays disabled until it answers reachable.
4. Choose *Create*. The page shows the webhook secret exactly once, to your
   session; store it if a Git host will call the
   [fetch-now hook](call-the-fetch-now-hook.md).

## Check

The source's row shows reachable. A token the host turns down shows as
refused, an answer of any other kind as failed; unreachable is only a host
that never answers. The next poll (`PRESENTATOR_SOURCE_POLL_SECONDS`) or
*Fetch now* on the source's page brings its decks into the list.

Related: [renew a source's secret](renew-a-source-secret.md).
