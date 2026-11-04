# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

The bid team at one infrastructure contractor. They screen the new tenders on the company's TenderTiger subscription and decide which ones are worth a bid. The app checks every tender against this one company's profile.

## Product Purpose

Saves the bid team from reading every tender document to find out whether the company qualifies. The app downloads a tender's documents, reads every file, pulls out the bidder eligibility criteria, checks the company against each one and gives a verdict and a score.

The verdict works as a shortlist filter. Eligible tenders get read in full and considered for a bid, and Not Eligible tenders are mostly dropped without anyone reading them. Success means the team spends its reading time only on tenders it can actually bid for, and doesn't lose a good tender to a wrong Not Eligible.

## Positioning

The app checks the tender against the company's own figures, not against keywords. It reads every document in the tender (including zips inside zips), keeps only the parts relevant to eligibility, and then compares each criterion with the company's figures. Where a requirement depends on another figure, such as the estimated cost, it works out the calculation and shows it. Each criterion names the file it came from. If any file could not be read, an Eligible verdict is downgraded to Requires Review, so an incomplete check never looks like a pass.

## Operating Context

- **Source of tenders:** the TenderTiger website, logged in with the company's account. The app lists the live tenders of a keyword search in pages of 20, 50, 100 or 500 (TenderTiger gives no total), which the team can narrow with the filters of TenderTiger's own search page.
- **Documents:** each tender's documents download as one zip, often with zips nested inside. Typical contents are the NIT, annexures and the BOQ, as PDF, Excel and similar files. The tenders are Indian public-sector works (railways, PWD and others), with values in INR.
- **Pace:** analysis is slow. A typical tender takes several minutes and about 90 LLM calls, so the team selects a batch, the server works through it one tender at a time, and the page shows live progress. The criteria found are saved in the tender's folder, so checking it again (after a profile change, say) takes one LLM call unless the team chooses to regenerate them.
- **Hosting:** a single-process FastAPI app on the company's own machine or server. Job progress is kept in memory and lost on restart; each tender's last result is saved in its folder and loaded again at startup, and the downloaded files are kept.
- **LLM:** a cloud model on ollama.com, which needs an API key. Without the key the tender list still loads, but every analysis fails with a clear message.

## Capabilities and Constraints

- **What the app does:** lists the tenders page by page, lets you search and filter a page by organisation and verdict, processes a selected batch (which may span pages) with live progress, and opens each tender's full analysis in a details view. The analysis is rendered markdown with the criteria table, calculations and verdict, and can be copied.
- **Verdicts:** Eligible, Not Eligible or Requires Review. Requires Review covers "Cannot determine", an answer the app could not read, no criteria found, or an incomplete check.
- **Score:** the percentage of criteria marked Met. Not met and Cannot determine both count against it.
- **Criterion status:** Met, Not met or Cannot determine. The model must use only the company details it is given and must never assume a value.
- **The TenderTiger login stays on the server:** the server downloads with its own session, and the browser only ever sends TCNOs back.
- **Terminology:** TCNO (the tender's ID), tender, NIT, BOQ, eligibility criterion, verdict, score, closing date, days left, organisation (British spelling).
- **The company profile** is markdown in `company_details.md`, edited in the app as a form (Edit profile in the app bar: each table of the profile becomes rows of fields) and saved back as markdown, or edited by hand. Each tender's analysis reads it when it starts, so a saved change applies to the tenders checked after it; existing verdicts are not re-checked. It still holds the sample ("ABC Infrastructure Ltd.") until the team enters the contractor's real profile.

## Brand Commitments

None. This is an internal tool with no brand. "Tender Processing" is a working title, and there is no logo, brand asset or defined voice to follow.

## Evidence on Hand

- **Sample company profile:** in `company_details.md`. It is a sample, not the real company.
- **A real downloaded tender:** `downloads/100091540/`, three PDFs.
- **Sample API responses:** in `README.md`.
- **No real company figures yet.** Future work must not invent turnover, net worth, experience or any other company figure, or present the sample profile as the real company.

## Product Principles

1. **Not Eligible has to be safe to drop.** Tenders marked Not Eligible are dropped without being read, so a wrong one silently loses a real opportunity. Every Not Eligible must show which criterion failed, the source file it came from and the company figures it was compared with.
2. **When unsure, say Requires Review.** A verdict is only definite when the evidence supports it. Unreadable files, missing company data and ambiguous criteria all lead to Requires Review, never to a definite verdict.
3. **Show the working.** Requirements, calculations and company figures are shown next to the verdict, so a bid manager can check a result in seconds without opening the PDFs.
4. **Use the bid team's time well.** The slow part is the analysis, not the person. The team picks a batch, can leave it running, and comes back to a clear shortlist.
