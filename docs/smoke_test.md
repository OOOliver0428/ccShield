# Smoke test (Wave 2 / F3 gate)

Manual end-to-end check that the tool is actually usable: QR login, real
danmaku, real SC/guard/medal data, manual ban + WS-push, manual unban.

Run this after any change that touches auth, the danmu/WS pipeline, the
ban routes, or the WBI signing layer. The test steps assume a B站
account you control with **房管 (moderator) permission on the test
room**, and a separate test account (a "small号") you'll use as the
ban target — never ban a real user during smoke.

## Prerequisites

- The repo is set up per `README.md` (`make install` has been run).
- Port `127.0.0.1:8000` is free.
- You have a phone with the B站 app installed, logged into the
  moderator account.
- You have a separate B站 account (or a friend willing to type one
  message in the test room) that you can ban and unban without
  consequences.

## Steps

### 1. Start the dev servers

```sh
make dev
```

You should see uvicorn bind to `127.0.0.1:8000` and Vite serve on
`127.0.0.1:5173`. Both should print "ready" without errors. If Vite
prints a missing-deps error, run `make install` again and retry.

### 2. Open the frontend and scan the QR

In a browser, open `http://127.0.0.1:5173/`.

You should see the login page with a QR code. Open the B站 app on your
phone, point it at the QR, and confirm the login. The page should
poll the QR status; within a few seconds it should show "logged in"
and redirect to the room input.

If the QR polls forever without a "logged in" result, the `LOCAL_TOKEN`
bootstrap may be failing. Open the browser dev tools, look at the
network tab, and check that `/api/auth/bootstrap` returns 200 with a
`LOCAL_TOKEN` header / body. Anything in the 4xx/5xx range is a
regression in the auth bootstrap.

### 3. Verify `.env` was written

In a separate terminal:

```sh
grep -E '^SESSDATA=.+|^BILI_JCT=.+' .env
```

Both lines should be present with non-empty values. If they're empty,
the QR login succeeded but `.env` write did not; check the backend
logs for `Failed to persist cookies` or similar.

**Do not paste the output anywhere.** The whole point of this step is
to confirm `.env` has cookies; the values themselves are the credential.

### 4. Enter the test room

Type or paste the test room's `ROOM_ID` (the numeric id, not the short
link) into the room input, submit. Within 1-2 seconds you should land
on the room page with live data flowing.

Use a room where:

- You have moderator permission (房管).
- The streamer is actually live. An offline room returns no danmaku
  and the WS connect may sit idle — that's not a regression, just
  nothing to show.

### 5. Verify the live data surfaces

Within a few seconds of connecting, you should see:

- **Danmaku list** scrolling with real messages from viewers. Send
  one yourself from a second device to confirm round-trip.
- **SC (Super Chat)**. If you can afford it, send yourself a 1-yuan
  SC and confirm it appears in the SC panel. If you can't, ask a
  friend to send one. SC envelopes are different from regular danmu
  and the panel renders them differently; the shape should match
  B站's SC card.
- **Guard badges** (舰长 / 提督 / 总督). If you have a guard in the
  room, they should show in the participant list or wherever guards
  are surfaced. If not, that's a feature gap, not a smoke-test
  failure.
- **Medals**. Fans-medal badges on the danmaku list should match the
  sender's actual medal if you can verify from the B站 app.

If danmaku shows up but SC/guards/medals don't, that's a panel-specific
regression. Cross-check the WS subscribe params in the dev console.

### 6. Ban the test account (small号)

From the test account, post a danmaku like "smoke test 1". Wait for
it to appear in the list. Click the ban control on that entry
(usually a "封禁" button or right-click menu; the exact UI depends on
your build). Confirm.

Within ~1 second, the entry should disappear from the danmaku list
(it's a ban, not a delete, but the panel treats them equivalently for
display). Open the banlist view: the test account should be there.

Now watch the WS frames in the browser dev console (Network tab →
the `/api/ws/banlist` connection). You should see a banlist update
event with the test account's UID shortly after the API call returns.
The WS-push is the whole point: clients stay in sync without polling.

**Sanity check the B站 app.** Open the test room in your phone's
B站 app. The test account's messages should also be blocked there
after the ban propagates. If the app doesn't block them, B站's
cache is stale; give it a minute.

### 7. Unban and verify the reverse

Click the unban (解除) control on the test account in the banlist.
Within ~1 second, the entry should disappear from the banlist view and
the next WS-pushed frame should carry a removal. The test account's
next danmaku in the room should re-appear in the list (if they send
one).

## Pass criteria

The smoke test passes when:

1. Steps 1-4 succeed without console errors.
2. At least one of: danmaku list (live messages), SC panel (a real SC),
   or guard/medal display is verified end-to-end against the real B站
   API.
3. The ban → WS-push → banlist visible loop in step 6 completes.
4. The unban → WS-push → banlist empty loop in step 7 completes.

Anything less is a regression. Note which step failed, capture the
backend log tail (`uv run uvicorn` output) and the relevant browser
devtools console output, and bring it to the issue you're filing.

## What this gate does NOT cover

- The credential redaction path in `scripts/capture_fixtures.py`. Run
  `python -m scripts.capture_fixtures --dry-run` instead; it doesn't
  need a real `.env`.
- WBI signing correctness when B站 rotates their mix-key table. That's
  covered by `backend/tests/test_wbi.py` (layer 1) plus a manual
  inspection of one fresh capture against the live API.
- Multi-tab sync. Single tab is enough for the gate; if you really
  care, open a second tab and confirm both stay in sync via the same
  WS.
- The internal WS protocol framing. The frontend WS client has its
  own unit tests; layer-1 already covers the JSON envelopes we send
  on top.
