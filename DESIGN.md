---
name: Tender Processing
description: An internal screening desk where a bid team turns a tender feed into a shortlist, one verdict per row.
colors:
  accent: "oklch(0.55 0.14 255)"
  accent-strong: "oklch(0.48 0.14 255)"
  accent-soft: "oklch(0.95 0.03 255)"
  accent-wash: "oklch(0.975 0.015 255)"
  eligible-bg: "oklch(0.95 0.04 150)"
  eligible-fg: "oklch(0.4 0.11 150)"
  eligible-dot: "oklch(0.58 0.13 150)"
  review-bg: "oklch(0.96 0.05 80)"
  review-fg: "oklch(0.45 0.1 65)"
  review-dot: "oklch(0.68 0.14 70)"
  not-bg: "oklch(0.95 0.03 25)"
  not-fg: "oklch(0.45 0.15 25)"
  not-dot: "oklch(0.58 0.16 25)"
  failed-bg: "#EDEDEA"
  failed-fg: "#3A3C42"
  failed-dot: "#5E6066"
  none-bg: "#F1F1EE"
  none-fg: "#5E6066"
  none-dot: "#B0B2B6"
  processing-fg: "oklch(0.45 0.14 255)"
  processing-dot: "oklch(0.6 0.14 255)"
  bg: "#F5F5F2"
  surface: "#FFFFFF"
  surface-sunk: "#FAFAF8"
  hover: "#FBFBF9"
  ink: "#1A1B1E"
  ink-2: "#3A3C42"
  ink-3: "#4A4C52"
  muted: "#5E6066"
  meta: "#6A6C72"
  faint: "#8A8C92"
  line: "#E4E4DF"
  line-2: "#EDEDE9"
  line-3: "#F0F0EC"
  control-line: "#DCDCD6"
  control-line-hover: "#C9C9C3"
  check-line: "#C4C4BE"
  track: "#EAEAE6"
  disabled-bg: "#E6E6E1"
  chrome: "#16171A"
  chrome-ink: "#F3F3F0"
  chrome-muted: "#9A9BA0"
  chrome-link: "#B5B6BA"
typography:
  headline:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "26px"
    fontWeight: 600
    letterSpacing: "-0.02em"
  title-lg:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "18px"
    fontWeight: 600
    lineHeight: 1.35
    letterSpacing: "-0.01em"
  title:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "15px"
    fontWeight: 600
  body:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.4
  body-sm:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    letterSpacing: "0.02em"
  section-label:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    letterSpacing: "0.04em"
  pill:
    fontFamily: "IBM Plex Sans, system-ui, -apple-system, Segoe UI, sans-serif"
    fontSize: "12px"
    fontWeight: 600
    lineHeight: 1.2
  data:
    fontFamily: "IBM Plex Mono, ui-monospace, Cascadia Mono, Consolas, monospace"
    fontSize: "14px"
    fontWeight: 500
    fontFeature: "tnum"
  data-sm:
    fontFamily: "IBM Plex Mono, ui-monospace, Cascadia Mono, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  xs: "4px"
  check: "5px"
  sm: "7px"
  control: "8px"
  track: "10px"
  card: "12px"
  panel: "14px"
  pill: "999px"
spacing:
  "4": "4px"
  "8": "8px"
  "12": "12px"
  "16": "16px"
  "20": "20px"
  "28": "28px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.surface}"
    rounded: "{rounded.control}"
    padding: "0 16px"
    height: "38px"
  button-primary-hover:
    backgroundColor: "{colors.accent-strong}"
  button-primary-disabled:
    backgroundColor: "{colors.disabled-bg}"
    textColor: "{colors.faint}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 14px"
    height: "38px"
  button-secondary-hover:
    backgroundColor: "{colors.hover}"
  button-sm:
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "32px"
  button-danger:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.not-fg}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "32px"
  button-danger-hover:
    backgroundColor: "{colors.not-bg}"
  icon-button:
    textColor: "{colors.muted}"
    rounded: "{rounded.sm}"
    size: "32px"
  input-search:
    backgroundColor: "{colors.surface-sunk}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "38px"
  input-search-focus:
    backgroundColor: "{colors.surface}"
  select:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 34px 0 12px"
    height: "38px"
  select-list:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.track}"
  select-list-search:
    backgroundColor: "{colors.surface-sunk}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "34px"
  select-option:
    textColor: "{colors.ink}"
    padding: "8px 12px"
  select-option-active:
    backgroundColor: "{colors.accent-wash}"
  select-option-selected:
    textColor: "{colors.accent-strong}"
  profile-field:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0 12px"
    height: "38px"
  profile-cell:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    padding: "0 10px"
    height: "38px"
  profile-cell-hover:
    backgroundColor: "{colors.hover}"
  profile-cell-key:
    textColor: "{colors.ink-2}"
  profile-table:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.track}"
  tab-track:
    backgroundColor: "{colors.track}"
    rounded: "{rounded.track}"
    padding: "4px"
  tab:
    textColor: "{colors.ink-3}"
    rounded: "{rounded.sm}"
    padding: "7px 12px"
  tab-active:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
  pill-eligible:
    backgroundColor: "{colors.eligible-bg}"
    textColor: "{colors.eligible-fg}"
    typography: "{typography.pill}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  pill-review:
    backgroundColor: "{colors.review-bg}"
    textColor: "{colors.review-fg}"
    typography: "{typography.pill}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  pill-not:
    backgroundColor: "{colors.not-bg}"
    textColor: "{colors.not-fg}"
    typography: "{typography.pill}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  pill-failed:
    backgroundColor: "{colors.failed-bg}"
    textColor: "{colors.failed-fg}"
    typography: "{typography.pill}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  pill-none:
    backgroundColor: "{colors.none-bg}"
    textColor: "{colors.none-fg}"
    typography: "{typography.pill}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  pill-processing:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.processing-fg}"
    typography: "{typography.pill}"
    rounded: "{rounded.pill}"
    padding: "4px 10px"
  panel:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.panel}"
  appbar:
    backgroundColor: "{colors.chrome}"
    textColor: "{colors.chrome-ink}"
    padding: "0 28px"
    height: "56px"
  selection-bar:
    backgroundColor: "{colors.chrome}"
    textColor: "{colors.chrome-ink}"
    rounded: "{rounded.card}"
    padding: "10px 10px 10px 18px"
  appbar-button:
    textColor: "{colors.chrome-link}"
    rounded: "{rounded.sm}"
    padding: "0 10px"
    height: "28px"
  appbar-button-hover:
    textColor: "{colors.surface}"
---

# Design System: Tender Processing

## Overview

**Creative North Star: "The Screening Desk"**

A quiet, warm-grey working surface where every tender is one row and every row ends in a verdict. The page is built for a bid team that reads a list, decides, and moves on: the type is IBM Plex at office-document sizes (12 to 15px for nearly everything, 26px for the one page title), the surfaces are white panels on a faintly warm paper ground, and the only saturated colour on a resting screen is the verdict pills plus one blue for whatever the user can act on.

Density is deliberate. Rows carry a two-line title, a mono meta line (TCNO, reference, published date), the organisation, the value in crore, the closing date with a days-left pill, and the verdict with its one-line reason, all at a comfortable 16px vertical rhythm. A near-black app bar holds the product name, the company every verdict is checked against and a quiet control to edit that company's profile; the same near-black returns only for transient chrome that floats over the page (the selection bar, toasts), so "dark" always means "about you, not about the data".

Depth is almost absent. Panels are separated by a 1px warm line and a 14px radius, not by shadow; shadow is reserved for things that hover over the page. Motion is short and purposeful: 0.12 to 0.18s colour transitions on one shared ease-out curve, a pulsing dot while a tender is processing, and a single reveal when a verdict lands in front of the user.

**Key Characteristics:**
- Warm-grey paper ground (#F5F5F2) with white bordered panels; no decorative imagery.
- IBM Plex Sans for reading, IBM Plex Mono for identifiers and money, tabular figures everywhere numbers line up.
- One blue accent for action and selection; five verdict tones that each own a bg / fg / dot triple.
- Near-black chrome only for the app bar and floating transient bars.
- Flat at rest; shadow only on things floating above the page.
- Every narrow layout keeps the verdict on screen.

## Colors

A restrained warm-neutral palette with one cool blue for action and a fixed set of verdict tones that carry nearly all the colour on the page.

### Primary
- **Ledger Blue** (`accent`): the single action colour. Primary buttons, the switch when on, checked checkboxes, focus outlines, links, the run progress bar, the app mark. Darkened from the design's oklch(0.6 0.14 255) so white text on it reaches 4.9:1 (WCAG AA); the original lightness survives only where no text sits on it (`processing-dot`, the text-selection tint, the search focus glow).
- **Ledger Blue Deep** (`accent-strong`): hover state of primary buttons and link-buttons; the chosen option in a dropdown list.
- **Blue Mist** (`accent-soft`): processing pill background, the wash a row starts from when its verdict lands, and the matched text in a dropdown search.
- **Blue Breath** (`accent-wash`): background of a selected row, and of the highlighted option in a dropdown list.

### Secondary (verdict tones)
Each verdict owns a triple: a pale background, an AA-legible foreground, and a mid-tone dot.
- **Clear Green** (`eligible-*`): Eligible; also the Shortlisted decision line and a "Met" criterion.
- **Caution Ochre** (`review-*`): Needs review (API value "Requires Review"); also "Cannot determine" and closing within 20 days.
- **Brick Red** (`not-*`): Not eligible (API value "Not Eligible"); also "Not met", errors, closing within 7 days, and the one destructive action (Delete in Closed tenders).
- **Slate** (`failed-*`): a run that failed for this tender; neutral, because a failure says nothing about eligibility.
- **Unread Grey** (`none-*`): not processed yet; also the default days-left pill.
- **Processing Blue** (`processing-fg`, `processing-dot` on `accent-soft`): a tender being checked right now.

### Neutral
- **Warm Paper** (`bg`): page ground.
- **Sheet White** (`surface`): panels, rows, secondary buttons, the selected tab.
- **Sunk Paper** (`surface-sunk`): table header, panel footer, modal footer, search field at rest.
- **Row Hover** (`hover`): row and secondary-button hover.
- **Ink** (`ink`), **Ink 2** (`ink-2`), **Ink 3** (`ink-3`): primary text; body copy in help and modal; TCNO and inactive tab text.
- **Muted** (`muted`): page summary, decision "Dropped", analysis table headers, ETA.
- **Meta** (`meta`): the 12px meta line, column headers, footers, placeholders. Darkened from the design's #7A7C82 for AA at 12px.
- **Faint** (`faint`): disabled text and quiet icon glyphs (row drop, pill chevron) only; never readable body text.
- **Lines** (`line`, `line-2`, `line-3`): panel border, inner dividers, row separators, in descending strength.
- **Control Line** (`control-line`), **Check Line** (`check-line`): borders of inputs, buttons, checkboxes. **Control Line Hover** (`control-line-hover`): the deeper border of a hovered field or secondary button, and of the open Filters button.
- **Track** (`track`), **Disabled** (`disabled-bg`): tab track and progress track; disabled primary button.
- **Chrome** (`chrome`, `chrome-ink`, `chrome-muted`, `chrome-link`): app bar, selection bar, toasts, modal backdrop; `chrome-link` is also the resting text of the app bar's Edit profile button.

### Named Rules
**The One Blue Rule.** Blue means "you can act on this, or you have selected it". It is never used to colour data, status or decoration, except the processing tone, which is the system acting on the user's behalf.

**The Verdict Owns Its Hue Rule.** Green, ochre and red belong to the verdicts and their direct echoes (criterion status, closing urgency, decisions). Red alone also marks errors and deleting. Nothing else on the page may borrow them.

**The Tinted Panel Speaks In Its Own Tone Rule.** Secondary text on a verdict-tinted panel (counts, criterion labels, section labels) takes that panel's `*-fg`, not grey. Grey on a tinted background reads as disabled and fails contrast.

## Typography

**Body Font:** IBM Plex Sans (with system-ui, -apple-system, Segoe UI, sans-serif)
**Label/Mono Font:** IBM Plex Mono (with ui-monospace, Cascadia Mono, Consolas, monospace)

**Character:** A plain, engineered office pairing. Sans for anything a person reads as language; mono for anything a person compares character by character (TCNOs, references, money, counts, source file paths).

### Hierarchy
- **Headline** (600, 26px, -0.02em): the page title only ("Tenders").
- **Title Large** (600, 18px, 1.35, -0.01em): the tender title in the details modal, never clamped there.
- **Title** (600, 15px): app bar name, run panel status, "Full analysis", section headings in the company profile editor (its sub-headings drop to 13px in `ink-3`). Summary headline in the modal steps to 16px.
- **Body** (400 or 500, 14px, 1.4): row tender titles (500, clamped to two lines), buttons, inputs, run detail, analysis text (1.6).
- **Body Small** (400, 13px, 1.6 in prose): organisation names (500), help copy, footers, facts lines, tab labels (500).
- **Label** (500, 12px, 0.02em): column headers and the 12px meta line in `meta` grey.
- **Section Label** (600, 12px, 0.04em, uppercase): headings of help items and the Not met / Could not be determined groups in the verdict summary. Coloured `meta` on white, or the panel's `*-fg` on a tinted panel.
- **Pill** (600, 12px; 11px for days-left): verdict and status pills.
- **Data** (Plex Mono 500, 14px, tabular): tender value in crore, right-aligned, with a 12px `meta` "Cr" unit.
- **Data Small** (Plex Mono 400, 12px): TCNO, references, tab counts, source file names, kbd hints (11px).

### Named Rules
**The Mono Means Compare Rule.** Mono is for identifiers, money, counts and file paths. It never sets prose, headings or labels.

**The Source Case Rule.** TenderTiger often sends text in lower case. A tender title gets a capital first letter; organisations and places are title-cased with "of", "and" and the like kept lower case. Only first letters are raised, so abbreviations already in capitals (PWD, NHAI) stay.

**The Tabular Figures Rule.** Any line where numbers sit next to each other or update in place (summary lines, run stage, ETA, closing dates, footers) uses `font-variant-numeric: tabular-nums`.

## Layout

A single centred column (max 1480px, 28px side padding, 20px gap between blocks, 120px bottom padding so the floating selection bar never covers the last row). Order: page head (title and summary line left, verdict tabs right, wrapping), run panel, the tender panel (toolbar, table, footer). "How the eligibility check works" is a modal opened from the app bar's ? button.

The tender list is a real `<table>` with explicit ARIA table roles, laid out as CSS grid rows. Wide (1200px and up): seven columns, `52px | 2.6fr | 1.5fr | 120px | 150px | 190px | 44px` (select, Tender, Organisation, Value, Closes, Eligibility, drop). Each row reads along its first line: the title, organisation, value, closing date and verdict pill share one baseline (grid `first baseline`), with the checkbox and drop button beside it, however tall the verdict or organisation runs. 768 to 1199px: Organisation folds into the Tender cell as a meta line and the search field takes its own toolbar row, so Eligibility stays on screen. Below 768px: the header hides and each row becomes a card with areas `check tender drop / check verdict verdict / check closes value`, with Closes and Value carrying their own 11px labels. In the modal, the analysis table stacks into one bordered block per criterion with each cell labelled by its column. The app bar wraps the company line under the brand below 768px so the full company name and Edit profile stay readable. The company profile editor is an extra-wide scrollable modal (full screen below 992px); on narrow screens each of its tables scrolls sideways inside its own border instead of squeezing its fields below 130px.

Spacing steps are 4, 8, 12, 16, 20 and 28px. Rows use 16px vertical padding; toolbar 14px 16px; panel interiors 16 to 20px; help items 28px apart in an auto-fit grid of 240px minimum columns.

### Named Rules
**The Verdict Stays On Screen Rule.** At every width the verdict column is visible without horizontal scroll. Columns fold or stack before Eligibility is ever dropped.

## Elevation & Depth

Flat by default, with tonal layering doing the work: warm paper, white sheets bordered by a 1px warm line, sunk-paper header and footer bands. Shadow appears only on surfaces that float above the page, and as a tiny lift on the selected tab.

### Shadow Vocabulary
- **Tab lift** (`box-shadow: 0 1px 2px rgba(20, 20, 25, .1)`): the active verdict tab on its track.
- **Floating chrome** (`box-shadow: 0 16px 40px rgba(20, 20, 25, .25)`): the selection bar and toasts.
- **Floating list** (`box-shadow: 0 12px 32px rgba(20, 20, 25, .14)`): a dropdown list open over the page (the organisation filter). Lighter than chrome, because it is white and sits close to its field.
- **Modal** (`box-shadow: 0 24px 60px rgba(20, 20, 25, .18)`), over a `chrome` backdrop at 45% opacity.
- **Focus glow** (`box-shadow: 0 0 0 3px oklch(0.6 0.14 255 / .15)`): a text field you type into (the main search, the search box inside a dropdown list) while focused, with its border turning `accent`.

### Named Rules
**The Floating-Only Shadow Rule.** If it sits in the page flow it has no shadow; if it floats over the page it gets one of the floating shadows (chrome, list or modal).

## Shapes

Soft, consistent rounding that steps with size: 2px for search-match highlights, 4px for kbd hints and inline focus targets, 5px for checkboxes, 6 to 7px for icon buttons, tabs, the app mark, the app bar's Edit profile button and the search box inside a dropdown list, 8px for every 38px control, 10px for the tab track, toasts, dropdown lists and the bordered tables of the profile editor, 12px for the selection bar and verdict summary, 14px for page panels and the modal. Pills, dots, switches and progress bars are fully round. Borders are 1px warm lines throughout; the checkbox alone uses 1.5px.

## Components

### Buttons
Quiet and solid; one primary per area.
- **Shape:** 38px tall, 8px radius; 32px small variant; 36px inside the selection bar.
- **Primary:** `accent` with white 600 text, 16px side padding, may carry a mono count chip (white at 20%). Hover `accent-strong`. Disabled `disabled-bg` with `faint` text.
- **Secondary:** white with a `control-line` border, `ink` text; hover deepens the border and fills `hover`. Pressed decision buttons (Shortlist, Drop) take the eligible or not tone fill and a tone-mixed border. While what it started loads (Default filters), its text turns `faint` and it stays focusable (`aria-disabled`), so the keyboard focus stays on it.
- **Danger:** the secondary shape in red, for deleting only (Delete in Closed tenders): `not-fg` text and icon, a border of `not-dot` mixed 45% into white; hover fills `not-bg` with a full `not-dot` border.
- **Icon button:** 32px, transparent, `muted` glyph; hover gains a border and white fill. Bordered 38px variant sits in the toolbar.
- **Link button:** `accent` underlined text at 12px (Retry, inline actions, Clear filters). In the filters' footer, Clear filters sits before the small secondary Default filters and the small primary Apply filters.
- **Focus:** every interactive element shows a 2px `accent` outline at 2px offset on `:focus-visible`.
- **Transitions:** background, border and colour at 0.15s on `cubic-bezier(.16, 1, .3, 1)`.

### Chips (verdict pills)
- **Style:** fully round, 4px 10px padding, a 6px tone dot then the label, 600 12px in the tone's fg on the tone's bg. Labels are the display forms "Eligible", "Not eligible", "Needs review", "Failed", "Not processed", "Processing".
- **State:** a pill with a result is a button that opens the analysis; hover adds an inset 1px ring mixed from its fg and nudges a chevron 2px right. The processing dot pulses (1s, opacity 1 to .35).
- **Days-left pill:** 11px 600 on `none` by default; ochre within 20 days, red within 7 days and "Closes today".

### Cards / Containers
- **Corner Style:** 14px panels; 12px verdict summary.
- **Background:** `surface`, with `surface-sunk` header and footer bands.
- **Shadow Strategy:** none (see Elevation).
- **Border:** 1px `line`; inner dividers `line-2`, row separators `line-3`.
- **Internal Padding:** 16 to 20px.

### Inputs / Fields
- **Style:** 38px, 8px radius, 1px `control-line`. Search sits on `surface-sunk` with a leading search icon and a `/` kbd hint (left out on touch screens); the organisation filter is a white 220px field (see Searchable Select), followed by the decision and closing date filters, 160px native selects. Below 1300px the search takes its own row and the other controls share the next, with Check eligibility at its right. Native selects (these and the filters' comparisons) draw the same 12px `muted` chevron, and an empty date field shows its dd-mm-yyyy format in `meta`.
- **Focus:** fields you type into turn white with an `accent` border and a 3px soft blue glow; other fields use the 2px accent outline. Fields set flush inside a grid (the profile editor) draw that outline inset by 2px, so it stays inside the cell.
- **Checkbox:** a real `<input type="checkbox">` drawn by CSS: 17px, 5px radius, 1.5px `check-line`; checked and indeterminate fill `accent` with a white tick or bar.
- **Switch:** a `role="switch"` button holding a 28 by 16px track (`accent` when on); Regenerate criteria in the selection bar.

### Searchable Select
A select that opens a list you can search, like select2 (Tom Select over a real `<select>`, which keeps working on its own if the library does not load).
- **Field:** the select's own look: white, 38px, 8px radius, `control-line` border, the chosen value cut short with an ellipsis, and a 12px `muted` Bootstrap Icons chevron 12px from the right edge. Hover deepens the border; the border turns `accent` while the list is open.
- **List:** a white panel as wide as the field but at least 340px (or the screen width less 32px, if that is narrower), 10px radius, 1px `line` border, the floating-list shadow, opening 4px below the field from `<body>` so no panel clips it. A sunk 34px search box sits at the top, above a `line-2` divider, and glows like the main search.
- **Options:** 13px, 8px 12px padding, kept in their own (alphabetical) order while searching. The highlighted option fills `accent-wash`; the chosen one is `accent-strong` 600; matched text sits on `accent-soft` with a 2px radius. No match reads "No organisation matches" in `meta`.

### Navigation
- **App bar:** 56px `chrome` band; 28px `accent` square mark with "TP", 15px 600 title; right side "Checking against" in `chrome-muted`, the company name in 600, and the Edit profile button: 28px, transparent, `chrome-link` 12px 500 text with a pencil icon, 7px radius and a white 18% border; hover turns the text white and the border 36%. It opens the Company Profile Editor. At the far right, 8px apart, Closed tenders in the same style (folder icon and label; below 992px the icon alone in a 28px square, so the company name is never cut short, with the label kept for screen readers) opens the Closed Tenders popup, and a 28px square ? button opens "How the eligibility check works" in a large modal (help items in an auto-fit grid of 240px minimum columns, two across; full screen on phones). On a phone both stay at the right of the brand's line while the company line wraps below.
- **Verdict tabs:** toggle buttons (`aria-pressed`) on a `track` well with 4px inset; each tab is a tone dot, a 13px 500 label and a mono count; the active tab is white with the tab-lift shadow.
- **Page size:** "Per page" in 13px `meta` beside a compact 32px, 76px-wide select (20, 50, 100, 500) with the page's chevron, always in the footer's right-hand group, before the hint or the pager. A new size opens the page holding the first tender shown, and the browser remembers it.
- **Pager:** in the tender panel's footer, on the right (on its own line when narrow), once the list runs past one page; until then the footer keeps its hint. Small secondary Previous and Next buttons 12px either side of a 13px `meta` status, "**Page 2** · tenders 21–40", the page in `ink` 500. TenderTiger gives no total, so there is no "of N": Next is offered while a page comes back full, and an empty next page leaves the list where it is with a short toast. While a page loads, the status reads "Loading page 3…" (or "Loading 100 per page…") and the rows fade to 50%; the new page brings the top of the list into view. Sorting (the Value and Closes headers) is TenderTiger's, over the whole list, and starts again from page 1 with the same loading state; tabs, search and the summary line cover the page shown. A selection carries across pages and orders, and the selection bar counts those "on other pages".

### Verdict Summary (signature)
The first thing in the details modal: a 12px-radius panel tinted with the verdict's bg and bordered with its dot mixed 35% into white. A white-backed pill and a 16px headline lead; counts, section labels and criterion fact labels (Requirement, Company, Source) take the tone fg; each criterion is a two-column definition list with the source file in mono. Failed runs fall back to a neutral sunk panel.

### Company Profile Editor
A form, not a document: the company's figures are edited as fields, and the file behind them stays markdown.
- **Header:** the 18px title "Company profile" and a 13px `muted` line on what the profile is for.
- **Profile title:** a 12px 500 `muted` label over a 38px white field.
- **Sections:** each numbered heading is a 15px 600 title set 12px below a `line-2` divider; sub-headings are 13px 600 `ink-3`. Both are fields that read as headings: at rest a transparent 34px field whose text lines up with the content below, on hover a white fill and `control-line` border, on focus the inset accent outline. A quiet `faint` trash icon button at the right removes the heading with everything under it, asking first when any of that holds a value.
- **Adding structure:** each section ends with small secondary "Add sub-heading" and "Add table" buttons side by side, and the form ends, past a `line-2` divider, with "Add heading", which starts with the next section number. They are a step above the tables' link buttons. A new heading or table arrives with an empty Item / Value table; a new table focuses its first column name, selected, ready to rename.
- **Removing safely:** anything removed that holds values (a heading's contents, a table, a column) asks first; removing a table's last column removes the table.
- **Tables:** each table is a 10px-radius grid with a 1px `line` border: a `surface-sunk` header row of flush 34px column-name fields in 13px 500 `muted`, then rows of flush 38px fields, every column split by `line-3` hairlines. The first column names the row (Company Name, FY24, Package A) in `ink-2` 500; figures use tabular numbers. Hover fills a field with `hover`; focus is the inset accent outline. Each row ends in a quiet `faint` remove icon button. Each column name carries a 26px `faint` remove button that, with a mouse, appears only while that column is pointed at or focused; touch screens show it always.
- **Table footer:** "Add row" and "Add column" accent link buttons with plus icons on the left; "Remove table", a `muted` link button with a trash icon, set apart on the right. Removing is quieter than adding.
- **Footer:** the sunk modal footer with a 13px `meta` note on when changes apply, then Cancel and the primary "Save profile" (Ctrl+S). A click outside does not close the editor, and closing with unsaved edits asks first.

### Closed Tenders
A large popup (full screen on phones) listing the closed tenders whose documents are still in the download folder, so the team can free the space.
- **Header:** the 18px title, then a 13px `muted` line with the count and size of the closed tenders (none while no tender has closed).
- **Tables:** a 10px-radius block with a 1px `line` border and a `surface-sunk` header row of 12px 500 `meta` labels. Each row: the title (two lines, the whole title as its tooltip) over its TCNO in 12px mono `meta`; the closing date over "8 days ago"; the file count over the size, right-aligned; and a small red Delete with a trash icon (the danger button). It asks first, and its toast says what went.
- **No closing date on record:** the tenders checked before closing dates were kept follow under a section label with a 13px `muted` note.
- **Phone:** each tender is a block, the title with Delete beside it, then one line under them, "03 Nov 2026 · 8 days ago" at the left and "3 files · 2.3 MB" at the right (the files take the left when there is no date).
- **States:** "Looking through the download folder…" with a spinner while it scans; a failed scan says so in `not-fg` with Try again; an empty list says so in `muted`, centred.

### Run Panel and Selection Bar
The run panel is a page panel with a 15px status title, a single-line detail and stage line, actions on the right, and a 4px round `accent` progress bar on a `track`. A finished run lists its outcome as tone-dotted counts that filter the list. The selection bar is a floating `chrome` bar with 12px corners centred 24px above the bottom, sliding up 8px on entry; below 768px it spans the width and wraps.

### Verdict Landing
When a tender finishes while the page is watched, its row starts from `accent-soft` and drains to white over 1.4s while the verdict stack is uncovered left to right over 0.45s. Reduced motion removes both and keeps the colour changes.

## Do's and Don'ts

### Do:
- **Do** keep the accent at oklch(0.55 0.14 255) wherever white text sits on it; use the lighter oklch(0.6 0.14 255) only for dots, tints and glows.
- **Do** give every new status a full bg / fg / dot triple and apply it through the tone variables, never as one-off colours.
- **Do** colour secondary text on a tinted verdict panel with that panel's `*-fg`.
- **Do** show money as "₹511.80 Cr" in mono with tabular figures and a `meta` unit; show dates as "02 Nov 2026".
- **Do** use Bootstrap Icons (aria-hidden) for every glyph and CSS-drawn controls on real inputs.
- **Do** keep tabular data in a real `<table>` with ARIA roles, however it is laid out visually.
- **Do** keep the verdict visible at every breakpoint: fold columns, then stack to cards.
- **Do** honour `prefers-reduced-motion`: drop movement, keep meaningful colour change.
- **Do** let people edit structured records as labelled fields (rows in a bordered grid), never as raw markup.
- **Do** open dropdown lists and other pop-overs from `<body>` with the floating-list shadow, so a panel's rounded clipping never cuts them.

### Don't:
- **Don't** use Unicode characters as icons.
- **Don't** use the verdict greens, ochres or reds for anything that is not a verdict, criterion status, urgency or decision; red alone also marks errors and deleting.
- **Don't** set grey secondary text on a tinted panel.
- **Don't** put shadows on in-flow panels or rows.
- **Don't** use `faint` for readable text; it is for disabled states and quiet icons.
- **Don't** show the API's "Not Eligible" or "Requires Review" in the interface; display "Not eligible" and "Needs review".
- **Don't** use the near-black chrome for content panels; it is reserved for the app bar and floating bars.
