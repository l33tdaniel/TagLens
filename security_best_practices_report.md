# TagLens Security Findings (Exploitable Only)

## Executive Summary

I reviewed the codebase with parallel focused analysis (auth/session, upload pipeline, frontend sinks, B2 integration, DB/access control) and filtered to findings with concrete exploit paths.

Confirmed exploitable issues:
- 2 × XSS (one reflected, one stored/DOM)
- 1 × open redirect
- 1 × cross-tenant object tampering risk in direct B2 mode
- 1 × resource-exhaustion DoS in direct B2 mode
- 1 × forced-logout CSRF behavior

No exploitable SQL injection or route-level authz bypass was found in the reviewed routes/queries.

---

## Critical

### F-001 — Reflected XSS on `/login` via unescaped `next`
- **Severity:** Critical
- **Location:** `.venv/lib/python3.12/site-packages/robyn/templating.py:19`, `app.py:2017`, `frontend/pages/login/Login.html:90`
- **Evidence:** Robyn’s `JinjaTemplate` creates a Jinja `Environment(...)` without autoescape; `next_path` is rendered directly into an HTML attribute.
- **Impact:** Unauthenticated attacker can execute arbitrary JavaScript in victim browser on the TagLens origin.
- **Exploit path:** Send victim `GET /login?next=/"><img src=x onerror=alert(document.domain)>`; payload is injected into hidden input `value`.
- **Fix:** Enable Jinja autoescaping globally and explicitly escape template variables used in attributes.

### F-002 — Stored/DOM XSS in ACL “Shared With” rendering
- **Severity:** High
- **Location:** `frontend/pages/user_profile/UserProfile.html:567`, `frontend/pages/user_profile/UserProfile.html:576`, `app.py:2146`, `app.py:2183`, `app.py:1727`
- **Evidence:** ACL entries are rendered with `innerHTML` and interpolate `a.email || a.username` without escaping.
- **Impact:** Attacker-controlled account fields can execute JS in another user’s session when ACL is viewed.
- **Exploit path:** Register with crafted `email/username` via direct POST (server only checks non-empty/length), get owner to share a photo to that account, owner opens ACL panel, payload executes.
- **Fix:** Replace `innerHTML` string building with DOM APIs (`textContent`) or sanitize before insertion; enforce server-side character validation for username/email.

---

## High

### F-003 — Cross-tenant B2 object overwrite in direct-upload flow
- **Severity:** High
- **Location:** `app.py:1378`, `app.py:1388`, `app.py:1428`, `app.py:1854`
- **Evidence:** `/api/uploads/b2/init` returns a bucket upload token/url not bound to the returned `file_key`; backend later reads by predictable key pattern.
- **Impact:** Authenticated attacker in direct-upload mode can tamper with another user’s stored object version (integrity break).
- **Exploit path:** Obtain upload token via `/api/uploads/b2/init`, upload to victim key name (`<owner_id>/<photo_id><ext>`), victim fetches latest object by that key.
- **Fix:** Use server-mediated upload or scoped credentials/prefix enforcement; verify uploaded object name + owner mapping on completion.
- **Condition:** Applies when `TAGLENS_DIRECT_B2_UPLOAD=1` and B2 storage is enabled.

### F-004 — Open redirect after login using `//host`
- **Severity:** High
- **Location:** `app.py:840`, `app.py:2017`, `app.py:2045`, `app.py:2111`
- **Evidence:** `_normalize_redirect_path` only checks `startswith("/")`, so `//attacker.example` passes.
- **Impact:** Trusted-domain login flow can redirect users to attacker-controlled domain (phishing/session theft chain).
- **Exploit path:** Victim visits `/login?next=//attacker.example/phish`, logs in, receives redirect to attacker domain.
- **Fix:** Reject `//`, backslashes, and absolute URLs; allow-list internal routes only.

---

## Medium

### F-005 — Direct-B2 large object DoS via unbounded read in job worker
- **Severity:** Medium
- **Location:** `app.py:1448`, `app.py:1474`, `app.py:563`, `app.py:672`
- **Evidence:** Direct upload path has no server-enforced size cap for B2 object; worker later calls `response.read()` into memory and processes bytes.
- **Impact:** Authenticated attacker can trigger memory exhaustion / service instability.
- **Exploit path:** Upload very large object through direct B2 init token, call `/api/uploads/b2/complete`, worker downloads full object into RAM.
- **Fix:** Enforce hard size limits at completion (verify object metadata before queueing) and stream with bounded reads.
- **Condition:** Applies when `TAGLENS_DIRECT_B2_UPLOAD=1` and B2 storage is enabled.

### F-006 — Forced logout CSRF behavior
- **Severity:** Low
- **Location:** `app.py:2211`
- **Evidence:** Logout clears session cookie even when CSRF validation fails.
- **Impact:** Cross-site attacker can force user logout (availability/nuisance).
- **Exploit path:** Cross-site POST to `/logout` without valid CSRF token still returns Set-Cookie clearing session.
- **Fix:** On CSRF failure, return 403 and do not mutate auth cookies.

---

## Not Found (Exploitable)

- No exploitable SQL injection identified in reviewed DB query paths.
- No concrete route-level authz bypass/IDOR found beyond intended sharing mechanisms.
