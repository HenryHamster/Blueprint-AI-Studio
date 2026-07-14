# Resident "Workday" Agent — Clean Workflow Redesign (v2)

**Goal:** get from "I have a machine" to "chatting with my resident strategist"
in a few obvious steps, and *always* know what state you're in. No CLI
archaeology, no silent failures, no confusing modes.

*(Decisions from the 2026-07-04 review folded in.)*

---

## The happy path (what it should feel like)

1. **Add a runner** — Devices → Add runner → pick OS + preset → one clean install
   command → within seconds it shows as **"● Connected · `LAPTOP-LC201HRE` · ready."**
2. **Go live** — switch to **Workday** → session starts in **< 2s** → header reads
   **"Workday · active · `LAPTOP` attached · listening."** (No flag, no toggle.)
3. **Chat** — reply in seconds; step away → it keeps working safely.
4. **Always-truthful status** — Off / Ready / Starting / Active / Blocked (+why+fix)
   / Error (+why). Never a silent timeout.

---

## Design by area

### 1 · Engine — fast, robust start  *(DONE — in review)*
The resident spawn now builds a **minimal in-memory payload** and commits in a
**sub-second transaction** — zero GitHub I/O held across the DB connection (that
was the `connection closed` 500). The agent builds its own context on the runner.

### 2 · Modes — Chat + Workday, drop the rest  *(DECIDED)*
Today's four (`chat` / `check_in` / `monitor` / `continuous`) collapse to the two
that actually mean something; **`check_in` and `monitor` are removed outright** (not
demoted to settings):
- **Chat** — reactive strategist. Cloud, no runner needed. Ask → answer.
- **Workday** — resident strategist. Runner-backed, always-on.
- **Off** — no session.

Migration: any project in `check_in`/`monitor` → **Chat** (or **Workday** if it has a
live runner). Alembic migration to collapse the mode set; the UI offers only
Chat / Workday.

### 3 · Enablement — there is nothing to "enable"  *(DECIDED: no toggle)*
`strategic_session_enabled` was only ever a **dark-launch safety gate** for an
unfinished feature — not a real product setting. **Retire it.** The resident agent
isn't something you toggle: **Workday + a connected runner IS the resident agent.**
The only natural requirement is "do you have a runner?" — and if not, the status
says so (with an Add-runner link), not a hidden flag. Keep the dark flag ONLY until
the fix is proven live (post spawn-fix smoke), then delete it. No Settings toggle,
no inline "enable" CTA — that was solving a problem we're removing.

### 4 · Runner — a clean, conventional install + a guided enroll  *(DECIDED)*
User-friendly and **conventional** — follow what standard developer tools do. The
operator can pre-install common prereqs (node, etc.) the normal way, so we DON'T
over-bundle. A `blueprint-runner` you install like any dev tool (clean entry point,
baked orchestrator URL, token config) — NOT a hand-managed script.
- **Devices → Add runner → a light guided flow.** A small wizard *only because it
  earns its keep*: pick OS + preset → copy a clean install + `blueprint-runner start
  --token <token>` → the UI shows **live "waiting for your runner… ✓ connected on
  this project."** No guessing whether it worked (that was the missing piece).
- **Prereqs follow standard convention** — documented + checked with a clear message
  if missing (like any CLI that needs node ≥ N). Bundle only what's genuinely painful
  to get (`claude`); leave the common stuff to normal install.
- **Baked orchestrator URL** in the runner config → the wrong-URL bug class can't happen.
- Retires today's manual "install node 22 + claude, then `python3
  scripts/local_runner.py` with a URL you have to know."
- **Fix the hostname:** runner shows "None" — heartbeat reports + backend stores the
  real hostname.
- **Fleet panel per runner:** ● status · hostname · project · agent-available ·
  **"ready for Workday" / "not ready (reason)."**

### 5 · Status — one truthful state machine
| State | Meaning | Fix-it |
|---|---|---|
| **Off** | not in Chat/Workday | — |
| **Ready** | Workday selectable, runner live | (enter Workday) |
| **Starting** | session spinning up (< 2s) | — |
| **Active** | live — · runner attached · thinking · replying · idle-working | [Stop] |
| **Blocked** | no runner → [Add runner] | — |
| **Error** | start failed → reason + [Retry] | — |

### 6 · Project clarity
Make the active project obvious in UI + CLI (`--project`/`projects` shipped). A
persistent "current project" indicator.

### 7 · Multi-operator ownership
Session gets an **`owner_user_id`**. A second member entering Workday joins/observes
(no duplicate). Owner leaving stops; non-owner leaving doesn't. Explicit Stop always
available.

---

## Parallel build tracks

**Track A — Engine (backend lifecycle).** Fast minimal spawn *(done, in review)* +
ownership model. `strategic_resident_session.py`, `strategic_spawning.py`, ownership migration.

**Track B — Session-control UX (backend + admin-ui).** Collapse modes to Chat+Workday
(+ migration), **retire the enablement flag**, the status state machine.
`StrategicSessionControl.tsx`, `strategic_state_read_model.py`, mode migration. *(Simpler now.)*

**Track C — Runner distribution & fleet (backend + admin-ui + packaging).** The
product-grade `blueprint-runner` installer (bundled node + claude, baked URL), the new
enroll dialog, hostname fix, fleet visibility. `FleetRunnersPanel.tsx`, `routers/runner.py`,
`routers/admin_runners.py`, a new runner-package/installer. *(Biggest new piece.)*

---

## Resolved decisions
1. **Modes** → Chat + Workday; drop check_in + monitor outright.
2. **Enablement** → no toggle; retire the flag; Workday + runner is the whole thing.
3. **Runner** → product-grade self-contained installer (`blueprint-runner`), not a script.
