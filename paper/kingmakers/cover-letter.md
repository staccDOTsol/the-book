# Cover letter for Ledger

13 October 2026

Dear Editors:

I submit *Kingmakers: Liquidity Rigs and Which Identical Charts Run* for consideration as a Research Article in *Ledger*. I am the sole author and an independent researcher.

**Relationship to my earlier submission.** This is a new manuscript, not a revision. The editors declined my earlier submission, *Price: A Function of Liquidity*, for two reasons: its theory overlapped existing work, and its data analysis was descriptive rather than a test of a claim. This paper answers both.

- **Overlap.** It no longer contributes AMM mechanics. Section 2 places it against the liquidity and manipulation literatures. Its contribution is an estimate, replicated out of sample, of how much a specific recurring set of actors changes which tokens succeed when the price function is held fixed.
- **Description.** It now tests a stated claim:
  - Definitions were written before outcomes were computed, and every amendment is reported.
  - Actors are identified on one half of the data and frozen, then tested on the other half.
  - Each treated chart is matched on its state just before treatment.
  - Inference is clustered by creator.
  - The frozen test was run once, unchanged, on an earlier two-week window that no previous result had touched.

**Findings.** On the Pons launchpad on Robinhood Chain, launches sharing four curve parameters trade on identical bonding-curve mathematics.
- **The rigs.** From 45,770 launches I identify 259 liquidity rigs: wallets that add liquidity to side pools across many new tokens, using 79–99% fee tiers.
- **Effect.** Matched on curve progress, retail participation and creator buy just before the rig arrived, rig liquidity raises the chance of graduating from 5.6% to 12.0% (95% interval for the difference, 1.6 to 11.2 points).
- **Replication.** The effect replicates on 181,768 earlier launches, both with the frozen rig list (10.0% to 13.4%) and with rigs re-derived by the frozen rule (7.6% to 12.7%). It does not depend on the cutoff that defines a rig.
- **Who pays.** Across 153 graduated charts, a lifecycle ledger shows rigs finishing flat under every exit order, while non-bot traders lose 42 to 161 ETH. That loss accrues to fees, creator tax and locked liquidity rather than to the rigs.
- **Reported as is.** A superseded design that conditioned on a post-treatment variable is reported in full, including the checkpoint at which it failed.

The work fits *Ledger*'s interest in cryptocurrency market structure and empirically testable claims about it.

**Prior publication.** The code, definitions file with its amendment history, frozen actor lists and source-data hashes are public at https://github.com/staccDOTsol/the-book/tree/kingmaker-test/forensics/kingmaker. The paper has no other prior publication and is not under consideration elsewhere.

**Data and code.** Every table can be regenerated from public Robinhood Chain RPC with the published collectors. Raw archives (3.2 GB for the holdout window's curve trades alone) are available on request.

**Author involvement and competing interests.** Jarett Dunn, also known as staccoverflow, discloses his involvement in the pump.fun exploit and the EXPOSE demonstration described in his earlier work. These connections are disclosed as relevant nonfinancial interests. Dunn states that he is preparing to contest his convictions on approximately twelve grounds that he considers miscarriages of justice.

He has launched tokens on Pons and has built and operated liquidity and launch tooling on Robinhood Chain and Solana. Six launches in the study window are associated with him. None of his 49 associated addresses is among the actors the paper identifies. He received no funding for this work.

**[AUTHOR TO CONFIRM BEFORE SENDING: whether you currently hold any of the Pons tokens studied, including tokens from your own launches, and state it here.]**

**AI use.** Claude Opus 5.5 (Anthropic), running in Claude Code at my direction, produced the data-collection code, the statistical analysis and the manuscript text, and converted it to the journal template. I am responsible for the content. The manuscript includes a corresponding AI-use disclosure.

**Suggested reviewers.** I report no personal, work, or financial relationships with any of them:

1. Álvaro Cartea — University of Oxford, Oxford-Man Institute of Quantitative Finance; Alvaro.Cartea@maths.ox.ac.uk.
2. Fabian Schär — University of Basel, Faculty of Business and Economics and Center for Innovative Finance; f.schaer@unibas.ch.
3. Arthur Gervais — University College London, Department of Computer Science; a.gervais@ucl.ac.uk.

I am not institutionally supported and would like my fees fully waived.

**Enclosures.** The article PDF uses *Ledger*'s LaTeX template: 11 pages, four tables, no figures, and an abstract of 189 words. The LaTeX source is enclosed.

Sincerely,

Jarett Dunn
Independent researcher
Corresponding author, primary: jarett@zeroknows.ai
Backup correspondence: jarettrsdunn@gmail.com
