"""Educational content.

Owned prose, written for this product rather than assembled from templates. Each guide explains
one thing a contractor needs in order to read the data correctly, and each is deliberately
short. There is no keyword-stuffing, no padded word count and no invented statistics: where a
guide refers to a number, it refers the reader to the live directory so the figure is the one
the database actually holds.

The guides exist because the product's credibility rests on a contractor understanding what a
permit record does and does not prove. A guide that overstates what the data supports would
undermine the same trust the rest of the product works to build.
"""

from __future__ import annotations

from typing import Any

#: The guide catalogue. `sections` is a list of (heading, paragraphs) tuples so the template
#: stays generic and a new guide is a data change.
GUIDES: list[dict[str, Any]] = [
    {
        "slug": "how-contractors-find-construction-opportunities",
        "title": "How commercial HVAC contractors find construction opportunities",
        "summary": (
            "Where public permit data comes from, what it can tell you, and where it stops "
            "being useful."
        ),
        "updated": "September 2026",
        "sections": [
            (
                "Start with the building department, not a lead list",
                [
                    "Every commercial construction project in a Texas city passes through a "
                    "building department. A permit is filed before the work happens, which is "
                    "what makes permit records early — earlier than a project appears in most "
                    "commercial databases.",
                    "The catch is that permit data is published by each city separately, in "
                    "its own format, under its own record types. Dallas, Fort Worth and the "
                    "Collin County cities each describe mechanical work differently, and none "
                    "of them publishes anything resembling a bid notice.",
                ],
            ),
            (
                "Look for mechanical scope, not just a building permit",
                [
                    "A general building permit tells you a building is going up. It says "
                    "nothing about who will install the mechanical systems. A mechanical "
                    "permit, or a work description that names HVAC scope, is the signal that "
                    "the mechanical work is part of the project.",
                    "That distinction is the reason a directory built only on building permits "
                    "produces a long list a contractor cannot use. The list has to be scoped to "
                    "records that actually evidence mechanical activity.",
                ],
            ),
            (
                "Understand what a permit does not tell you",
                [
                    "A permit does not tell you whether the mechanical package is available. "
                    "It does not tell you whether the work is already under contract, who the "
                    "general contractor is, or when bids close. No permit feed in this market "
                    "publishes bid status.",
                    "Treat a permit record as evidence that a project exists and is moving. Use "
                    "it to decide which projects to investigate, not as a substitute for "
                    "contacting the parties involved.",
                ],
            ),
            (
                "Work the record back to the parties",
                [
                    "The practical sequence is: find a project with mechanical evidence, read "
                    "the contributing permits and their status, note the value and building "
                    "class if the source declares them, then contact the parties the record "
                    "names — or the building department if it names none.",
                    "Reading the status carefully matters. A record showing a final "
                    "certificate of occupancy describes a finished building, not a live "
                    "opportunity.",
                ],
            ),
        ],
    },
    {
        "slug": "how-to-evaluate-a-construction-project",
        "title": "How to evaluate a construction project before pursuing it",
        "summary": (
            "A short, repeatable check that separates a project worth a call from a record "
            "that will waste your estimator's afternoon."
        ),
        "updated": "September 2026",
        "sections": [
            (
                "Confirm the work is not already finished",
                [
                    "The single most common wasted effort is pursuing a project that is "
                    "complete. Check the permit status before anything else. Words like "
                    "'final', 'finaled', 'complete' and 'closed' mean the mechanical work on "
                    "record has been let or installed.",
                    "Only a status that indicates active work — issued, plan review, under "
                    "construction — is worth pursuing.",
                ],
            ),
            (
                "Check the evidence is really mechanical",
                [
                    "A plumbing permit for a new office building is not a mechanical "
                    "opportunity, even though the building will need HVAC. Look for a "
                    "mechanical permit at the address, or a description that names mechanical "
                    "scope: HVAC, chiller, boiler, air handler, rooftop unit, ventilation, "
                    "ductwork.",
                    "Be careful with replacement language. A like-for-like rooftop unit swap "
                    "is service work, not a construction opportunity.",
                ],
            ),
            (
                "Weigh the project's scale and building class",
                [
                    "Where a source declares a project value or a building class, use it. A "
                    "declared value is the issuing authority's own figure, not an estimate. "
                    "When a source declares neither, treat the project as unrated rather than "
                    "assuming a size.",
                ],
            ),
            (
                "Check for disagreement between sources",
                [
                    "More than one publisher may describe the same address with different "
                    "values. Both figures are real; neither should be averaged or silently "
                    "preferred. Note the disagreement and confirm with the issuing body.",
                ],
            ),
        ],
    },
    {
        "slug": "reading-permit-evidence",
        "title": "How to read the evidence behind a construction opportunity",
        "summary": (
            "What 'Not verified' means, why every field carries a source, and how to check a "
            "claim yourself."
        ),
        "updated": "September 2026",
        "sections": [
            (
                "Every stated fact has a source",
                [
                    "On an opportunity record, each populated field is supported by at least "
                    "one source record — the publisher, the record identifier, the source's own "
                    "date, and the excerpt that supports the value. A field with no support is "
                    "left empty and shown as 'Not verified'.",
                    "That is deliberate. Filling a gap with a plausible-looking value would "
                    "make the record look complete and make it unsafe to rely on.",
                ],
            ),
            (
                "A missing value is information",
                [
                    "If a project shows no general contractor, that is not a fault in the "
                    "product. It means no source published one. The absence tells you "
                    "something useful about how much is on record for that project.",
                ],
            ),
            (
                "When two sources disagree, both are kept",
                [
                    "Contradictions are preserved rather than reconciled. If two publishers "
                    "state different project values, both are retained and the field is "
                    "flagged. You decide which to trust, with both in front of you.",
                    "Several permits from one publisher at one address are not a "
                    "contradiction — that is normal project scope, and the records are "
                    "clustered into a single project accordingly.",
                ],
            ),
        ],
    },
    {
        "slug": "how-to-track-construction-projects",
        "title": "How to track and monitor commercial construction projects",
        "summary": (
            "Why one-off searching loses opportunities, what a watch list is actually for, and "
            "how change detection turns a static list into a pipeline."
        ),
        "updated": "September 2026",
        "sections": [
            (
                "The problem with searching once",
                [
                    "Most contractors find a project the same way: they search a permit portal "
                    "when they have a slow week, write down what looks interesting, and then "
                    "nothing happens to that list.",
                    "The cost is not the searching. It is that a project's record changes — a "
                    "new permit is filed, a status moves to issued, mechanical scope appears — "
                    "and nobody notices, because nobody is looking at that address again.",
                ],
            ),
            (
                "What a watch list is for",
                [
                    "A watch list is a standing instruction to re-check a set of projects. It "
                    "is not the same as a bookmark. A bookmark answers 'what did I find "
                    "interesting'. A watch answers 'tell me when this changes'.",
                    "That distinction is why the two are separate actions in this product. "
                    "Marking something saved should never quietly enrol you in notifications "
                    "you did not ask for.",
                ],
            ),
            (
                "What counts as a meaningful change",
                [
                    "Not every difference deserves an alert. A record being re-fetched with "
                    "identical content is not news. A new permit number at the same address, a "
                    "status moving from issued to final, mechanical evidence appearing where "
                    "there was none, or a declared value changing — those are.",
                    "The product records each detected difference with its previous value, its "
                    "current value and the source, so an alert can be checked rather than "
                    "trusted. Routine corrections appear on the project timeline without "
                    "raising an alert.",
                ],
            ),
            (
                "From watch list to pipeline",
                [
                    "A watched project that turns out to be worth pursuing should leave the "
                    "watch list and enter your own working process: reviewing, targeting, "
                    "contacted, pursuing, closed out.",
                    "Those stages are your filing decisions. They are deliberately not the "
                    "project's procurement status, which the sources do not publish and this "
                    "product therefore never asserts.",
                ],
            ),
        ],
    },
    {
        "slug": "commercial-construction-leads-explained",
        "title": "How construction project leads work",
        "summary": (
            "What a construction lead actually is, why lead lists vary so much in quality, and "
            "what to check before you trust one."
        ),
        "updated": "September 2026",
        "sections": [
            (
                "A lead is a project worth investigating",
                [
                    "Strip away the marketing and a construction lead is a claim: there is a "
                    "project, at this stage, in this place, and it may need your trade. Every "
                    "lead list is an answer to some part of that claim, and the differences "
                    "between products are mostly about which parts they can actually evidence.",
                    "A list built on permit records can evidence that a project exists and is "
                    "moving. A list built on planning documents can evidence that it is "
                    "designed. Neither can evidence that the mechanical package is still "
                    "available, unless a source publishes bid status, which in most markets "
                    "none does.",
                ],
            ),
            (
                "Why lead quality varies so much",
                [
                    "The usual failure is completeness without provenance. A record is "
                    "assembled, a plausible value is filled in for every empty field, and the "
                    "result reads as authoritative while resting on nothing.",
                    "The second failure is volume. A list that includes every building permit "
                    "in a market is not a lead list for a trade — it is a permit directory, and "
                    "an estimator pays for the sorting.",
                ],
            ),
            (
                "What to check before trusting a lead list",
                [
                    "Ask three questions. Can I open the source this came from? Is a missing "
                    "value shown as missing, or quietly filled in? Does the list state bid "
                    "status only where a source actually published it?",
                    "Those three answers separate a tool that saves an estimator time from one "
                    "that costs an afternoon per bad record.",
                ],
            ),
            (
                "Where public records fit",
                [
                    "Public construction records are the earliest reliable signal in most "
                    "markets, because a permit is filed before the work starts. Their weakness "
                    "is that they are published per city, in different formats, with different "
                    "field sets.",
                    "Turning them into usable leads is mostly an evidence problem: normalise "
                    "the records, keep the source attached to every field, cluster them into "
                    "projects, and leave anything unsupported empty.",
                ],
            ),
        ],
    },
]

GUIDE_SLUGS = tuple(guide["slug"] for guide in GUIDES)
