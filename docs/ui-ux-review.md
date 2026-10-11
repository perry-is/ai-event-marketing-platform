# Event Marketing UI/UX review

This review covers two frontend-only passes. The first pass (commit `b8cb125`) restructured Campaign review. The second pass extends the same approach to the whole journey: New Event, extraction and clarification, the Event summary, approval wording, and destination status. No API contract, database schema, auth, readiness, versioning, approval, or execution rule changed. `src/api.generated.ts` regenerates without differences.

## 1. Existing UI strengths

- The shell is restrained and private. It shows no invented metrics, and navigation is limited to New Event, Events, Groups, and Settings.
- New Event already opens with "Tell me about the event." and one free-text field, not a long form.
- Event facts and marketing copy are separate data. Campaign review shows the facts snapshot beside the copy.
- Campaign versions are visibly immutable. Approval binds to the version under review, and execution is a separate action.
- Facebook Groups is clearly manual. UNKNOWN outcomes offer verification and never a blind retry.
- The app already had labelled fields, a skip link, visible focus, and desktop plus iPhone Playwright coverage. All of this was kept.

## 2. Existing UX problems

- **No sense of journey.** No screen said where the Event was in the details → campaign → approval → distribution sequence.
- **Questions came last.** On Event detail, "Here's what I found" and the clarification questions sat below the summary card, so the operator had to scroll to find the actual task.
- **Technical labels.** Proposal labels were derived from field paths, such as "Price kind" and "Venue". Values such as "in person" and "free" appeared in raw lowercase.
- **Detached upload.** The New Event upload was a bare native file input with no drop target and no way to remove a chosen file. "Add another source" floated on its own below the source list.
- **Revisions were hard to spot.** After a revision, nothing showed which sections had changed.
- **Machine-flavoured approval text.** Approval and status copy used phrases like "Phase 1 actions are simulated", "Simulated auto execution", "Manual action package", and "0 failures · 0 uncertain". The approval heading stayed "Approve the version you reviewed." even after distribution had finished.
- **Weak status cues.** "Failed" was used for both retryable and final failures. Status pills relied mostly on color.
- **Inconsistent palette.** Much of the stylesheet still used an older green-grey palette, so the brand navy and blue looked inconsistent.

## 3. Design principles adopted

1. **Attention goes to "Is this right?"** Whatever needs the operator's input comes first on each screen. Everything else is reference.
2. **Always show where you are.** A four-step progress indicator appears on Event detail and Campaign review.
3. **Human language over system language.** No field paths, hashes, enums, or "execution". Practice mode is described as "nothing is sent to real platforms yet".
4. **Facts versus marketing is a visible boundary.** The Event summary is labelled "Event facts · source of truth" and explains where date, place, and price changes belong.
5. **Text and shape, not color alone.** Every status has a glyph and words as well as color.
6. **Truthful loading copy.** Examples: "Reading event details…", "Creating your campaign…", "Updating the campaign…", "Sending to destinations…". No fake progress.
7. **Modest system.** We reused the existing CSS module and tokens. There is no new framework or dependency.

## 4. Component and layout changes

- **New `src/ui.tsx`:**
  - `ProgressSteps`: an ordered list labelled "Event progress", with `aria-current="step"` and done/current text for screen readers.
  - `StatusLabel`: status text plus a decorative glyph and a tone.
- **New Event (`events.tsx`):**
  - The file input sits inside an integrated drop area that supports drag-and-drop or choosing a file.
  - A chosen file shows as a chip with a labelled Remove button.
  - A "what happens next" line sits under the primary button.
- **Event detail (`events.tsx`):**
  - "Here's what I found" and "Just a couple things I need" now come before the summary card.
  - The questions section has a highlighted surface and a count ("Needs your answer · 1 question").
  - Proposal labels use plain names: Event, Date and time, Location, Host, Price, Format.
  - Uncertain proposals get a warm surface and a "?" glyph with "Please double-check this detail." or the extraction's own reason.
  - Delivery and price values are sentence-cased.
  - The not-ready hint now points to the items above.
  - Source material and "Add another source" are merged into one card, which has an empty state and visible keyboard focus on the button-styled label.
- **Campaign review (`campaigns.tsx`):**
  - Progress indicator and a reworded lead ("Is this campaign right?").
  - A success notice after a new version is created: "Version 2 is ready — 'Make LinkedIn warmer.'"
  - "Updated in Version N" tags on changed copy sections.
  - A read-only banner at the top when viewing an older version. It holds the "Return to current version" action.
- **Approval and status:**
  - The approval heading follows context: "Ready to send Version N out?" → "Version N is approved." → "Here's what happened."
  - Plain-language plan rows and a natural-language outcome summary that leaves out zero counts.
- **Shell:** "Connect" in the text wordmark takes the orange brand accent. This is styled text only; no logo was recreated.
- **Styles:** Leftover green-grey values were mapped onto the navy/blue tokens. Success states stay green and warnings stay amber.

## 5. Responsive changes

- Progress steps collapse to stacked marker-over-label columns under 520px, so all four steps fit on an iPhone without horizontal scrolling.
- The read-only banner and source-card actions become full-width buttons on phones.
- Destination cards form two columns at 900px and wider. The manual Facebook Groups card spans the full width because it holds the longer copy and actions. Phones keep one column.
- The drop area and file chip wrap long filenames instead of widening the page.
- Every Playwright scenario still asserts there is no horizontal page overflow at desktop and iPhone sizes.

## 6. Campaign review approach

The structure from the first pass was kept:

- sticky section links
- Event facts
- Event listing copy: headline, summary, description, Eventbrite, Meetup
- social copy: LinkedIn organization, LinkedIn personal, Instagram, Facebook Groups
- creative direction with a reserved artwork slot
- revision
- approval and distribution
- version history

All copy stays expanded, with no tabs or accordions, so the exact approved words are never hidden. Cards pair up on desktop and stack on phones.

This pass adds orientation (progress steps) and change awareness. When a version has `revision_targets`, each targeted copy block gets a blue inset edge and an "Updated in Version N" tag. Untouched blocks look normal. The cue is driven only by data the API already returns, and no text diff is shown.

## 7. Version-history approach

- Each entry shows its version number, a human description, and its creation time. The description is the revision request, "Initial campaign", or the regeneration note.
- **Current** appears in the title ("Version 3 · Current").
- **Approved** is a separate pill: "Approved for distribution" or "Previously approved".
- **Viewing now** marks the version on screen.

These three facts are never merged.

Older versions open read-only, with a banner at the top of the page explaining that they can't be changed or approved. The revision form is hidden while viewing them. The helper text says approval belongs to one exact version and a newer version needs its own approval.

## 8. Approval UX

- The heading asks "Ready to send Version N out?" and names the exact version.
- The plan states "Version N will go to 5 destinations". Each destination is labelled "Sent automatically · practice mode" or "You post it · copy prepared for you".
- Content-only destinations, such as LinkedIn personal while access is unverified, are explained once in plain language. The duplicate exclusion line was removed.
- Replacement and uncertainty acknowledgements are unchanged in behaviour.
- After approval, the notice reads "Nothing is sent until you start distribution."

## 9. Execution-status UX

- Each destination card shows a status pill with a glyph and text:
  - ✓ Published (Simulated) / Marked complete by the operator
  - ! Needs retry / Didn't complete / Didn't complete · accounted for
  - ? Result uncertain — verify before retrying
  - → Manual action required
  - … In progress
- Failure cards add: "{Destination} didn't finish. The other destinations were not affected."
- UNKNOWN cards add: "We couldn't verify whether {Destination} completed. Verify it before trying again so nothing is posted twice."
- On UNKNOWN cards, Verify is the primary button, "Acknowledge uncertainty" is secondary, and no Retry is offered.
- The overall outcome banner keeps its existing labels and uses a readable count line, e.g. "4 published · 1 waiting on you".

## 10. How future graphics fit

- The Creative section already pairs an artwork preview slot with the visual brief. Generated graphics should render in that slot for the version being viewed, so a historical version shows its own graphic.
- Regenerating or revising a graphic should create a successor version, as copy revisions do. The "Updated in Version N" tag then applies to the artwork block with no new UI concept. `revision_targets` would need an artwork target, such as `creative_asset`.
- Variations can appear as a small selectable strip under the preview. Choosing one is a revision, and approval covers the chosen image.
- On mobile the slot already stacks above the brief at full width.

## 11. How future Facebook Group recommendations fit

The manual Facebook Groups card is now organized under explicit subheadings:

- **Where to share:** currently the backend's "No group was selected…" text.
- **Approved Facebook Groups copy:** read-only text with a Copy button.
- **Mark Facebook Groups complete.**

Recommended groups belong under "Where to share" as a short checklist, with a reason for each group. The operator picks or confirms groups there. Nothing auto-posts.

The Groups navigation item stays as the home for managing the group library.

## 12. Accessibility considerations

- The progress list has an accessible name, `aria-current="step"`, and visually hidden "(done)"/"(current step)" text. Glyphs are `aria-hidden`.
- Status is always conveyed in words. Color and glyphs are reinforcement only.
- Hidden file inputs remain real, focusable, labelled inputs. Focus shows on the drop area (`:focus-within`) and on the button-styled label.
- The Remove file button has an explicit accessible name ("Remove flyer.pdf").
- Notices use `role="status"` and errors use `role="alert"`.
- Touch targets stay at or above 40–48px. The existing skip link, landmarks, headings, and labels are unchanged.
- New text colors pass WCAG AA contrast on their backgrounds. Muted text uses `--emp-muted` (#596980) on white.

## 13. Intentionally unchanged

- All backend code, API shapes, generated API types, database migrations, authentication, CSRF handling, and session behaviour.
- Event readiness logic, proposal acceptance, clarification answering, and correction fields.
- CampaignVersion creation, immutability, revision routing, and stale-Event detection.
- Approval preconditions, acknowledgements, execution start, retry, reconciliation, manual completion, and their eligibility rules.
- Version history order (oldest first), all copy kept expanded, and the existing section navigation.
- The navigation set (New Event, Events, Groups, Settings) and the Groups and Settings placeholders.
- No image generation, group recommendations, real connectors, or logo files were added.
- Button names that tests and muscle memory rely on stay as they were: "Create Event", "Use these details", "Save answer", "Create campaign draft", "Create revised version", "Approve this version", "Start simulated distribution", and "Verify simulated result".
- Test selectors changed in exactly two places:
  - "Approve the version you reviewed." became "Ready to send Version 1 out?"
  - "Failed" became "Needs retry" for a retryable failure.

## 14. Backend recommendations (not implemented)

- **Approval wording:** "Ready to send this out?" sounds natural only before approval. A `distribution_stage` field (review / approved / sending / done) on the distribution response would let the UI avoid reconstructing that from several fields.
- **Readable revision summaries:** Version entries fall back to the raw revision request. A short server-produced summary, such as "Made LinkedIn warmer", would read better in history. The regeneration path's `revision_targets` value `"all selected content surfaces"` should become a structured flag.
- **Conflicting values:** Clarification candidates carry a `source_id` but no source label. Returning a human source name would let the UI say "I found two different dates: one in the flyer, one in your notes."
- **Graphics:** Each immutable CampaignVersion will need associated asset metadata, an authenticated preview URL, and an artwork revision target.
- **Group recommendations:** These should return explainable suggestions, and the operator's final selection should be stored in the reviewed distribution intent.
- **Field labels:** The proposal label map lives in the frontend. If new fact paths are added, a server-provided `display_label` would avoid drift.

## Campaign graphics UX (focused pass after WP1G)

This pass was frontend-only and started from `a8f3a21` (`feat: add campaign graphics workflow`). Graphics domain rules are untouched:

- version-bound assets and successor creation
- snapshot hashing
- exact-version approval and execution
- authenticated previews
- deterministic Event text

The WP1G UI already had solid behaviour: a version-bound preview, generate/revise actions, safe failure copy, and the approved graphic in the Facebook package. This pass keeps all of that and fixes placement, context, and wording.

### 1. Graphic placement

- The new `CampaignGraphicSection` (`src/campaignGraphic.tsx`) now sits directly after the Event facts and before listing and social copy. The scan order becomes: version → facts → graphic → listing copy → social copy → wording changes → approval → history. Before, it sat below all six copy cards.
- On desktop the section is a two-column figure: a contained 340px square preview on the left; graphic controls and the visual brief on the right. The graphic is prominent without becoming a hero that pushes copy review down.
- The section eyebrow reads "Part of Version N", so picture and wording read as one CampaignVersion.
- The section link was renamed from "Creative" to "Graphic" and moved to second position.

### 2. Empty state

- The preview area reads "This campaign doesn't have a graphic yet." with a short note on what will happen ("I'll create a background image and place the exact Event name, date, and place on top").
- The only action is **Generate graphic**. Its supporting text explains versioning in plain words: "Adding a graphic saves a new version of this campaign, so Version N stays unchanged."
- On desktop the empty state keeps the square footprint of a real graphic, so generating causes no layout jump.

### 3. Generate flow

- On click, every campaign action is disabled through the shared busy state, so repeated clicks are impossible. The button reads "Creating your graphic…".
- A polite live region says "Creating your graphic… The campaign stays as it is until it's ready." There are no percentages or fake stages.
- On success, the new version loads, the preview shows the new image, and both the page notice and the figure caption say "Graphic added in Version N."

### 4. Revision flow

- The graphic revision box lives inside the graphic section, in a bordered panel with the kicker **Change the picture** and the label "What would you like to change about the graphic?". Examples in the placeholder include "Use less orange, or give the title more breathing room." Helper text says the box changes the picture only and that Event text always comes from Event details.
- The copy revision card is now headed **Change the wording**. Its helper text points picture changes to the graphic section and fact changes to Event details. The two boxes never sit side by side and are labelled differently.
- Pending state: "Updating the graphic…". The typed request is kept on failure, so the operator can adjust it and try again.
- **Event-fact requests** (`EVENT_CORRECTION_REQUIRED` from the graphics endpoint) show inline under the graphic controls: "That changes the event details, not just the graphic. Update the Event first, then create a fresh campaign version and graphic." The message includes an **Update Event details** link.
- **Generation and output failures** show inline: "I couldn't finish the graphic. Your current campaign version and its approval are unchanged, so you can try again." The Generate/Revise button stays available. No provider, HTTP, or model details are shown.

### 5. Version-change cues

- This reuses the existing blue `changedTag` from copy revisions; no new visual language was added.
- The figure caption shows "Graphic added in Version N." or "Graphic updated in Version N.". A version that only carried the graphic forward shows "Graphic for Version N".
- If the previous current version was approved, the page notice adds: "Version N−1 stays approved; this new version needs its own approval."
- Version history describes graphic versions as **Graphic added** or **Graphic change: "…request…"**, using the existing `revision_request`/`revision_targets` data.
- The current version shows a **Needs approval** pill until it is approved. Older approved versions keep "Previously approved". Current, approved, and needs-approval remain separate labels.

### 6. Historical graphic behaviour

- Viewing an older version shows that version's own graphic, from its own preview URL.
- The caption carries a neutral "Saved with Version N · read-only" status. "Previously approved" appears when relevant.
- Controls are replaced with "This is the graphic saved with Version N. Return to the current version to change the graphic." The page-level read-only banner is unchanged.

### 7. Approval treatment

- The approval summary now includes a compact **Graphic included** row: a 64px thumbnail ("Graphic included in Version N") with "approving Version N approves this exact graphic with the copy." The image comes from the approval preview's destination plan, so it is the exact graphic being authorized.
- Versions without a graphic say "No graphic in Version N. Destinations receive text only."
- After approval, the graphic caption reads "✓ Included in approved Version N" with a text-and-glyph status. No other approved badges were added.

### 8. Distribution and manual-package treatment

- Above the destination cards, one **Graphic included** row with a thumbnail states that the approved graphic from Version N goes with each destination. The data comes from the active approval's plan. Destination cards were not turned into galleries.
- The Facebook Groups manual package is now ordered: **Where to share** (the placeholder for future recommendations) → **Graphic** (approved preview plus **Download graphic**) → **Approved Facebook Groups copy** with **Copy approved post** → instructions → **Mark Facebook Groups complete**. On desktop the graphic and copy sit side by side under "Where to share", which keeps a full-width slot for the future recommendation list.

### 9. Mobile behaviour

- Under 760px the graphic section stacks: preview at full card width (the composed Event text stays readable) → caption → change-the-picture panel → visual brief.
- Primary graphic buttons and the Download button go full width under 520px.
- The empty state uses a shorter minimum height rather than a full square.
- The manual package stacks graphic above copy.
- Section headings get `scroll-margin-top`, so jumping from the sticky section bar doesn't hide the heading.
- Playwright still asserts no horizontal overflow on the iPhone project.

### 10. Accessibility

- The preview has alt text "Campaign graphic for {Event}" and sits in a `figure` labelled "Campaign graphic in Version N". Intrinsic width and height are set to prevent layout shift.
- "View full size" adds visually hidden text naming the version and that it opens a new tab.
- Generation and revision progress use a persistent live region. Failures use `role="alert"` in the graphic section.
- Approved and read-only states pair a glyph with text. Needs approval, Previously approved, and Current are spelled out in the history buttons' accessible names.
- Both revision text areas have distinct labels and descriptions and are fully keyboard operable. Focus styling is unchanged.

### 11. Intentionally unchanged

- All backend, API, generated types, and graphics domain behaviour.
- The image alt names used by existing tests.
- The "Generate graphic" and "Revise graphic" button names.
- The page-level read-only banner, approval and execution actions and their eligibility, destination card design, navigation, and Event intake.
- No Facebook Group recommendations, live connectors, or image variations were added.
- **Noted, not changed:** the sticky campaign section bar can cover the top of a card while scrolling on small screens. Anchor jumps are now offset, but the bar's general behaviour was left as is.

### 12. Backend/domain recommendations (not implemented)

- **Structured change kind.** History tells "Graphic added" apart from a graphic revision by matching the server's `revision_request` text "Generated campaign graphic." Exposing a structured value (e.g. `change_kind: GRAPHIC_ADDED | GRAPHIC_REVISED | LAYOUT_ADJUSTED | COPY_REVISED`) would remove that string dependency and let the UI tell layout-only tweaks apart from visual edits.
- **Graphic on version-history rows.** The list doesn't say which versions carry a graphic. A `has_primary_graphic` flag (or a thumbnail URL) would allow a small thumbnail per history row without fetching every version.
- **Download filename.** The preview endpoint could send `Content-Disposition` with an Event-based filename, so downloads and "View full size" tabs are named consistently in every browser.
- **Approval graphic on the distribution summary.** The active approval's graphic is found by scanning plan targets. A top-level `approved_graphic` on the distribution/approval response would be clearer.
- **Supported options for layout requests.** `GRAPHIC_LAYOUT_REVISION_NOT_SUPPORTED` could return the supported options (title larger, details higher), so the UI can offer them as quick choices instead of only describing them.

## Internal branding pass (frontend only)

This pass makes the app read as one Event Marketing product. It is visual only: no API, schema, auth, route, workflow, approval, execution, connector or campaign-output behavior changed. Generated campaign copy and graphics are untouched, and no branding is added to outbound assets.

**Logo.** No approved logo master exists ([brand asset inventory](brand-asset-inventory.md)). The supplied JPEGs are flattened reference art and were not traced, cropped or approximated. The app uses a typographic wordmark: "Business" plus "Connect" in the orange accent, as before. `BrandLockup` in `src/ui.tsx` has a reserved slot, `APPROVED_LOGO_SRC` (currently `null`). Once an approved master is placed under `frontend/public/brand/`, set that path and the mark appears beside the wordmark everywhere.

**Palette.** The palette follows the recurring cues in the references: deep navy foundation, electric-blue actions, an orange accent. These are interface tokens in `src/global.css`, not an approved brand specification:

- brand colors: `--emp-navy`, `--emp-navy-deep`, `--emp-blue`, `--emp-orange`, `--emp-orange-bright`
- neutral surfaces
- state colors
- radius and elevation tokens

Status colors still pair with text and glyphs.

**What changed visually:**

- **Sign-in:** a two-panel layout on wide screens, with a navy brand panel (wordmark, three plain-language capabilities, decorative node texture) beside the sign-in card. On phones it collapses to a compact brand header above the card. The "unavailable" screen keeps the wordmark inside its card.
- **Shell:**
  - The navy sidebar now has a brand lockup and "Private workspace" caption.
  - Navigation items get decorative glyph tiles; the active item has an orange indicator and a blue tile.
  - The sidebar has a short footer line and stays in place (sticky) on desktop.
  - The top bar is white with a user initial chip.
  - A small branded footer was added.
  - The single `navigation` landmark and link names are unchanged.
- **Page headers:** eyebrows are blue with a short orange rule, and page titles use deeper navy at a heavier weight.
- **Surfaces:** one card language across Events, Event detail, Campaign review, history, revision and Groups: one radius, border and elevation. The Approval & distribution card gets a navy-to-orange top rule so the exact-version decision reads as the deliberate step.
- **Controls:** primary buttons are solid blue with depth and hover/pressed states. Secondary buttons have tinted hover. Text inputs show a visible blue focus ring, and the orange keyboard focus outline is kept.
- **Notices:** alerts and notices share one shape and keep their state colors.
- **Settings:** each destination is a provider card with a navy-to-blue top rule. Cards sit in a two-column grid on wide screens. Each card has a clear status pill and a highlighted list of setup reasons, with the connect/disconnect actions separated below a divider.

**Responsive check.** The capture walked through sign-in, New Event, proposals and clarification, the ready Event, Campaign review and approval, distribution, Events, Settings and Groups, at desktop size and iPhone size. Every page passed the no-horizontal-overflow check. Sticky elements (section links, Group save bar) can appear mid-page in full-page screenshots; that is a capture artifact, not a layout fault.