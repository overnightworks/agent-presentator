# Runbook: configure Keycloak for sign-in

Reader: the administrator of the Keycloak realm.

There is no password login and no `/setup`; accounts arrive with the Keycloak
sign-in. What a sign-in does is
[sign-in and session](../reference/sign-in-and-session.md).

## Before you start

The instance's `PRESENTATOR_PUBLIC_ORIGIN` and `PRESENTATOR_TALK_ORIGIN`
(both https) are known. Inside the internal network the lobby's public
hostname must resolve to the instance (an alias), because the instance answers
only its two configured hostnames.

## Steps

1. Create a client for the instance. Its id is
   `PRESENTATOR_OIDC_CLIENT_ID`; write its secret to a file whose absolute path
   is `PRESENTATOR_OIDC_CLIENT_SECRET_FILE`. The file is read once at start,
   minus one trailing newline.
2. Set `PRESENTATOR_OIDC_ISSUER` exactly to the `issuer` of the realm's
   discovery document, without a trailing slash. The token's `iss` is compared
   with it character for character: nothing is normalised and an explicit port
   stays part of it.
3. Turn `full.path` on in the client's groups mapper. With bare group names
   nobody matches `/apps/presentator` and every sign-in shows "No access".
   Members of `/apps/presentator` are admitted; members of
   `/apps/presentator/admin` are admins.
4. Register `http://<the lobby's public hostname>:<PRESENTATOR_PORT>/auth/backchannel-logout`,
   reached through the internal alias, as the client's back-channel
   logout URL. Its hostname is the lobby's public hostname: a request under any
   other Host, an internal hostname or an IP included, gets 404 and ends no
   session. The route is not live until the client registers
   it; a valid Logout Token then ends every session of its subject, on every
   device.
5. Register `<PRESENTATOR_PUBLIC_ORIGIN>/login` as the post-logout redirect
   URI, or Keycloak shows an error page at every logout.
6. Register both `<PRESENTATOR_PUBLIC_ORIGIN>/auth/callback` and
   `<PRESENTATOR_TALK_ORIGIN>/auth/callback` as valid redirect URIs: the talk
   host signs in at its own.

## Check

Sign in: a member of the group lands on the lobby, a non-member sees "No
access". Log out: the browser returns to `/login`. An INFO log line records an
account's creation or role change (event kind, Keycloak subject id, role).
