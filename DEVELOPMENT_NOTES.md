# SimCorp SF Case Logger: Development Notes

Extensive record of **what was tried, what failed, why, and the reasoning behind
the final design**. Written so that whoever maintains this next does not have to
re-discover the dead ends. The SimCorp portal is Salesforce Experience Cloud
(Lightning), which drives most of the non-obvious problems below.

- **App source:** `sc_case_logger.py` and the sibling `sc_*.py` modules in this repository (§16.1)
- **What it does:** GUI → log in to the SimCorp support portal → create a
  `Dimension > Error` case → fill it in → submit → upload files → open the created
  case → post the files as feed comments.
- **Runtime target:** `C:\AppWhitelist\Simcorp_SF` (application-whitelisted tree).

---

## 1. Tech stack decisions

### GUI: tkinter
Chosen because it is in the Python standard library (zero extra dependency, always
available, trivially bundled by PyInstaller). No need for PyQt/wx.

### Browser automation: Playwright (not Selenium)
The portal is Salesforce Lightning: content is rendered by Lightning Web Components
(LWC) inside **shadow DOM**, loads asynchronously, and has few stable ids. Playwright
was chosen because:
- Its locators (`get_by_role`, `get_by_text`, `get_by_label`, CSS) **automatically
  pierce open shadow roots**. Raw `document.querySelectorAll` does **not**, and this
  bit us repeatedly (see §3).
- Auto-waiting on locators handles the async rendering better than Selenium's
  explicit waits.

### Date picker: tkcalendar
tkinter has no native calendar widget. `tkcalendar` provides a real calendar popup.
The date fields are **optional/blank by default**, so we use a plain `Entry`
(blank) plus a "Pick…" button that opens a `tkcalendar.Calendar`. A `DateEntry`
always holds a value and can't be left blank.

---

## 2. Dependency install pain (greenlet / Python version)

**Symptom:** `pip install playwright` failed with *"Failed to build greenlet …
Could not build wheels for greenlet"*.

**Root cause:** Playwright depends on `greenlet` (a C extension). The machine has no
C compiler, and the **latest** greenlet had **no prebuilt wheel for Python 3.9**, so
pip tried to compile it from source and failed.

**Fix (pinned versions):**
- `greenlet==3.0.3`: the newest greenlet that still ships a cp39 wheel (and also has
  cp312 wheels, so it works on the 3.12 whitelisted Python too).
- `playwright==1.44.0`: the Playwright release that pins exactly `greenlet==3.0.3`.

These pins are the safest common denominator across Python 3.9 to 3.12. Do **not**
bump Playwright without checking that its pinned greenlet has a wheel for the target
Python.

**Two Pythons on this machine (important):**
- `C:\Program Files\Python39\python.exe` (3.9), first on PATH.
- `C:\AppWhitelist\Python\python.exe` (3.12), the **whitelisted** one.

The launcher/exe must prefer the **whitelisted** Python, because a `python.exe`
under `C:\Program Files` may be blocked by application whitelisting.

---

## 3. Shadow DOM: the recurring gotcha

Early diagnostics dumped the DOM with `document.querySelectorAll(...)` inside
`page.evaluate` and came back almost **empty** (only a search box), even though the
page clearly showed a category tree and a data table.

**Root cause:** LWC renders inside **shadow DOM**, and a plain `querySelectorAll` from
`document` does not cross shadow boundaries.

**Consequences / rules learned:**
- Use **Playwright locators** (`get_by_*`, CSS). They pierce open shadow roots.
- When you must use `page.evaluate`, you have to **recurse into `shadowRoot`s**
  manually (the diagnostic dumps in the probes do exactly this).
- A locator can *resolve* an element that Playwright still reports as **"not
  visible"** (e.g. a `<span>` label inside a custom control). The fix is to target
  the actual interactive ancestor (a `<button>`, `<a role="option">`, etc.), not the
  text node.

---

## 4. Login

**First attempt (failed):** guessed CSS selectors `#username`,
`input[name='username']`, `input[type='email']`, `#password`, `#Login`. The fields
were **never filled** and the run appeared frozen.

**Two root causes:**
1. The login inputs have **no id/name**. They only have **placeholder text**
   ("Username", "Password"). The real ids are dynamic (e.g. `141:0`).
2. Each wrong selector waited the full **30 s** default timeout before failing, so
   several misses in a row looked like a hang.

**Fixes:**
- Use `get_by_placeholder("Username")` / `get_by_placeholder("Password")` first,
  with id/name/type selectors only as fallbacks.
- Lower the **per-action timeout to 8 s** (`page.set_default_timeout(8000)`) so a
  missed selector falls through to the next fallback quickly. Navigation and the
  dialog use explicit longer timeouts.
- Added a `_dump_inputs()` diagnostic that lists every input's id/name/placeholder
  when the username fill fails, so selector problems are diagnosable from the log.

The **Log in** button has no id either. It's matched by `get_by_role("button",
name="Log in")`.

---

## 5. Service catalog navigation

### Category "Dimension" appears twice
`get_by_text("Dimension", exact=True)` returns **two** matches:
- the **left-nav** item (a `<span>`, small x ≈ 92),
- the **right-hand "Case Catalogue" instructions** panel (a `<p>`, large x ≈ 906).

Clicking `.first` sometimes hit the wrong one.

**Fix:** `_click_leftmost()`: enumerate the matches, compare `bounding_box().x`,
click the **left-most** one (the nav item).

### Catalog item "Error"
`get_by_text("Error", exact=True).first.click()` (it's a `<span>` in the table row).

### "Log a Case" dialog loads asynchronously
After clicking Error, the modal shows a **spinner** before the fields render.
**Fix:** wait for the "Log a Case" heading, then **poll** up to 30 s for the Subject
field to exist before touching anything.

---

## 6. The "Log a Case" form (step 1)

All fields resolve cleanly with `get_by_label` (labels are `*Priority`,
`*Operations & Onboarding`, `*Installation`, `Subject`, `Description`):

| Field                     | Control            | How it's driven                               |
|---------------------------|--------------------|-----------------------------------------------|
| Priority                  | native `<select>`  | `get_by_label(...).select_option(label=...)`  |
| Operations & Onboarding   | native `<select>`  | same                                          |
| Installation              | native `<select>`  | same                                          |
| Subject                   | `<input>`          | `get_by_label("Subject")`                     |
| Description               | `<textarea>`       | `get_by_label("Description")`                 |

**Dropdown option values captured from the live site** (baked into the GUI so the
combo boxes match the portal exactly):
- Priority: `--None--, 1-Critical, 2-High, 3-Medium, 4-Low`
- Operations & Onboarding: `--None--, Operation, Transition`
- Installation: `--None--, TEST / 26.04, DEV / 26.04, MIGR / 26.04, GOLD / 26.04, * PROD / 26.04 - MainProd`

---

## 7. Business Impact (step 2): the date-format trap

Fields on step 2:

| Field                       | Selector that works                         |
|-----------------------------|---------------------------------------------|
| Business Impact             | `textarea:visible` (see caveat below)       |
| When did it happen?         | `input[name='datWhenDidItHappen']`          |
| When did it last work?      | `input[name='datWhenDidItLastWork']`        |
| Reproducible by other users | `input[name='checkReproducibleOtherUsers']` |

**Caveat, the Business Impact label is ambiguous:** `get_by_label("Business Impact")`
matches **two** elements: the textarea **and** the ⓘ "Help" button that shares the
label text. `.first` hit the Help button. **Fix:** target `textarea:visible`
directly.

**The date format trap:** typing `13/07/2026` was rejected. The field's own error
message revealed the required format: **"allowed format 31. Dec 2024"**, i.e.
`d. Mon yyyy` (e.g. `13. Jul 2026`, no leading zero on the day).
**Fix:** `to_site_date()` converts the GUI's ISO date (`YYYY-MM-DD`) to
`f"{dt.day}. {dt.strftime('%b')} {dt.year}"`. The GUI stores ISO internally so it is
locale-independent. The site format is produced only at fill time.

**Playwright version quirk:** `locator.filter(visible=True)` does **not** exist in
Playwright 1.44. Use the CSS pseudo `:visible` instead.

---

## 8. Submit + wizard file upload

Clicking the final **Next** creates the case, then the wizard shows an **Upload
Files** step. Files are attached via the hidden `input[type=file]`
(`set_input_files`), then **Done/Finish**. This upload puts the files on the case's
**Related Files** (they later appear under "Owned by Me", important for §9).

Note: the wizard's "Upload File" input is **single-file** (see §9 for where this
matters more).

**(Removed) safe-mode:** during development there was a "Submit case" checkbox and a
`submit` flag so the run could stop before creating a real case (plus
`SCL_HEADLESS=1` and `SCL_DEBUG_SHOTS=1` env toggles for testing). Per request this
was removed. The program now **always** submits, guarded by a single confirmation
dialog. `SCL_DEBUG_SHOTS` still exists for troubleshooting (saves screenshots to
`_screenshots\`).

---

## 9. Posting files to the case feed: the long saga

This was by far the hardest part. Recorded in detail because almost every
"obvious" approach failed for a non-obvious reason.

### 9.1 The comment composer
- Placeholder text is **"Write a comment…"**.
- The paperclip is a `<button title="Attach file">` that appears **after** the
  composer is focused.
- Clicking the placeholder **expands** it (`feeds_placeholding-comment-publisher`)
  into a rich editor. After expansion the placeholder locator no longer applies, so
  you must type with **`page.keyboard.type()`** into the focused editor, not
  `locator.fill()` on the placeholder.

### 9.2 Uploading vs selecting existing files
First attempt used the Select File dialog's **"Upload File"** button + a file
chooser and tried to send **both** files at once:
> `Non-multiple file input can only accept single file`

The upload input is single-file. **But** the wizard (§8) already uploaded both
files, so they appear under **"Owned by Me"** in the Select File dialog. The reliable
approach is therefore to **select the existing file by name**, not re-upload.

### 9.3 Selecting a file row
- The dialog's file list **loads asynchronously** (spinner). Must poll until file
  rows exist before selecting. An early check found zero files.
- Clicking the filename **`<span>`** failed: Playwright reported it **"not
  visible"**. The actual selectable element is the wrapping **`<a role="option">`**.
  **Fix:** `get_by_role("option", name="<display name>")`, where display name is the
  filename **without extension** (Salesforce strips it, e.g. `Test_upload`).

### 9.4 The submit button: viewport-dependent bug
The feed has several "Comment" buttons: small **action links** (~18 px tall,
x ≈ 110) and the **brand submit** button (~32 px tall, class `slds-button_brand`,
x ≈ 715 at 1280-px width).

First fix tried "click the Comment button with **x > 400**". This worked in probes
(default 1280 viewport) but **failed in the real app**, which launches Chromium with
**`no_viewport=True`** (real window size). At that window size the brand button's x
was **below 400**, so the code fell back to an action link and the comment never
posted. It sat unposted in the composer.

**Fix (viewport-independent):** `_click_comment_submit()` prefers
`button.slds-button_brand:has-text("Comment")`. The fallback picks the **tallest**
visible/enabled "Comment" button (≥ 24 px). The submit is ~32 px, links are ~18 px.
Never rely on absolute x/y coordinates.

### 9.5 Composer mismatch on a freshly-created case
On a **fresh** case the feed has an extra "created this case" record post, so
`get_by_placeholder(...).first` (text), `Attach file`.first, and the submit button
could belong to **three different composers**. Result: text in one composer, the
file attached to another, and neither posted correctly.

Explored **feed-item scoping** (`div.slds-feed__item`) so text + attach + submit all
happen inside one item. This clarified the structure but the deciding fix was §9.6.

### 9.6 Fresh-case timing
Immediately after creation the feed is still settling and the comment composer isn't
ready, so the post silently failed. Running the **same** attach code against the same
case a minute later worked perfectly.
**Fix:** after navigating to the created case, **reload the page** and **wait for the
"Write a comment…" composer** to exist before attaching. Reload gives a clean, fully
rendered feed, the same conditions under which it always worked.

### 9.7 One attachment per comment (hard Salesforce limit)
Attempts to put **both** files on **one** comment all failed:
- Selecting a 2nd file in the dialog **replaced** the 1st (`selectedCount` stayed 1,
  so it's single-select).
- A 2nd Attach→Add cycle **replaced** the composer's attachment rather than
  appending.

**Conclusion:** Salesforce **Chatter comments allow only ONE attachment each.**
(Top-level **posts** allow multiple.)

### 9.8 Why not "one post with both files"
The user preferred a single feed **post** (posts support multiple attachments). But
the top-level post publisher's toolbar buttons are **icon-only with no
`aria-label`, `title`, or discernible icon symbol**, so there is no reliable handle to
click its attach control. Automating it would mean clicking toolbar icons by blind
position, which breaks the moment SimCorp tweaks the UI.

**Decision:** implement **two comments, one file each**, using the proven, reliable
comment flow. Both files land in the feed with the note. This trades "single post"
elegance for reliability. (If a single post is ever mandatory, it needs a fragile
position-based click and should be treated as best-effort.)

### 9.9 Verification pitfalls (how I fooled myself)
- **"posted a file"** text only appears for **top-level posts**, not comments, so
  counting it made a successful comment look like a failure.
- A leaf-text search (`textContent === "Test_log"`) **missed** the attachments
  because Salesforce wraps the filename in nested elements. A **broad** search found
  them as **"View file Test_log"** / **"View file Test_upload"**. Lesson: verify with
  a tolerant search, and confirm visually with a screenshot.

---

## 10. Navigating to the created case
After **Finish**, the wizard usually navigates straight to `…/s/case/<id>/…`.
`_open_created_case()` first checks the URL for `/s/case/`. If it's not there, it opens
**My Cases** and clicks the row matching the Subject. In practice the direct
navigation path is what fires.

---

## 11. Packaging & deployment

### 11.1 Launcher `.bat` (the pre-exe approach, now superseded)
`run_simcorp.bat` self-installed the pinned packages + Chromium on first run,
created shortcuts, and launched the app. Key points that still matter:
- **Prefer the whitelisted Python** (`..\Python\python.exe`), fall back to PATH.
- **Keep the browser in-folder** via `PLAYWRIGHT_BROWSERS_PATH=%HERE%browsers` so
  Chromium runs from inside `C:\AppWhitelist` (whitelist-friendly + portable).
- **PowerShell precedence bug** hit while writing the shortcut creator:
  `@($HERE + 'a.lnk', $b)` is mis-parsed: `+` binds the array, producing **one**
  concatenated string. Wrap each element in parentheses: `@(($HERE+'a.lnk'), $b)`.
  (The `.bat` avoids this because `cmd` expands `%HERE%` into a literal first.)

### 11.2 Standalone `.exe` (current deliverable)
Built with **PyInstaller**. Decisions:
- **onedir, NOT onefile.** A onefile exe unpacks a **Node driver** and DLLs to
  `%TEMP%\_MEI…` at runtime and executes them from there, and application whitelisting
  would block that. onedir keeps **every** executable inside
  `C:\AppWhitelist\Simcorp_SF`.
- `--collect-all playwright --collect-all tkcalendar --collect-all babel` so the
  Playwright **Node driver** + package data, tkcalendar data, and babel locale data
  are all bundled. (Missing the driver is the classic "Playwright works in dev but
  not in the exe" failure.)
- **Chromium kept external** (`browsers\`), not embedded. Embedding works but bloats
  the exe and slows startup. The app is **frozen-aware**: `_app_dir()` returns
  `dirname(sys.executable)` when `sys.frozen` is set (PyInstaller), else the script
  dir. `PLAYWRIGHT_BROWSERS_PATH`, the config file, and screenshots all resolve
  beside the exe.
- **De-risked before the big build:** first built a tiny console exe that only
  launches Chromium, to prove bundled Playwright + the external browser path work.
  Only then built the GUI exe. Verified the GUI exe opens its window
  (`SimCorp - Log a Case`) from the final folder.

### 11.3 Final folder layout
```
C:\AppWhitelist\Simcorp_SF\
  SimCorp SF.exe      the app
  _internal\          bundled Python runtime + libraries  (keep)
  browsers\           Chromium for Playwright             (keep)
  SimCorp SF.lnk      shortcut (also on Desktop)
  README.txt          end-user instructions
  DEVELOPMENT_NOTES.md this file
```
Deploy by copying the whole folder to `C:\AppWhitelist\Simcorp_SF` on another
PC. **No Python, no pip, no internet** required.

---

## 12. Rebuilding the exe after code changes

From a machine with the whitelisted Python + PyInstaller installed:

```bat
:: from a scratch build folder, next to a copy of sc_case_logger.py
"C:\AppWhitelist\Python\python.exe" -m PyInstaller --noconfirm --windowed ^
  --name "SimCorp SF" ^
  --collect-all playwright --collect-all tkcalendar --collect-all babel ^
  sc_case_logger.py
```

Then:
1. Copy `dist\SimCorp SF\SimCorp SF.exe` and `dist\SimCorp SF\_internal\` into the
   deployment folder.
2. Ensure a `browsers\` folder sits next to the exe. To (re)create it:
   `set PLAYWRIGHT_BROWSERS_PATH=<folder>\browsers` then
   `"C:\AppWhitelist\Python\python.exe" -m playwright install chromium`.
3. Point the shortcut at `SimCorp SF.exe`.

Packages the whitelisted Python needs to build:
`greenlet==3.0.3`, `playwright==1.44.0`, `tkcalendar`, `pyinstaller`.

---

## 13. Known limitations / watch-outs

- **Not code-signed.** SmartScreen/AV may warn on first run on a new machine.
- **Selectors are UI-coupled.** SimCorp is Salesforce, so a portal UI change can break
  the flow. The most fragile spots: the left-nav category click, the Select File
  dialog `role=option` rows, and the brand "Comment" submit button. All use
  role/label/class rather than absolute coordinates to minimise this.
- **One file per comment** is a Salesforce limit, not a bug (§9.7). Multiple files =
  multiple comments.
- **`no_viewport=True`** means never use absolute x/y for element selection (§9.4).
- **Credentials** in `.sc_case_logger.json` are **base64-obfuscated, not
  encrypted**. Do not treat that file as secure.
- **Every run creates a REAL case** (single confirmation only). There is no dry-run
  mode anymore.

---

## 14. Cleanup pass + subject prefix (Aug 2026)

### 14.1 Why
The single 989-line file had grown one very long `run_automation()` function, an
untyped `fields` dict shared between GUI and worker, terse identifiers (`cfg`,
`_try`, `_shot`, `bi`, `fbox`, `pw`) and several `except Exception: pass`
handlers that hid real failures. None of that was wrong, but it made the flow
hard to follow and easy to break. The portal-facing behaviour is **unchanged**:
every selector, fallback order and tuned `wait_for_timeout` value was preserved
verbatim, because those are the hard-won parts (§3 to §10).

### 14.2 What changed
- **`run_automation()` split into named steps**: `log_in_to_portal`,
  `open_log_a_case_dialog`, `fill_case_details_step`,
  `fill_business_impact_step`, `complete_upload_wizard`, `open_created_case`,
  `post_files_as_case_comments`. The orchestrator
  `run_case_logging_automation()` now reads as the flow itself.
- **`fields` dict → `CaseSubmission` dataclass**, and the config dict →
  `StoredSettings` dataclass. A mistyped field name is now an immediate
  `AttributeError` instead of a silent `KeyError`/`None`.
- **The repeated `for _ in range(30): if cond: break; wait(1000)` polling loop**
  (it appeared five times) is now one `wait_until(page, condition, seconds)`
  helper. Same behaviour, one place to fix.
- **`wait_for_network_idle()`** replaces the four copies of
  `try: wait_for_load_state("networkidle") except PWTimeout: pass`. The comment
  there explains *why* the timeout is expected (Lightning long-polls, so
  networkidle rarely fires).
- **Narrow exception handling.** Bare `except Exception: pass` is gone except in
  the two places where catching everything is the actual design, each with a
  comment: `try_strategies()` (the selector-fallback mechanism) and the
  top-level automation guard (must report and still leave the browser open).
  Everything else catches `OSError`, `ValueError` or `PlaywrightError`.
- **Full descriptive names throughout**: `try_strategies`, `report_progress`,
  `save_debug_screenshot`, `click_comment_submit_button`, `credentials_frame`,
  `installation_combobox`, and so on. No single-letter or abbreviated locals.
- **The GUI `_build()` method split** into one `_build_*_section()` per frame.
- **Dead import removed**: the old file imported `tkcalendar.DateEntry` (never
  used, the date fields must be blankable, see §1) and then re-imported
  `Calendar` locally. Now `Calendar` is imported once at the top.

### 14.3 Subject prefix (new feature)
A tick box **"Add subject prefix"** sits directly above the Subject field with a
small text box beside it, shown only while the box is ticked. Default text is
`UAT: `. When ticked, the subject sent to the portal is
`<prefix><subject>`.

- `apply_subject_prefix()` inserts a single space if the stored prefix does not
  already end with one, so `"UAT: "` and `"UAT:"` both produce
  `UAT: <subject>`.
- The **prefixed** subject is what the whole run uses, including
  `open_created_case()`, which falls back to finding the case in My Cases *by
  subject*. Prefixing anywhere later would have broken that lookup.
- The prefix text and the tick state persist in `.sc_case_logger.json`
  (`subject_prefix`, `subject_prefix_enabled`).
- The confirmation dialog now shows the final subject, prefix included, so the
  prefix cannot be applied unnoticed.

### 14.4 Settings file compatibility
`load_settings()` still reads the old `ops` key written by earlier versions, and
falls back to defaults when the file is missing or corrupt. New key names are
`operations_and_onboarding`, `subject_prefix`, `subject_prefix_enabled`.

One behaviour change: **"Remember on this PC" now governs the username as well
as the password.** Previously the username was saved regardless of the tick.
Non-credential preferences (priority, ops, installation, subject prefix) are
always saved.

### 14.5 Verification
Checked with the whitelisted Python 3.12 before rebuilding: `py_compile`,
round-trip tests of `apply_subject_prefix`, `to_portal_date` (including the
rejected `13/07/2026` form), settings load/save and legacy-`ops` reading, and a
headless Tk pass that builds the window, toggles the prefix box and asserts the
composed subject reaching `CaseSubmission`. The browser flow itself is unchanged
and was not re-run against the live portal, because it creates real cases.

### 14.6 Rebuild gotcha: build INSIDE the whitelist tree

The first rebuild of the cleaned source was done in a scratch folder under
`%TEMP%`. The resulting exe died instantly with:

```
File "socket.py", line 52, in <module>
ImportError: dynamic module does not define module export function (PyInit__socket)
[PYI-...:ERROR] Failed to execute script 'pyi_rth_multiprocessing'
```

This looks like a broken build, and it is not. The collected `_socket.pyd` was
**byte-identical** to the one in the known-good deployed `_internal\`. The
failure is **application whitelisting**: the exe was being run from `%TEMP%`,
outside `C:\AppWhitelist`, so loading the bundled `.pyd`/DLLs was blocked.
It fails in PyInstaller's own runtime hook, before a single line of
`sc_case_logger.py` runs, which is the tell that it is an environment problem
rather than a code one.

**Rule: build and test the exe from a folder inside `C:\AppWhitelist`.**
Rebuilding in `C:\AppWhitelist\_build_simcorp_sf` produced an exe that
opens its window normally. This is the same reason §11.2 chose onedir over
onefile (onefile unpacks to `%TEMP%` at runtime and would hit exactly this).

---

## 15. Research: reading cases (Aug 2026)

Findings from a **read-only** exploration of the live portal (nothing was posted),
gathered before designing the "Respond to a case" tab.

### 15.1 Portal navigation: corrected URLs

| What | Reality |
|------|---------|
| My Cases | **`/s/case-lists`** |
| `/s/mycases` | **Dead, renders "Invalid Page"** |
| Case page | `/s/case/<18-char Id>/<slug>`, e.g. `/s/case/500Tb00001XXXXXXXXX/<subject-slug>` |

**This is a live bug in `open_created_case()`**: its last-resort fallback is
`page.goto("https://supportportal.simcorp.com/s/mycases")`, which lands on
"Invalid Page". It has never mattered because the wizard normally navigates
straight to `/s/case/...` (§10), but the fallback cannot work as written.

Also: the nav items are **`role="menuitem"`**, not links, with
`href="javascript:void(0);"`. `get_by_role("link", name="My Cases")`, which the
current fallback uses, **cannot match them**. `get_by_role("menuitem", ...)`
works.

### 15.2 Resolving a case number to a URL
`/s/case-lists` renders a list view whose rows contain `<a href="/s/case/...">`
for both the case number and the subject. Scanning anchors whose text matches
`^\d{8}$` yields a clean `{case number: URL}` map, 26 entries on the default
view. No fragile global search needed.

**Caveat:** the default pinned list view is **"All Open Cases"**, so a closed
case will not be in that map. The list-view picker offers ~39 views including
**"All Cases"**, "All Closed Cases", "My Cases". Any case-number lookup must
switch to **"All Cases"** first, or it will silently fail to find closed cases.

### 15.3 Case feed structure (Aura Chatter, light DOM, no shadow piercing needed)

Unlike the Log-a-Case wizard (§3), the case feed is **Aura**, not LWC, so plain
`document.querySelectorAll` works. Useful selectors:

| Purpose | Selector |
|---------|----------|
| Feed item | `.forceChatterFeedItem` (**not** `article.slds-feed__item`, which is inconsistent) |
| Post body | `.forceChatterMessageBody` |
| Timestamp | `.cuf-timestamp` |
| Comments on an item | `.forceChatterComment`, `.cuf-commentItem` |
| Comment author | `.cuf-commentNameLink` |
| Comment age | `.cuf-commentAge` |
| Item has comments | `.has-comments`, count in `.qe-commentCount` |
| Attachment block | `.forceChatterFeedAuxBody` |
| Highlights (fields) | `.forceHighlightsPanel` |

**Ordering: the feed renders newest-first.** Verified on case 00900006. Item 0
is 1h ago, item 3 is 2h ago. To display a thread in reading order, **reverse the
DOM order**. Do *not* try to sort by the displayed timestamp: Chatter shows
**relative** times for recent items ("1h ago", "3 hours ago") and **absolute**
ones for old items ("13. July 2026 at 09.47"), so timestamp text is not sortable.
DOM order is authoritative.

**Three kinds of feed item**, distinguishable by header text:
1. **Text post**: real content, e.g. "Jane Smith (SimCorp Asia Pty. Ltd.)".
2. **Record update**: "… updated this record." + an aux body like
   "Status / New to In Progress".
3. **Case creation**: "… created this case." + aux body "00900006 / View more
   details". This is the item the app's own file comments attach to.

Each item ends with `Like / Comment / <n> comment(s) / <n> view(s)` and its own
**"Write a comment..."** composer, so every item is independently repliable,
which is what makes per-thread replies possible (and what made §9.5 tricky).

### 15.4 The case Description is NOT on the case page

Confirmed by exhaustive search: the record tabs are only **Feed** and
**Related**. There is no Details tab (the "More" seen in a tab dump is the site
nav overflow, not a record tab). The **Related** tab holds only Files, Articles,
Error Logs, Contact Roles. Searching the whole rendered page for the literal
string "Description" returns **nothing**.

The only case fields exposed are in `.forceHighlightsPanel`:
`Subject, Case Number, Type, Priority, Contact Name, Status`.

The Description sits behind the **"View more details"** link on the
"created this case" post, which expands `forceChatterFeedAuxBodyRecordSummary`
inline. Clicking it headless raised Salesforce's "Sorry to interrupt / CSS Error"
dialog, so **this one step still needs confirming in a headed session**.

### 15.5 Encoding trap
Portal text contains zero-width spaces (`​`) and non-breaking spaces
(`\xa0`), from @-mentions in particular. Printing it through a cp1252 console
raises `UnicodeEncodeError`. Any text pulled from the feed must be normalised
before display or re-posting.

---

## 16. Two tabs and the module split (Aug 2026)

### 16.1 The app is now several modules
One 1,400-line file could not absorb a second tab and a long-lived browser
session. The build entry point is still
`sc_case_logger.py`, and PyInstaller follows the imports, so the build command
only gains the sibling files (§16.6).

| Module | Holds |
|--------|-------|
| `sc_case_logger.py` | the Log-a-Case wizard automation, and it starts the app |
| `sc_settings.py` | paths, stored settings, `CaseSubmission` |
| `sc_portal.py` | login + the shared waiting / locator-fallback helpers |
| `sc_portal_session.py` | the long-lived headless browser behind the Respond tab |
| `sc_case_reader.py` | reading a case thread, and replying to one post |
| `sc_gui.py` | the window |

`try_strategies`, `wait_until` and `wait_for_network_idle` had been written
twice. They now live once, in `sc_portal.py`.

### 16.2 Respond tab
Case number → thread → reply under a chosen post.

- Resolution uses the **global search route** `/s/search/All/Home/<number>`,
  found by typing into the header search box and reading the URL it lands on.
  This finds open **and** closed cases. The list views are filtered and would
  silently miss closed ones. `/s/global-search/<number>` is **not** a valid
  route. That guess returned nothing.
- The list-view `<select>` was investigated as an alternative and rejected: the
  element holding the ~39 view names is a **native `<select class="slds-select">`
  that is not visible**, so `select_option` retries until it times out.
- The thread is displayed **oldest first**, by reversing DOM order (§15.3).
- Post bodies come from `.forceChatterMessageBody` when present, and otherwise
  are parsed out of the item's rendered text between "Actions for this Feed
  Item" and the `Like`/`Comment` footer. The class is **not** reliably present
  by the time the feed settles. An early version showed every post as
  "(no text)" because it trusted the class alone.

### 16.3 Replying is scoped to one feed item
Each feed item contains its **own real `<input placeholder="Write a comment...">`**
(4 items → 4 inputs), so a reply must be scoped or it lands on the wrong post.
`post_reply()` works entirely inside
`page.locator('.forceChatterFeedItem').nth(post.dom_index)`.

`CasePost.dom_index` exists precisely because the display order is reversed:
position 1 on screen is the *last* item in the DOM. Never target a composer by
display position.

**Verified without posting anything:** typing into the composer of the targeted
item put the draft in item 0 only (`has_draft=True` for item 0, `False` for
items 1-3), and exactly one brand submit button resolved inside that item:
visible, enabled, **32 px** tall, matching the submit-vs-action-link rule of
§9.4. The final submit click itself is unexercised. It reuses the same
brand-class-then-tallest logic already proven by the Log-a-Case flow.

### 16.4 The Description does not exist on the portal (confirmed)
Do not spend time looking for it again. The case page has only **Feed** and
**Related** tabs. Related holds Files, Articles, Error Logs, Contact Roles. A
whole-page text search for "Description" returns nothing.

The "View more details" link on the creation post is **not** an expander. It
**navigates to `/s/feed/<feedItemId>`**, whose record summary shows only
Subject, Case Number, Type, Priority, Contact Name, Status. It also raises
Salesforce's "Sorry to interrupt / CSS Error" dialog, and because it leaves the
case page it **breaks every reply composer**. An earlier version called it while
reading a case and made replying impossible.

`_read_description()` was therefore removed. `render_thread()` states plainly
that the portal does not publish the Description, rather than showing a
misleading blank.

### 16.5 Why the Respond tab owns a browser thread
Playwright's sync API is **thread-affine**: a page may only be touched from the
thread that created it. Loading a case and replying to it are separate user
actions minutes apart, and logging in costs ~28 s. `PortalSession` therefore
owns a private worker thread and takes work as callables on a queue. The GUI
submits from short-lived threads and marshals results back with `after(0, ...)`.
Measured: 28 s to start the session, then ~28-41 s per case load with **no
second login**.

### 16.6 Rebuilding
```bat
"C:\AppWhitelist\Python\python.exe" -m PyInstaller --noconfirm --windowed ^
  --name "SimCorp SF" --icon icon.ico ^
  --collect-all playwright --collect-all tkcalendar --collect-all babel ^
  sc_case_logger.py
```
Copy **all** of `sc_*.py` plus `icon.ico` into the build folder first, and build
inside `C:\AppWhitelist` (§14.6). The window icon is set separately at
runtime from `icon.PNG` beside the exe. Tk needs a PNG, the exe needs an ICO,
so both files ship. A missing icon is ignored rather than fatal.

---

## 17. The case browser and its cache (Aug 2026)

The Respond tab began as "type a case number". That is unusable day to day, so
it is now a browser over the whole case list, backed by an on-disk cache.

### 17.1 The delta signal
The portal's case list view carries a **`Last Modified Date`** column, e.g.
`18.08.2026 15.04` (minute precision). One list refresh yields that value for
every case at once, which makes an exact, cheap staleness test:

> a cached thread is stale exactly when the case's `Last Modified Date` differs
> from the one recorded when that thread was read.

Everything else is served from the cache. Measured: **20.8 s** to read a thread
from the portal versus **0.001 s** from the cache.

Column positions are not fixed (the view's columns can be reordered per user),
so the harvester reads the header row and looks columns up **by name**. If
`Last Modified Date` is ever missing it says so in the log and falls back to
re-reading everything, rather than silently serving stale text.

### 17.2 The full column list
`Case Number`, `Subject`, `Type`, `Case Catalogue Category`,
`Case Catalogue Item`, `Impact`, `Urgency`, `Status`, `Pending Reason`,
`Contact Name`, `Product / Service`, `Created Date`, `Last Modified Date`.

Status values seen in practice: `In Progress`, `Waiting for Customer`,
`Completed by SimCorp`, `Pending`.

### 17.3 Loading every row
Salesforce list views load more rows as they are scrolled. `_load_every_list_row()`
scrolls until the row count holds still for two passes. Verified to terminate
immediately on a short list (23 rows, no growth).

### 17.4 SQLite, not JSON
`_cache\cases.sqlite3` with three tables: `cases` (the list), `threads` (what has
been read, as JSON), `meta` (last refresh time). SQLite because writes are
atomic: a half-written JSON file would lose the whole cache, which matters for
something meant to run unattended for months. It is in the standard library, so
the build gains nothing.

**Each call opens its own connection.** sqlite3 connections cannot be shared
across threads, and this cache is touched from the Tk thread and from worker
threads. The database is tiny and the calls are rare, so the cost is irrelevant.

A case that leaves the list view is dropped from `cases` but its **thread is
kept**. It costs nothing and makes re-opening that case instant.

### 17.5 What the user does
- **Load cases**: one page load, reports `23 cases, 2 new, 1 updated, 20 unchanged`
  and names the changed ones.
- The tree groups by status, each group headed `In Progress (11) - 3 to read`.
  Every row shows `new` / `stale` / `cached`, and stale rows are amber.
- **Clicking a case** shows it instantly when cached, otherwise says
  "Loading <number> from the portal..." and reads it.
- **Refresh changed** reads every stale case in one background pass, updating the
  tree as each finishes. One failing case is logged and skipped rather than
  killing the run.
- The **Case number / Find** box still reaches cases outside the list view.
  The view is filtered, so closed cases are only reachable through search.

After a reply is posted the thread is re-read and stored with an empty
`last_modified`, which guarantees it is re-read at the next list refresh rather
than being trusted against a value that has already moved on.

### 17.6 Escaping traps when embedding JavaScript
Two bugs came from `\n` inside JS passed through Python:

- `body.match(/[0-9,]+\+? items?[^\r\n]*/)`: the `[^\r\n]` collapsed into real
  newlines, and a JS regex cannot span lines. Replaced with `.*`, which already
  excludes newlines in JS.
- `.split(/[\r\n]/)`: same problem. Replaced with
  `.split(String.fromCharCode(10))`.

Rule: **never put `\n`, `\r` or `\+` inside an embedded JS string.** Use
`String.fromCharCode(10)`, or `.` where newlines are already excluded.

---

## 18. Making the thread read like the portal (Aug 2026)

### 18.1 Rich text instead of a plain dump
`sc_thread_view.py` renders a case into the Tk text widget with tags rather than
printing one block of text. `configure_tags()` is called once per widget, and
`render_thread()` writes and tags the ranges.

| Element | Treatment |
|---------|-----------|
| Subject | bold, dark blue, larger |
| Field pairs | grey label + bold dark value on one line |
| `@mentions` | **blue** (`#1a5fb4`) |
| Attachments | green (`#0b6e4f`), listed under their post |
| Case numbers / URLs | purple / blue underlined |
| System entries | grey italic, so the audit trail recedes |
| Comments | indented behind a `│` gutter bar |

**Tk text widgets default to a fixed-width font**, which made the first attempt
look like a terminal. The widget is now set to **Segoe UI 10**, falling back to
whatever it already had if that family is missing.

The mention pattern requires continuation words to be **capitalised**
(`@[A-Za-z][\w'-]*(?:\s+[A-Z][\w'-.]*)*`), otherwise the ordinary sentence after
a mention is swallowed into it. `"@Jane Smith (Customer) , thanks."` must
match only `@Jane Smith (Customer)`.

### 18.2 Screenshots shown inline
Chatter renders an attached picture as an `<img>` pointing at

```
/sfc/servlet.shepherd/version/renditionDownload?rendition=THUMB720BY480&versionId=<id>&...
```

so the URL the browser already displays is the one to download. Non-image
attachments have no such image. They appear as `.cuf-contentAttachmentTitle`
plus `.cuf-contentAttachmentSize`, which is where the file name and size come
from.

`download_post_images()` fetches through **`page.context.request`**, so the
portal session's cookies are reused. A plain HTTP client would be unauthenticated.
Files are named by their Chatter **version id**, which never changes, so
re-reading a case does not download its screenshots again.

Two traps when displaying them:
- **Tk keeps no reference to an embedded image.** Without holding it, the
  picture is garbage collected and the thread shows an empty box. They are
  pinned on `text_widget._embedded_images`.
- Pillow scales each picture into 620x460 (720x1018 became 325x460), so one
  screenshot cannot fill the pane.

Filter out `/img/userprofile/` and `/img/icon/`. Those are avatars and the
60x60 case icon, not attachments.

The build needs `--collect-all PIL --hidden-import PIL.ImageTk`.

### 18.3 Auto-loading the active cases
After a list refresh the app reads, unprompted, every stale case whose status is
in `sc_settings.AUTO_LOAD_STATUSES`, currently **In Progress** and **Waiting for
Customer**. Those are the cases someone is about to open. Completed and closed
ones are left until clicked.

It runs on the shared background reader used by "Refresh changed", which:
- reads one case at a time and updates the tree as each lands,
- logs and **skips** a case that fails rather than abandoning the rest,
- can be stopped with the **Stop** button (a `threading.Event` checked between
  cases, so it stops after the one in flight rather than mid-read),
- refuses to start a second pass while one is running.

The tick box **Auto-load active** turns it off, and the choice is remembered in
`auto_load_enabled`.

---

## 19. Parallel loading, attachments on replies, closing cases (Aug 2026)

### 19.1 A pool of browsers, one login
Reading a case costs ~20-40 s, almost all of it waiting on Salesforce, so a
backlog is spread across several browsers.

**The login is shared, not repeated.** The primary session exports its cookies
with `context.storage_state(path=...)`, and each worker starts a context from
that file. Measured: 12 cookies, 3 KB, and workers never see the login page.

Measured on five cases:

| | |
|---|---|
| Login (once, shared) | 26.6 s |
| One case, on its own | 38.6 s |
| Five cases across five browsers | **49.9 s wall clock** for 209.3 s of work |

Failures: none. Per-case time rises a little under load (35-50 s), which is
contention on the portal rather than on us.

**Selenium was considered and not used.** Playwright is already bundled and its
selector behaviour here is hard-won. Adding Selenium would mean a second browser
stack and a second set of quirks for no gain. Playwright drives several browsers
natively.

Worker threads are needed because Playwright's sync API is **thread-affine**:
each worker owns its own Playwright instance, browser and page, and never
touches another's objects.

Details: workers start **staggered** (`DEFAULT_STAGGER_SECONDS = 5`) so the
portal sees a ramp rather than a burst. The pool is only used for
`MINIMUM_CASES_FOR_POOL` (3) or more, because below that starting browsers costs
more than it saves. Ongoing delta refreshes go back down the single shared
session. `CaseCache` writes are serialised with a lock, since several workers
finish at once.

### 19.2 Attachments: the comment dialog is broken, the publisher is not
Attaching a file to a **comment** cannot currently be automated. Clicking
"Attach file" on a comment composer raises Salesforce's
"Sorry to interrupt / CSS Error" and the Select File dialog never renders.
Confirmed across: headless and **headed**, the scoped composer and the page-wide
`.first` composer, and the exact sequence the Log-a-Case flow uses (navigate
straight to the case, wait for the composer, click, type, attach). Zero file
inputs appear anywhere on the page afterwards.

The feed's **top-level publisher** has its own working control, labelled
**"Attach up to 10 files"**, reached by clicking
`button[title='Share an update...']` to expand the publisher first. It is a
button, not an `<input type=file>`, so the chooser is captured with
`page.expect_file_chooser()` rather than by writing to an input that does not
exist until clicked.

So: **a reply with files is posted as a case update, and a reply without files
stays a comment under the post it answers.** The confirmation dialog says which
is about to happen. Ten files fit on one update, unlike the one-file-per-comment
limit of §9.7.

### 19.3 Closing a case
The case page carries a `CLOSE CASE` brand button (it is not in the highlights
panel). `close_case()` clicks the first visible, enabled one, then accepts a
confirmation step if one appears (`Confirm` / `Yes` / `OK` / `Close Case`), and
the thread is re-read afterwards. It is guarded by a confirmation naming the
case, because it changes the case on the portal.

### 19.4 Ordering and the hourly check
`STATUS_SORT_ORDER` puts **Waiting for Customer** first, then In Progress,
Pending, Completed by SimCorp. Anything unrecognised sorts alphabetically after
those. `AUTO_LOAD_STATUSES` leads with Waiting for Customer for the same reason:
those are the cases waiting on an answer.

`AUTO_REFRESH_MINUTES = 60` re-checks the case list every hour for as long as
the window is open. It only refreshes the **list** (one page load) and the
usual delta rules decide whether any thread is re-read. It skips itself while a
read is running or before anyone has signed in, and always reschedules itself in
a `finally`, so a failed attempt cannot stop the timer.

### 19.5 Layout
Each section of the Respond tab is its own pane of one `ttk.Panedwindow`
(list / thread / reply / progress), so a sash moves only the two sections it
sits between. Previously the lower half was a single pane and dragging moved
everything.

A faint rule (`post_rule`) separates consecutive posts.

### 19.6 Icons
The exe carries `icon.ico`, built with `--icon`. The window icon is set
separately at runtime from `icon.PNG`, because Tk needs a PNG. Both files ship.

**Windows caches shortcut icons by the IconLocation string**, so rebuilding the
exe leaves the old icon on the shortcut. `ie4uinit.exe -show` was not enough.
The fix that works: delete and recreate the `.lnk`, and point `IconLocation` at
`icon.ico` rather than at the exe. That's a different cache key, so Windows loads it
fresh. This is why `icon.ico` stays in the deployment folder rather than being
tidied away as a build-only file.

---

## 20. The "no Case Number column" failure (Aug 2026)

### 20.1 What actually happened
The hourly check reported:

```
Opening the case list...
  0 rows loaded
  ! The case list did not show a 'Case Number' column, so it could not be read.
    The portal layout may have changed.
```

**The layout had not changed.** Re-running the same read minutes later returned
21 rows normally. The message was wrong, and it was wrong in the most expensive
way: it blamed a permanent cause for a transient one, which would send the next
maintainer hunting for a portal redesign that never happened.

The giveaway is the line above it in the same log:

```
logged in, now at: https://supportportal.simcorp.com/secur/frontdoor.jsp?...retURL=%2Fapex%2FCommunitiesLanding
```

The session had settled on **`/secur/frontdoor.jsp`**, Salesforce's session
staging step, not on a real portal page. Navigating to `/s/case-lists` from
there produces a page whose table never renders. `wait_for_url("**/s/**")` had
been treated as proof of a finished login, and it is not.

### 20.2 Three separate defects, three fixes

**1. Login did not confirm where it landed.** `sc_portal.is_authenticated_page()`
now rejects `frontdoor` and `/s/login`, and `log_in_to_portal` re-opens the
portal home (up to three times) until the URL is a real page, saying so in the
log if it never gets there.

**2. An empty list was fatal and misdiagnosed.** `fetch_case_rows` now retries
`LIST_LOAD_ATTEMPTS` (3) times, reloading between attempts, and only then gives
up. `_describe_empty_case_list()` works out the real cause and says which:
signed out mid-read, "Invalid Page", no table rendered at all, or a table with
no rows. The "layout may have changed" wording is now reserved for the one case
that genuinely means it (**rows rendered but no Case Number column**) and it
lists the columns it did find.

**3. A background timer opened a modal dialog.** The hourly check popped an
error box over whatever the user was doing. `_run_in_background` takes a
`quiet` flag: timer-driven work (the hourly refresh, and any auto-load it
triggers) reports failures to the progress log only, while anything started by
a click still gets a dialog. Nothing scheduled may interrupt.

### 20.3 Close Case is confirmed working
The same log shows the close path working end to end on case 00900003:
`Looking for the Close Case button... / confirmed with 'Confirm'. / close
requested.` followed by a successful re-read. The confirmation step does appear
in practice, so handling it was necessary rather than defensive.

---

## 21. Logging a case in the background, and a colour-coded progress log (Aug 2026)

### 21.1 The case logger no longer opens a window
Logging a case now runs **headless by default**, matching the Respond tab.
A tick box, **"Show browser while logging"**, brings the window back for
watching a run. The choice is remembered in `log_case_show_browser`.

Two things follow from hiding it, and both had to change with it:

- **Viewport.** A visible run keeps `no_viewport=True` so the window is its real
  size. A hidden run gets an explicit `1600x1200` viewport instead, because
  headless with no viewport can fall back to a small default and leave elements
  laid out off-screen. Nothing is selected by coordinate either way (§9.4), but
  visibility checks still depend on things being laid out.
- **The end of the run.** The flow used to sit in
  `while not close_browser_event.is_set()` so the case could be reviewed in the
  open browser. Hidden, that would hang forever on a button nobody can act on,
  so the wait now happens **only** when the browser is visible. A hidden run
  closes itself and logs the case URL instead, which is the thing you actually
  wanted from the open window.

`Close Browser` is only enabled for a visible run.

The `SCL_HEADLESS=1` environment toggle still forces headless, so it overrides
the tick box rather than fighting it.

### 21.2 Progress lines carry colour
`classify_progress_line()` tags each line and the two progress panes render it:
green `#0b6e4f` for success, red `#b00020` for failure, amber `#8a5a00` for
something worth noticing, and no colour at all for ordinary narration, so the
colour means something.

**Order matters in `PROGRESS_STYLES`: failures are tested first.** Otherwise
"could not post comment" matches "post" and comes out green. The success words
are the narrow ones ("case logged successfully", "comment posted", "reply
posted"), and the failure list is deliberately broad ("! ", "could not",
"failed", "aborted", "traceback").

The final milestone message was changed from "Case submitted, files attached to
case comments." to **"Case logged successfully."** so there is one unambiguous
green line to look for at the end of a run.

---

## 22. Opening a screenshot full size (Aug 2026)

Double-clicking a picture in the thread opens it in its own window
(`sc_image_viewer.py`), sized 1:1 when it fits the screen and scaled down when
it does not, with the true pixel size named in the footer.

### 22.1 Why it is quick
Nothing is fetched. `download_post_images()` already saved the file beside the
cache when the thread was read (§18.2), so opening is a decode of a local file.
Decoded images are also held in a small LRU (`IMAGE_CACHE_SIZE = 6`), so opening
the same screenshot again is instant.

The window paints **"Loading..." first and decodes on the next event-loop tick**
(`window.after(20, load)`). Decoding on the spot would block the event loop and
the window would only appear once the work was finished, which reads as a freeze
rather than as loading.

### 22.2 Tk cannot bind to an embedded image
There is no `<Double-Button-1>` on an image inside a text widget. The click is
resolved instead:

1. `_write_image()` keeps the name returned by `image_create()` in
   `text_widget._image_files`, mapped to the file on disk. The registry is reset
   at the top of `render_thread()`, alongside the image references.
2. `image_at_click()` turns the pointer position into a text index with
   `index("@x,y")`, asks `dump(..., image=True)` what sits there, and looks the
   name up.

The handler returns `"break"`, which stops the double-click also selecting the
word behind the picture and leaving a stray highlight on a read-only pane.

A grey italic line under each picture says the double-click is available.
Without it the behaviour is invisible.

**Testing note:** `bbox()` returns `None` for a widget that has not been laid
out, which includes anything on a notebook tab that is not the selected one. A
test that clicks an embedded image must select the Respond tab and pump the
event loop first, or it will silently test the wrong coordinates.

### 22.3 Wheel zoom
The viewer is a `tk.Canvas` rather than a label, so it can zoom and pan:
wheel to zoom, drag to pan, **Fit** and **1:1** buttons, and the caption reports
the current percentage.

Three things that make it usable rather than merely working:

- **Zoom is anchored at the pointer.** Before changing scale the image point
  under the cursor is worked out (`canvasx(x) / scale`), and afterwards the view
  is scrolled so that same point sits back under the cursor. Zooming about the
  centre instead drifts away from whatever detail is being examined.
- **Rapid notches coalesce.** The scale updates immediately but the resample is
  deferred by `REDRAW_DELAY_MILLISECONDS` (60 ms), cancelling any pending
  redraw. Twelve notches spun quickly queue in ~0 ms and resample once, instead
  of twelve times on a multi-megapixel image.
- **Resampling filter follows direction.** `LANCZOS` when shrinking, where the
  quality shows, and `BILINEAR` when enlarging, where it does not and costs far
  more.

**The zoom ceiling is computed, not fixed.** `MAXIMUM_ZOOM` alone would let 8x
on a 2560x1440 capture allocate ~236 Mpx (~700 MB as RGB).
`maximum_usable_scale()` caps the scale so the resampled bitmap stays within
`MAXIMUM_RENDERED_PIXELS` (40 Mpx, ~120 MB), which works out at 7.4x for a
720x1018 screenshot but only 2.2x for a 4K one.

---

## 23. Magpie, and the attachment lifecycle (Aug 2026)

### 23.1 The app is called Magpie
`sc_settings.APPLICATION_NAME` / `APPLICATION_TAGLINE` drive the window title,
so there is one place to change it. **The settings file keeps its old name**
(`.sc_case_logger.json`) on purpose: renaming it would silently lose an existing
install's saved credentials and preferences on upgrade.

The executable and shortcuts are still `SimCorp SF`. Renaming those breaks any
pinned taskbar item and anything else pointing at the path, so it is a separate,
deliberate step rather than a side effect.

### 23.2 Screenshots are filed per case and deleted when the case closes
`download_post_images()` writes into
`_cache\attachments\<case number>\<version id>.img` rather than one flat folder,
so a case's pictures can be dropped as a unit without working out which file
belonged to which thread.

The delete trigger is a case **leaving the list view**. The pinned view is
"All Open Cases", so a closed case simply disappears from it.
`store_case_rows()` now reports those in `RefreshOutcome.removed`, and the GUI
calls `purge_case_attachments()` for each. Closing a case from the Close button
purges it immediately as well, rather than waiting for the next refresh.

**The cached thread is deleted along with the images**, and that is the part
that makes reopening work. Leaving the thread behind would leave it pointing at
files that no longer exist, so the case would render with broken pictures.
Dropping it means a reopened case comes back as `is_new` and is read again from
the portal, images and all, which is exactly the behaviour asked for.

`purge_case_attachments()` deletes both the per-case folder **and** any file
named in the cached thread's payload. The second part matters for upgrades: a
cache written by an earlier version has every case's images together in one
flat folder, and those would otherwise never be reclaimed.

The status line now carries `N images (X MB)` so the cost on disk is visible
rather than something to discover later.

### 23.3 Deploying over a running copy
Killing the app and copying immediately fails: Windows releases a process's
loaded DLL handles slightly after it dies, so `_internal\libcrypto-3.dll` was
still locked and `_internal` copied only partly, leaving a **mixed old/new
tree** that still launched. Wait for the lock to actually clear (try opening
the DLL with `[System.IO.File]::Open(..., 'None')` until it succeeds), then copy
with `robocopy /MIR` and verify: file counts should match and the exe hash
should equal the staged one. Note `robocopy` exits **1** on success ("files were
copied"). Only 8 and above are failures.

---

## 24. Session expiry, and the Transition project step (Aug 2026)

### 24.1 The session expired and never came back
Left open overnight, every hourly check failed identically:

```
Opening the case list...
  the list was empty; reloading (attempt 2)...
  ! ... the portal signed us out while loading the case list
    (ended at https://supportportal.simcorp.com/s/login/?ec=302&startURL=%2Fs%2Fcase-lists)
```

Salesforce expired the idle session. The §20 work correctly *detected* the
sign-out, but detection without recovery just means failing accurately: the
session was dead and nothing ever re-authenticated it, so all fifteen overnight
attempts hit the same corpse.

Two fixes, and both are needed:

- **Keep-alive.** `PortalSession` runs a thread that, when the session has been
  idle for `KEEP_ALIVE_INTERVAL_SECONDS` (10 minutes), loads the portal home
  once. It only fires when nothing else has used the session, and the task queue
  serialises it, so it never competes with real work. The app can sit an hour
  between hourly checks, which is what let the session lapse.
- **Recovery.** `_execute()` wraps every task: if it raises *and* the page is no
  longer on a signed-in URL, it logs in again and retries the task once. The
  keep-alive makes expiry unlikely. This makes it survivable.

`/s/login/?ec=302` is the tell. `sc_portal.is_authenticated_page()` already
rejected it (§20.2), so both fixes reuse that one definition.

### 24.2 Transition has a different wizard
Selecting **Transition** for Operations & Onboarding does not add a field to the
Business Impact step. It **replaces that step** with a required
**Project Selection**. The automation assumed a fixed two-step wizard
(details -> business impact -> create) and stalled on a step it did not expect.

`advance_through_dialog()` now walks the wizard instead of counting it: at each
step it asks what is on screen (project radios, Business Impact, or the upload
step), fills that in, presses Next, and stops once the upload step appears.
That handles both flows and any future step without another rewrite.

Three details that cost time:

- **The fields are in shadow DOM.** A first probe using
  `document.querySelectorAll` found only the site search box (§3 all over again).
  The probe had to recurse `shadowRoot`s to see the form at all.
- **The radio group's name has a random suffix**: `selProjectSelection-26e3` on
  one load, `-2d26` on the next, so it is matched by the prefix
  (`input[name^='selProjectSelection']`) for detection, and by the stable
  `value` (`ChoiceListCollectionCIProjects.N`) for selection.
- **The radio cannot be clicked.** It is visually hidden with its `<label>` over
  the top, so Playwright refuses with "label intercepts pointer events" and
  retries forever. `select_transition_project()` reads the radio's `id` and
  clicks `label[for=...]`, as a person would, with `check(force=True)` as a
  fallback. It then confirms the radio actually took rather than trusting the
  click.

The seven projects are baked into `sc_settings.TRANSITION_PROJECTS`, matching
the §6 precedent for portal dropdowns. **Two of them share the label
`Client - Customer Care`**, which is why the choice is stored as a value and the
duplicate is shown as `Client - Customer Care (2)`. Selecting by visible text
alone would be ambiguous.

The GUI shows the Project picker only when Transition is chosen.

### 24.3 Transition confirmed working, and two things it exposed
A real Transition case (00900007) went through end to end, which also settled
the shape of that flow: it is **three steps**, not two:

```
details -> project selection -> business impact -> create
```

so Transition **adds** a step rather than replacing Business Impact as §24.2
first assumed. The walker handles either, which is the point of walking the
steps instead of counting them.

Two faults the run's log made obvious:

- **"Waiting for Business Impact step..."** was still being printed by
  `fill_business_impact_step`. The walker has already identified the step by the
  time it calls that function, so the line announced a decision that had been
  made and implied a wait that no longer happened. Removed. (The removal was
  written once before and lost: the patch that carried it aborted on a failed
  assertion *before* writing the file, so only the other half of that change
  landed. Worth checking a file actually changed when a scripted edit reports a
  problem.)
- **"Please find the attached files." was posted on a case with no files.**
  That is the default text, so every fileless case got a comment promising
  attachments that did not exist. Now the default is skipped when nothing is
  attached, while a comment the user actually typed is still posted.

### 24.4 The required dropdowns were verified, not assumed
Whether a non-default Priority actually reaches the portal was checked rather
than trusted. Three combinations were set and then **read back out of the form**
(piercing shadow DOM to find the `<select>` and reporting its selected option):

| Asked for | Portal held |
|-----------|-------------|
| `2-High` / Operation / `DEV / 26.04` | all three |
| `1-Critical` / Transition / `* PROD / 26.04 - MainProd` | all three |
| `4-Low` / Operation / `GOLD / 26.04` | all three |

All landed. This works because these really are native `<select>` elements
(`lkupDefaultPriority`, `picklistEnvironment`, `pickInstallation`), so
`select_option(label=...)` sets them and fires the change the LWC listens for,
unlike the Transition project control, which is a hidden radio behind a label
and needs the click treatment of §24.2.

Reading a control back after setting it is the cheap way to tell "the code ran"
from "the portal accepted it", and is worth doing for anything that decides how
a case is triaged.

---

## 25. A login page cached as a case, and three UI faults (Aug 2026)

### 25.1 The serious one: reading a case while signed out
The thread pane showed case 00900001 as **"(no subject)", 0 items**, with a URL
of `/s/login/?ec=302`. `read_case()` had been pointed at a case, the portal had
redirected to the login page, and the reader dutifully found no feed items and
returned that as the case. `store_thread()` then cached it **against the case's
current Last Modified**, so it looked perfectly up to date and nothing would
ever re-read it. Silent corruption, invisible until someone opened the case.

Two guards, because either alone leaves a hole:

- `read_case()` now checks the landing URL is a signed-in page **and** contains
  `/s/case/`, and raises otherwise. That turns a silent bad read into a normal
  failure, which the session layer already knows how to recover from by signing
  in again and retrying (§24.1).
- `store_thread()` refuses a thread with **no posts and no subject**, which is what a
  login page looks like after parsing. A guard at the boundary catches any other
  route to the same bad state.

`purge_unusable_threads()` runs at startup and sweeps what earlier versions
already cached: threads that are empty, unparseable, or orphaned (belonging to
cases no longer listed). On the live cache it removed four: **two of them
login pages cached as real cases** (00900001, 00900002), plus two orphans left
by closed cases. Swept cases simply show as needing a read again.

### 25.2 Rebuilding the tree re-rendered the case
Every finished read calls `_show_cached_case_list()`, which rebuilds the tree
and restores the selection, and `selection_set()` makes Tk fire
`<<TreeviewSelect>>` exactly as though the row had been clicked. During a
five-case auto-load the log filled with ten copies of
"Case 00900005 shown from cache", each one re-rendering the thread and throwing
away the reader's scroll position.

**A flag held across `selection_set()` does not fix this**: Tk delivers the
virtual event asynchronously, so the flag is already cleared by the time the
handler runs. That was tried and measured still firing 5 times out of 5. The fix
that works needs no timing at all: the handler returns early when the selected
case is already the one on screen.

### 25.3 Buttons could be dragged out of sight
Reply lived inside the paned window, so dragging a sash down clipped **Post
reply** and **Close case** off the bottom. Reply now sits outside the splitter,
packed to the bottom of the tab before the splitter is packed, so it always
keeps its space and the splitter takes what is left. Verified by driving every
sash to both extremes and checking the buttons stay mapped and on screen.

### 25.4 A read in progress now says so
The tree showed a case as "new" or "stale" while it was actually being read,
which reads as "nothing is happening" during a 20-second-per-case auto-load.
Rows being read now show **reading...** in blue.

---

## 26. Closing a case gave no feedback, and a pop-out log (Aug 2026)

### 26.1 Closing left the row lying
Closing case 00900004 worked (the thread header read **Status: Closed**) but the
tree still showed it under **In Progress**, and its Cache column flipped to
**new**. From the outside that looks like nothing happened at all.

Two separate faults:

- **The case list was never refreshed.** A row's status comes from the last
  `/s/case-lists` read, so closing a case could not change it. The row kept the
  status it was listed with.
- **The thread was purged immediately.** `_close_case` called
  `purge_case_attachments()` on success, which deletes the cached thread. The
  row therefore became "new" **while the user was still looking at that very
  case**, the worst possible moment to throw its cache away.

Now closing re-reads the case, reports the status the portal actually returned,
and **refreshes the case list**. The pinned view is "All Open Cases", so the
closed case leaves the list, and the existing removal path (§23.2) purges its
images then, once, at the right time, rather than eagerly.

Worth stating plainly, because it surprises people: **there is no "closed"
group to move to.** The portal view Magpie reads is All Open Cases, so a closed
case disappears from the tree rather than moving down it. Showing closed cases
would mean driving the list-view picker, which is the hidden `<select>` that
could not be driven (§17.1).

### 26.2 The pop-out log
The inline Progress pane is a few lines tall and scrolls away, so by the time
someone asks "what happened?" the answer is gone. A **Logs** button on the
Respond toolbar opens a window holding the last
`RESPOND_LOG_HISTORY_LINES` (5,000) Respond-tab lines, each timestamped,
carrying the same green/red/amber colouring as the inline pane.

- It is a `collections.deque(maxlen=...)`, a ring buffer, so an app left open
  for weeks cannot grow without bound.
- History is captured whether or not the window is open, so opening it after
  something goes wrong still shows what happened.
- **Clear logs** empties both the buffer and the window. **Copy all** puts the
  lot on the clipboard, which is what actually gets pasted into a bug report.
  **Follow new lines** can be turned off to read back without being dragged to
  the bottom.
- Only Respond-tab lines are kept. Log-a-Case has its own pane and mixing the
  two would bury the thing being looked for.
