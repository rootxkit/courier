# Operator sign-in

Every API route and the console feed need a signed-in operator (P6-08).
There is no shared login and no anonymous access; the pages themselves
(`/login`, `/map`, `/replay`) load without one and send you to sign in.

## First account

The API refuses anonymous requests, including one to create an account, so
the first admin is made on the machine running the API:

```
python tools/operators.py create-admin <username>
```

It asks for the password twice without showing it (12+ characters, not
containing the username). On the laptop, `local\capacity\run\33-create-admin-v2.bat`
does the same in a window.

Further accounts: `python tools/operators.py create <username> --role viewer`
(or `operator`, `admin`), or `POST /operators` as an admin. Also `list`,
`set-password`, `enable`, `disable`. A provisioning script can pipe a
password with `--password-stdin` instead of typing it.

## Roles

| Role | May |
|---|---|
| `viewer` | see everything: map, alerts, replay, registry, audit log |
| `operator` | also act on alerts |
| `admin` | also change the registry and manage accounts |

An admin cannot lower their own role or disable themselves, so the system
cannot be left with nobody able to manage it.

## Sessions

- Signing in sets an HttpOnly cookie; the token never reaches page scripts.
  A script can ask for it (`"want_token": true`) and send it as
  `Authorization: Bearer`.
- A session ends after `SESSION_TTL_S` (12 h), after `SESSION_IDLE_TIMEOUT_S`
  (1 h) unused, on sign-out, or when an admin changes the account's role,
  password or disables it.
- `LOGIN_MAX_FAILURES` (5) wrong passwords in a row lock the account for
  `LOGIN_LOCKOUT_S` (15 min). `tools/operators.py enable` lifts it.
- A state-changing request from the browser must carry `X-Courier-Request: 1`;
  the pages do. A form on another site cannot, so it cannot act as you.
- Every sign-in, failed sign-in, sign-out and account change is an `events`
  row, and every registry change names the operator who made it.

## The console feed

The console (port 8000) never reads a database, so it cannot look up a
session. At sign-in the API also sets `courier_feed`, a ticket signed with
`FEED_TICKET_SECRET` that the console checks. It lasts `FEED_TICKET_TTL_S`
(10 min); the page renews it before reconnecting. Without a valid ticket the
feed closes with code 4401. A revoked session therefore loses the feed within
one ticket's lifetime.

`FEED_TICKET_SECRET` must be the same for the API and the console, at least
32 characters, and not the example value outside `COURIER_ENV=dev`.

## Verified 2026-09-29, on the laptop

With the running API and console and two test accounts: anonymous requests
refused (401), the feed closed with 4401 without a ticket and delivered the
station state with one, a cookie state change without the header refused
(403), a viewer refused an admin action and the account list (403), sign-out
ended the session, a wrong password refused (401). The full suite, 1,156
tests, passed.
