# Runbook: configure Keycloak for sign-in

Reader: the administrator of the Keycloak realm.

There is no password login and no `/setup`; accounts arrive with the Keycloak
sign-in. What a sign-in does is
[sign-in and session](../reference/sign-in-and-session.md).

## Before you start

The instance's `PRESENTATOR_PUBLIC_ORIGIN` (https) and its internal lobby
address are known.

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
4. Register `<internal lobby address>/auth/backchannel-logout` as the client's
   back-channel logout URL. The route is not live until the client registers
   it; a valid Logout Token then ends every session of its subject, on every
   device.
5. Register `<PRESENTATOR_PUBLIC_ORIGIN>/login` as the post-logout redirect
   URI, or Keycloak shows an error page at every logout.

## Check

Sign in: a member of the group lands on the lobby, a non-member sees "No
access". Log out: the browser returns to `/login`. An INFO log line records an
account's creation or role change (event kind, Keycloak subject id, role).
